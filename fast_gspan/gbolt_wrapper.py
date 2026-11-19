"""Python wrapper for gBolt C++ implementation.

Standalone version with no dependency on neural_graph_mining.
"""

import glob
import math
import operator
import os
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx
import pandas as pd


class IncompleteResultWarning(RuntimeWarning):
    """gBolt returned only part of the frequent patterns.

    Issued when ``max_projection_size`` or ``time_limit`` stopped at least one
    pattern from being extended. Turn it into an error with
    ``warnings.simplefilter("error", IncompleteResultWarning)``.
    """


# Line printed to stderr by the bundled gBolt once time_limit has passed.
_TIME_LIMIT_REACHED = "gbolt: time limit reached"

# A DFS code: one (from, to, from_label, edge_label, to_label) tuple per edge.
DfsCode = List[Tuple[int, int, int, int, int]]


def _warn_incomplete(
    stop_reasons: List[str], stacklevel: int, progressed: bool = True
) -> None:
    """Issue an IncompleteResultWarning for the given unextended patterns.

    *stacklevel* is relative to the caller of this function.
    """
    counts = Counter(stop_reasons)
    detail = ", ".join(f"{n} by {reason}" for reason, n in sorted(counts.items()))
    message = (
        f"gBolt did not extend {len(stop_reasons)} frequent pattern(s) ({detail}); "
        f"their supergraphs may be missing from the result. Continue with "
        f"FastgSpan.resume_from_graphs(). Patterns stopped by max_projection_size "
        f"need a larger max_projection_size to be extended."
    )
    if not progressed:
        message += (
            " This run extended nothing: raise time_limit (reading the input and "
            "returning to the unextended patterns takes time too) or "
            "max_projection_size."
        )
    warnings.warn(message, IncompleteResultWarning, stacklevel=stacklevel + 1)


def _effective_min_support(min_support: float) -> int:
    """Return the support threshold mined with for *min_support*.

    A plain ``int``, also for NumPy scalars. Every pattern occurs in at least
    one graph, so the threshold is at least 1.
    """
    try:
        # Exact for int and NumPy integers, which math.ceil turns into floats
        threshold = operator.index(min_support)
    except TypeError:
        if hasattr(min_support, "as_integer_ratio"):
            # Exact for floats of any precision. math.ceil goes through a
            # Python float, which rounds np.longdouble either way.
            numerator, denominator = min_support.as_integer_ratio()
            threshold = -(-numerator // denominator)
        else:
            threshold = math.ceil(min_support)
    return max(threshold, 1)


def _relative_support(min_support: int, n_graphs: int) -> float:
    """Return the value of gBolt's ``-s`` option for an absolute *min_support*.

    gBolt mines the patterns found in at least ``int(N * s)`` graphs, where N
    also counts the ``t # -1`` line that ends the input, so N is
    ``n_graphs + 1``. Aiming halfway between two counts keeps rounding errors
    from landing on the wrong one. Requires ``1 <= min_support <= n_graphs``,
    which keeps the result within gBolt's range ``(0, 1]``.
    """
    return (min_support + 0.5) / (n_graphs + 1)


# Key of the DataFrame.attrs entry in which FastgSpan records how a result
# was mined, for resume_from_graphs() to check
_ATTRS_KEY = "fast_gspan"


class ProgressMonitor:
    """Monitor gBolt output files for progress reporting."""

    def __init__(self, output_file_base: str, update_interval: float = 0.5):
        self.output_file_base = output_file_base
        self.update_interval = update_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pattern_count = 0
        self._start_time = 0.0

    def _count_patterns_in_file(self, filepath: str) -> int:
        try:
            if not os.path.exists(filepath):
                return 0
            with open(filepath) as f:
                return f.read().count("t #")
        except Exception:
            return 0

    def _monitor_loop(self):
        spinner_chars = "|/-\\"
        spinner_idx = 0

        while not self._stop_event.is_set():
            total_patterns = 0
            thread_files = glob.glob(f"{self.output_file_base}.t*")
            for thread_file in thread_files:
                total_patterns += self._count_patterns_in_file(thread_file)

            self._pattern_count = total_patterns
            elapsed = time.time() - self._start_time

            spinner = spinner_chars[spinner_idx % len(spinner_chars)]
            status = f"\r{spinner} Mining... Found {total_patterns} patterns (elapsed: {elapsed:.1f}s)"
            sys.stdout.write(status)
            sys.stdout.flush()

            spinner_idx += 1
            self._stop_event.wait(self.update_interval)

    def start(self):
        self._start_time = time.time()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self, final_count: Optional[int] = None):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)

        elapsed = time.time() - self._start_time
        count = final_count if final_count is not None else self._pattern_count
        sys.stdout.write(
            f"\rMining complete. Found {count} patterns (elapsed: {elapsed:.1f}s)\n"
        )
        sys.stdout.flush()


def _find_vendor_gbolt() -> Optional[str]:
    """Find gBolt executable built from the vendored source."""
    vendor_dir = Path(__file__).parent / "vendor" / "gbolt" / "build"
    candidate = vendor_dir / "gbolt"
    if candidate.exists() and os.access(str(candidate), os.X_OK):
        return str(candidate)
    return None


def _vendor_lib_env() -> dict:
    """Return env dict with bundled lib/ added to the library search path.

    On macOS wheels, libomp.dylib is bundled alongside the gBolt binary in a
    lib/ subdirectory. We set DYLD_LIBRARY_PATH (macOS) or LD_LIBRARY_PATH
    (Linux) so the binary can find them at runtime.
    """
    env = os.environ.copy()
    lib_dir = Path(__file__).parent / "vendor" / "gbolt" / "build" / "lib"
    if lib_dir.is_dir():
        key = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
        existing = env.get(key, "")
        env[key] = f"{lib_dir}:{existing}" if existing else str(lib_dir)
    return env


class GBoltWrapper:
    """Low-level Python wrapper for the gBolt binary.

    ``max_projection_size`` is an opt-in memory guard: a pattern of two or more
    edges with more embeddings than this is still reported but not extended, so
    its supergraphs may be missing and an :class:`IncompleteResultWarning` is
    issued. The default ``0`` means unlimited: no pattern is lost for having
    too many embeddings.

    ``time_limit`` is a budget in seconds for one mining call. Once it is used
    up, the patterns reached are still reported but not extended, with the same
    warning. ``timeout``, in contrast, kills gBolt and raises an error.

    Every returned pattern has a ``stop_reason``: ``None`` when it was
    extended, otherwise ``"max_projection_size"`` or ``"time_limit"``.
    """

    def __init__(
        self,
        gbolt_path: Optional[str] = None,
        min_support: int = 2,
        max_vertices: int = 0,
        num_threads: int = 0,
        timeout: Optional[float] = None,
        show_progress: bool = False,
        verbose: bool = False,
        max_projection_size: int = 0,
        time_limit: Optional[float] = None,
    ):
        self.min_support = min_support
        self.max_vertices = max_vertices
        self.num_threads = num_threads
        self.timeout = timeout
        self.show_progress = show_progress
        self.verbose = verbose
        self.max_projection_size = max_projection_size
        self.time_limit = time_limit
        self._gbolt_path = self._find_gbolt_executable(gbolt_path)

        if not self._gbolt_path:
            raise RuntimeError(
                "gBolt executable not found. Run: python -m fast_gspan build"
            )

        self._verify_binary()

    def _verify_binary(self):
        """Check that the gBolt binary actually runs on this system."""
        try:
            result = subprocess.run(
                [self._gbolt_path, "--help"],
                capture_output=True,
                text=True,
                timeout=5,
                env=_vendor_lib_env(),
            )
            # returncode 0 or 1 are both acceptable (--help may return 1)
        except OSError as e:
            raise RuntimeError(
                f"gBolt binary cannot execute: {e}\n"
                f"This may be a GLIBC version mismatch. The pre-built binary "
                f"may require a newer glibc than your system provides.\n"
                f"Fix: rebuild from source with 'python -m fast_gspan build'"
            ) from e
        else:
            stderr = result.stderr.strip()
            if "GLIBC" in stderr or "GLIBCXX" in stderr:
                raise RuntimeError(
                    f"gBolt binary incompatible with this system:\n{stderr}\n\n"
                    f"Fix: rebuild from source with 'python -m fast_gspan build'"
                )

    def _find_gbolt_executable(self, gbolt_path: Optional[str]) -> Optional[str]:
        if gbolt_path and os.path.exists(gbolt_path):
            return gbolt_path

        # 1. Vendored build
        vendored = _find_vendor_gbolt()
        if vendored:
            return vendored

        # 2. Common relative paths
        search_paths = [
            "./gBolt/build/gbolt",
            "../gBolt/build/gbolt",
            "../../gBolt/build/gbolt",
            "gBolt/build/gbolt",
        ]
        for path in search_paths:
            if os.path.exists(path):
                return os.path.abspath(path)

        return None

    # ----- graph conversion -----

    @staticmethod
    def _graph_to_gbolt_format(graph: nx.Graph, graph_id: int = 0) -> str:
        lines = [f"t # {graph_id}"]
        nodes = sorted(graph.nodes())
        node_to_idx = {node: idx for idx, node in enumerate(nodes)}

        for node in nodes:
            label = graph.nodes[node].get("label", 0)
            lines.append(f"v {node_to_idx[node]} {label}")

        for u, v in graph.edges():
            edge_label = graph[u][v].get("label", 0)
            lines.append(f"e {node_to_idx[u]} {node_to_idx[v]} {edge_label}")

        return "\n".join(lines)

    def _graphs_to_gbolt_file(self, graphs: List[nx.Graph], filename: str) -> None:
        with open(filename, "w") as f:
            for i, graph in enumerate(graphs):
                f.write(self._graph_to_gbolt_format(graph, i))
                f.write("\n")
            f.write("t # -1\n")

    # ----- output parsing -----

    def _collect_gbolt_thread_outputs(
        self, temp_dir: str, base_output_file: str
    ) -> List[Dict[str, Any]]:
        patterns: List[Dict[str, Any]] = []
        thread_files = glob.glob(f"{base_output_file}.t*")

        for thread_file in sorted(thread_files):
            if os.path.getsize(thread_file) > 0:
                try:
                    with open(thread_file) as f:
                        content = f.read().strip()
                        if content:
                            patterns.extend(self._parse_gbolt_thread_output(content))
                except Exception as e:
                    if self.verbose:
                        print(f"Warning: Could not read {thread_file}: {e}")

        return self._deduplicate_patterns(patterns)

    @staticmethod
    def _parse_gbolt_thread_output(output_text: str) -> List[Dict[str, Any]]:
        patterns: List[Dict[str, Any]] = []
        lines = output_text.strip().split("\n")
        current_pattern: Optional[Dict[str, Any]] = None
        graph_lines: List[str] = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line.startswith("t #"):
                if current_pattern is not None and graph_lines:
                    current_pattern["graph_data"] = "\n".join(graph_lines)
                    patterns.append(current_pattern)

                parts = line.split()
                if len(parts) >= 5 and parts[3] == "*":
                    current_pattern = {
                        "pattern_id": int(parts[2]),
                        "support": int(parts[4]),
                        "vertices": [],
                        "edges": [],
                        "graph_data": "",
                        "stop_reason": None,
                    }
                    graph_lines = []
                else:
                    current_pattern = None

            elif line.startswith("v ") and current_pattern is not None:
                parts = line.split()
                if len(parts) >= 3:
                    current_pattern["vertices"].append(
                        (int(parts[1]), int(parts[2]))
                    )
                    graph_lines.append(line)

            elif line.startswith("e ") and current_pattern is not None:
                parts = line.split()
                # Extended format: e <from> <to> <from_label> <edge_label> <to_label>
                if len(parts) >= 6:
                    from_v, to_v = int(parts[1]), int(parts[2])
                    from_label = int(parts[3])
                    edge_label = int(parts[4])
                    to_label = int(parts[5])
                    current_pattern["edges"].append((from_v, to_v, edge_label))
                    current_pattern.setdefault("dfs_codes", []).append(
                        (from_v, to_v, from_label, edge_label, to_label)
                    )
                    graph_lines.append(line)
                # Legacy format: e <from> <to> <edge_label>
                elif len(parts) >= 4:
                    current_pattern["edges"].append(
                        (int(parts[1]), int(parts[2]), int(parts[3]))
                    )
                    graph_lines.append(line)

            # Reported but not extended: u: <reason>
            elif line.startswith("u: ") and current_pattern is not None:
                current_pattern["stop_reason"] = line[3:].strip()

            elif line.startswith("x: ") and current_pattern is not None:
                graph_ids_str = line[3:].strip()
                if graph_ids_str:
                    current_pattern["graph_occurrences"] = [
                        int(x) for x in graph_ids_str.split() if x.strip()
                    ]

        if current_pattern is not None and graph_lines:
            current_pattern["graph_data"] = "\n".join(graph_lines)
            patterns.append(current_pattern)

        return patterns

    @staticmethod
    def _deduplicate_patterns(
        patterns: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        unique: List[Dict[str, Any]] = []
        seen: set = set()

        for pattern in patterns:
            key = (
                tuple(sorted(pattern["vertices"])),
                tuple(sorted(pattern["edges"])),
            )
            if key not in seen:
                seen.add(key)
                unique.append(pattern)
            else:
                for existing in unique:
                    existing_key = (
                        tuple(sorted(existing["vertices"])),
                        tuple(sorted(existing["edges"])),
                    )
                    if existing_key == key and pattern["support"] > existing["support"]:
                        existing["support"] = pattern["support"]
                        break

        return unique

    @staticmethod
    def _write_resume_file(dfs_codes: List[DfsCode], filename: str) -> None:
        with open(filename, "w") as f:
            for i, dfs_code in enumerate(dfs_codes):
                f.write(f"t # {i}\n")
                for frm, to, from_label, edge_label, to_label in dfs_code:
                    f.write(f"e {frm} {to} {from_label} {edge_label} {to_label}\n")

    # ----- main entry point -----

    def mine_frequent_subgraphs(
        self,
        graphs: List[nx.Graph],
        resume_from: Optional[List[DfsCode]] = None,
    ) -> List[Dict[str, Any]]:
        """Mine frequent subgraphs from a list of NetworkX graphs.

        Returns a list of pattern dicts with keys:
        pattern_id, support, vertices, edges, stop_reason, dfs_codes (optional),
        graph_occurrences (optional), graph_data.

        With *resume_from*, the DFS codes of patterns that a previous run on
        the same graphs reported but did not extend, only those patterns and
        their supergraphs are mined. Each resumed pattern that is reached is
        reported again, with its new ``stop_reason``.
        """
        patterns, _ = self._run(graphs, resume_from)
        if patterns is None:
            return []

        stop_reasons = [p["stop_reason"] for p in patterns if p.get("stop_reason")]
        if stop_reasons:
            _warn_incomplete(stop_reasons, stacklevel=2)
        return patterns

    def _run(
        self,
        graphs: List[nx.Graph],
        resume_from: Optional[List[DfsCode]] = None,
        min_support: Optional[int] = None,
    ) -> Tuple[Optional[List[Dict[str, Any]]], bool]:
        """Run gBolt once, without issuing IncompleteResultWarning.

        Returns the raw patterns (None if gBolt failed) and whether
        ``time_limit`` passed during the run. *min_support* overrides
        ``self.min_support``.
        """
        if not graphs:
            return [], False

        min_support = _effective_min_support(
            self.min_support if min_support is None else min_support
        )
        if min_support > len(graphs):
            # No pattern can occur in more graphs than there are
            return [], False
        relative_support = _relative_support(min_support, len(graphs))

        started = time.monotonic()

        with tempfile.TemporaryDirectory() as temp_dir:
            input_file = os.path.join(temp_dir, "input.txt")
            output_file = os.path.join(temp_dir, "output.txt")

            self._graphs_to_gbolt_file(graphs, input_file)

            cmd = [
                self._gbolt_path,
                "-i", input_file,
                "-o", output_file,
                "-s", str(relative_support),
                "-d",
            ]

            if self.max_vertices > 0:
                cmd.extend(["-x", str(self.max_vertices)])

            if self.max_projection_size > 0:
                cmd.extend(["--max-projection-size", str(self.max_projection_size)])

            if resume_from is not None:
                resume_file = os.path.join(temp_dir, "resume.txt")
                self._write_resume_file(resume_from, resume_file)
                cmd.extend(["--resume", resume_file])

            env = _vendor_lib_env()
            if self.num_threads > 0:
                env["OMP_NUM_THREADS"] = str(self.num_threads)

            progress_monitor = None
            if self.show_progress:
                progress_monitor = ProgressMonitor(output_file)

            if self.time_limit is not None and self.time_limit > 0:
                # The budget covers the whole call, so take off the time already
                # spent writing the input. A used-up budget still has to be
                # positive: gBolt reads 0 as "no limit".
                remaining = self.time_limit - (time.monotonic() - started)
                cmd.extend(["--time-limit", f"{max(remaining, 1e-9):.9f}"])

            try:
                if self.verbose:
                    print(f"Running: {' '.join(cmd)}")

                if progress_monitor:
                    progress_monitor.start()

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    env=env,
                )

                if result.returncode != 0:
                    if progress_monitor:
                        progress_monitor.stop(0)
                    warnings.warn(
                        f"gBolt exited with code {result.returncode}. "
                        f"stderr: {result.stderr.strip()[:500]}",
                        RuntimeWarning,
                        stacklevel=3,
                    )
                    return None, False

                output_patterns = self._collect_gbolt_thread_outputs(
                    temp_dir, output_file
                )

                if progress_monitor:
                    progress_monitor.stop(len(output_patterns))

            except subprocess.TimeoutExpired:
                if progress_monitor:
                    progress_monitor.stop(0)
                raise RuntimeError(
                    f"gBolt execution timed out after {self.timeout}s. "
                    f"Increase timeout or reduce dataset size."
                )
            except Exception as e:
                if progress_monitor:
                    progress_monitor.stop(0)
                raise RuntimeError(f"Error running gBolt: {e}")

            return output_patterns, _TIME_LIMIT_REACHED in result.stderr


_RESULT_COLUMNS = [
    "support",
    "description",
    "num_vert",
    "pattern_id",
    "vertices",
    "edges",
    "graph_ids",
    "stop_reason",
]


class FastgSpan:
    """High-level gSpan interface backed by gBolt.

    Provides a convenient API for frequent subgraph mining. If gBolt is not
    available an informative error is raised (no silent fallback).

    By default no pattern is lost for having too many embeddings. Setting
    ``max_projection_size`` trades that for bounded memory and ``time_limit``
    for bounded time; see :class:`GBoltWrapper`. The patterns that were not
    extended have a ``stop_reason`` in the result, and
    :meth:`resume_from_graphs` continues from them.
    """

    def __init__(
        self,
        gbolt_path: Optional[str] = None,
        min_support: int = 2,
        min_num_vertices: int = 1,
        max_num_vertices: int = 0,
        num_threads: int = 0,
        timeout: Optional[float] = None,
        show_progress: bool = False,
        verbose: bool = False,
        max_projection_size: int = 0,
        time_limit: Optional[float] = None,
    ):
        # min_support is a property of the backend, see below
        self.min_num_vertices = min_num_vertices
        self.max_num_vertices = max_num_vertices
        self.num_threads = num_threads
        self.timeout = timeout
        self.show_progress = show_progress
        self.verbose = verbose
        self.max_projection_size = max_projection_size
        self.time_limit = time_limit

        self._gbolt = GBoltWrapper(
            gbolt_path=gbolt_path,
            min_support=min_support,
            max_vertices=max_num_vertices,
            num_threads=num_threads,
            timeout=timeout,
            show_progress=show_progress,
            verbose=verbose,
            max_projection_size=max_projection_size,
            time_limit=time_limit,
        )

    @property
    def min_support(self) -> int:
        """Minimum absolute support.

        Stored in the backend only, so that changing it after construction
        changes what is mined, filtered and recorded alike.
        """
        return self._gbolt.min_support

    @min_support.setter
    def min_support(self, value: int) -> None:
        self._gbolt.min_support = value

    # ----- pattern utilities -----

    @staticmethod
    def _pattern_to_description(pattern: Dict[str, Any]) -> str:
        """Build a human-readable DFS-code description of a pattern.

        Format per edge line: ``(<from>, <to>, <from_label>, <edge_label>, <to_label>)``
        """
        if "dfs_codes" in pattern and pattern["dfs_codes"]:
            lines = []
            for frm, to, fl, el, tl in pattern["dfs_codes"]:
                lines.append(f"({frm}, {to}, {fl}, {el}, {tl})")
            return "\n".join(lines)

        # Fallback when dfs_codes are not available
        lines = []
        for frm, to, el in pattern.get("edges", []):
            lines.append(f"({frm}, {to}, {el})")
        return "\n".join(lines)

    @staticmethod
    def pattern_to_graph(pattern: Dict[str, Any]) -> nx.Graph:
        """Convert a mined pattern dict into a NetworkX graph."""
        g = nx.Graph()
        for vid, vlb in pattern.get("vertices", []):
            g.add_node(vid, label=vlb)
        for frm, to, elb in pattern.get("edges", []):
            g.add_edge(frm, to, label=elb)
        return g

    # ----- mining -----

    def _pattern_to_row(
        self, pattern: Dict[str, Any], threshold: int
    ) -> Optional[Dict[str, Any]]:
        """Convert a raw pattern into a result row, or None if it is filtered out.

        *threshold* is the effective min_support of the call. ``min_num_vertices``
        is not applied here, see :meth:`_is_result_row`.
        """
        num_vertices = len(pattern["vertices"])
        if pattern["support"] < threshold:
            return None
        if self.max_num_vertices > 0 and num_vertices > self.max_num_vertices:
            return None

        return {
            "support": pattern["support"],
            "description": self._pattern_to_description(pattern),
            "num_vert": num_vertices,
            "pattern_id": pattern["pattern_id"],
            "vertices": pattern["vertices"],
            "edges": pattern["edges"],
            "graph_ids": pattern.get("graph_occurrences"),
            "stop_reason": pattern.get("stop_reason"),
        }

    def _is_result_row(self, row: Dict[str, Any]) -> bool:
        """Apply ``min_num_vertices``.

        An unextended pattern is kept whatever its size: it is where
        resume_from_graphs() continues from.
        """
        return row["num_vert"] >= self.min_num_vertices or bool(row["stop_reason"])

    def _patterns_to_rows(
        self, patterns: Optional[List[Dict[str, Any]]], threshold: int
    ) -> List[Dict[str, Any]]:
        rows = (self._pattern_to_row(pattern, threshold) for pattern in patterns or [])
        return [row for row in rows if row is not None]

    @staticmethod
    def _row_dfs_code(row: Dict[str, Any]) -> DfsCode:
        """Rebuild the DFS code of a result row from its vertices and edges."""
        labels = {int(vid): int(label) for vid, label in row["vertices"]}
        return [
            (int(frm), int(to), labels[int(frm)], int(label), labels[int(to)])
            for frm, to, label in row["edges"]
        ]

    @staticmethod
    def _mark(df: pd.DataFrame, threshold: int) -> pd.DataFrame:
        """Record in ``df.attrs`` the support *threshold* it was mined with.

        *threshold* comes from :func:`_effective_min_support`, so it is a plain
        ``int`` and the attrs stay JSON-serializable (Parquet).
        """
        df.attrs[_ATTRS_KEY] = {"min_support": threshold}
        return df

    @staticmethod
    def _check_min_support(previous: pd.DataFrame, threshold: int) -> None:
        """Raise ValueError unless *previous* was mined with *threshold*.

        Resuming cannot find the patterns that a higher threshold left out:
        they are not below any unextended pattern of *previous*. *threshold*
        is the effective min_support of the resume.
        """
        settings = previous.attrs.get(_ATTRS_KEY)
        if not (isinstance(settings, dict) and "min_support" in settings):
            # fast-gspan 0.1.4 and earlier raised a min_support below 1% of
            # the graphs to 1%, and the threshold they used cannot be worked
            # out reliably afterwards
            raise ValueError(
                "previous has no record of the min_support it was mined with "
                "(previous.attrs), as is the case for results of failed runs "
                "and of fast-gspan 0.1.4 and earlier. Those versions could "
                "mine with a higher threshold than min_support, and resuming "
                "cannot find the patterns this left out: call "
                "run_from_graphs() again"
            )
        if settings["min_support"] != threshold:
            raise ValueError(
                f"previous was mined with min_support="
                f"{settings['min_support']}, not {threshold}"
            )

    def run_from_graphs(self, graphs: List[nx.Graph]) -> pd.DataFrame:
        """Mine frequent subgraphs from *graphs* and return a DataFrame."""
        # Read once: mining, filtering and the attrs record use the same value
        threshold = _effective_min_support(self.min_support)
        if not graphs:
            return self._mark(pd.DataFrame(columns=_RESULT_COLUMNS), threshold)

        patterns, _ = self._gbolt._run(graphs, min_support=threshold)
        rows = [
            row
            for row in self._patterns_to_rows(patterns, threshold)
            if self._is_result_row(row)
        ]

        stop_reasons = [row["stop_reason"] for row in rows if row["stop_reason"]]
        if stop_reasons:
            _warn_incomplete(stop_reasons, stacklevel=2)

        df = pd.DataFrame(rows, columns=_RESULT_COLUMNS)
        if patterns is None:
            # gBolt failed (already warned about): the empty result is left
            # unmarked, so that it cannot be resumed as if it were complete
            return df
        return self._mark(df, threshold)

    def run_from_file(self, filepath: str) -> pd.DataFrame:
        """Read graphs from a gSpan-format file and mine frequent subgraphs."""
        graphs = _read_gspan_file(filepath)
        return self.run_from_graphs(graphs)

    def resume_from_graphs(
        self, graphs: List[nx.Graph], previous: pd.DataFrame
    ) -> pd.DataFrame:
        """Continue mining from an incomplete result and return the merged result.

        *previous* is a DataFrame returned by :meth:`run_from_graphs` or by an
        earlier resume, for the same *graphs* (in the same order), the same
        ``min_support`` and the same ``max_num_vertices``. Mining continues
        from its rows whose ``stop_reason`` is set. The
        embeddings of those patterns are not stored anywhere: gBolt rebuilds
        them from their DFS codes, so each resume first spends some time
        getting back to where the previous run stopped.

        The returned DataFrame holds the rows of *previous* with an updated
        ``stop_reason``, plus the newly found patterns. It is complete once no
        ``stop_reason`` is left. *previous* is not modified.

        Results record their ``min_support`` in ``DataFrame.attrs``. A
        *previous* mined with another one, or without that record (results of
        fast-gspan 0.1.4 and earlier), is rejected with ValueError.
        """
        if "stop_reason" not in previous.columns:
            raise ValueError(
                "previous has no 'stop_reason' column; pass a result of "
                "run_from_graphs() or resume_from_graphs()"
            )
        # Read once: checking, mining, filtering and the attrs record use the
        # same value
        threshold = _effective_min_support(self.min_support)
        self._check_min_support(previous, threshold)

        records = previous.to_dict("records")
        for record in records:
            if pd.isna(record["stop_reason"]):
                record["stop_reason"] = None
        pending = {r["description"]: r for r in records if r["stop_reason"]}
        if not pending or not graphs:
            return previous.copy()

        dfs_codes = [self._row_dfs_code(record) for record in pending.values()]
        patterns, time_limit_reached = self._gbolt._run(
            graphs, resume_from=dfs_codes, min_support=threshold
        )
        if patterns is None:
            # gBolt failed (already warned about): nothing was resumed
            return previous.copy()

        new_rows: Dict[str, Dict[str, Any]] = {}
        for row in self._patterns_to_rows(patterns, threshold):
            new_rows.setdefault(row["description"], row)

        progressed = False
        not_reached = 0
        for description, record in pending.items():
            new = new_rows.pop(description, None)
            if new is None:
                # Not reported again: still unextended, for the same reason
                not_reached += 1
            elif new["support"] != record["support"]:
                raise ValueError(
                    f"previous does not match these graphs: support of pattern "
                    f"{description!r} is {new['support']}, was {record['support']}"
                )
            else:
                progressed = progressed or new["stop_reason"] is None
                record["stop_reason"] = new["stop_reason"]

        # Without a time limit every pattern to resume must have been found again
        if not_reached and not time_limit_reached:
            raise ValueError(
                f"previous does not match these graphs or settings (min_support, "
                f"max_num_vertices): {not_reached} of its unextended patterns were "
                f"not found"
            )

        known = {record["description"] for record in records}
        added = [row for desc, row in new_rows.items() if desc not in known]
        progressed = progressed or bool(added)

        # Rows below min_num_vertices were only kept while they were unextended
        merged = [row for row in records + added if self._is_result_row(row)]

        stop_reasons = [r["stop_reason"] for r in merged if r["stop_reason"]]
        if stop_reasons:
            _warn_incomplete(stop_reasons, stacklevel=2, progressed=progressed)

        return self._mark(pd.DataFrame(merged, columns=_RESULT_COLUMNS), threshold)

    def resume_from_file(self, filepath: str, previous: pd.DataFrame) -> pd.DataFrame:
        """Like :meth:`resume_from_graphs`, for graphs in a gSpan-format file."""
        graphs = _read_gspan_file(filepath)
        return self.resume_from_graphs(graphs, previous)


# ----- utility: read gSpan text format -----

def _read_gspan_file(filepath: str) -> List[nx.Graph]:
    """Parse a gSpan-format graph database file into a list of NetworkX graphs."""
    graphs: List[nx.Graph] = []
    current: Optional[nx.Graph] = None

    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("t #"):
                parts = line.split()
                gid = int(parts[2])
                if gid == -1:
                    break
                current = nx.Graph()
                graphs.append(current)
            elif line.startswith("v ") and current is not None:
                parts = line.split()
                current.add_node(int(parts[1]), label=int(parts[2]))
            elif line.startswith("e ") and current is not None:
                parts = line.split()
                current.add_edge(int(parts[1]), int(parts[2]), label=int(parts[3]))

    return graphs
