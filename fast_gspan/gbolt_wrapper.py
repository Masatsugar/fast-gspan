"""Python wrapper for gBolt C++ implementation.

Standalone version with no dependency on neural_graph_mining.
"""

import glob
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import networkx as nx
import pandas as pd


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


class GBoltWrapper:
    """Low-level Python wrapper for the gBolt binary."""

    def __init__(
        self,
        gbolt_path: Optional[str] = None,
        min_support: int = 2,
        max_vertices: int = 0,
        num_threads: int = 0,
        show_progress: bool = False,
        verbose: bool = False,
    ):
        self.min_support = min_support
        self.max_vertices = max_vertices
        self.num_threads = num_threads
        self.show_progress = show_progress
        self.verbose = verbose
        self._gbolt_path = self._find_gbolt_executable(gbolt_path)

        if not self._gbolt_path:
            raise RuntimeError(
                "gBolt executable not found. Run: python -m fast_gspan build"
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

    # ----- main entry point -----

    def mine_frequent_subgraphs(
        self, graphs: List[nx.Graph]
    ) -> List[Dict[str, Any]]:
        """Mine frequent subgraphs from a list of NetworkX graphs.

        Returns a list of pattern dicts with keys:
        pattern_id, support, vertices, edges, dfs_codes (optional), graph_data.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            input_file = os.path.join(temp_dir, "input.txt")
            output_file = os.path.join(temp_dir, "output.txt")

            self._graphs_to_gbolt_file(graphs, input_file)

            n_graphs = len(graphs)
            relative_support = max(self.min_support / n_graphs, 0.01)
            if relative_support >= 1.0:
                relative_support = 1.0 - 0.5 / n_graphs

            cmd = [
                self._gbolt_path,
                "-i", input_file,
                "-o", output_file,
                "-s", str(relative_support),
                "-d",
            ]

            if self.max_vertices > 0:
                cmd.extend(["-x", str(self.max_vertices)])

            env = os.environ.copy()
            if self.num_threads > 0:
                env["OMP_NUM_THREADS"] = str(self.num_threads)

            progress_monitor = None
            if self.show_progress:
                progress_monitor = ProgressMonitor(output_file)

            try:
                if self.verbose:
                    print(f"Running: {' '.join(cmd)}")

                if progress_monitor:
                    progress_monitor.start()

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    env=env,
                )

                if result.returncode != 0:
                    if progress_monitor:
                        progress_monitor.stop(0)
                    if self.verbose:
                        print(f"gBolt stderr: {result.stderr}")
                    return []

                output_patterns = self._collect_gbolt_thread_outputs(
                    temp_dir, output_file
                )

                if progress_monitor:
                    progress_monitor.stop(len(output_patterns))

                return output_patterns

            except subprocess.TimeoutExpired:
                if progress_monitor:
                    progress_monitor.stop(0)
                raise RuntimeError("gBolt execution timed out")
            except Exception as e:
                if progress_monitor:
                    progress_monitor.stop(0)
                raise RuntimeError(f"Error running gBolt: {e}")


class FastgSpan:
    """High-level gSpan interface backed by gBolt.

    Provides a convenient API for frequent subgraph mining. If gBolt is not
    available an informative error is raised (no silent fallback).
    """

    def __init__(
        self,
        gbolt_path: Optional[str] = None,
        min_support: int = 2,
        min_num_vertices: int = 1,
        max_num_vertices: int = 10,
        num_threads: int = 0,
        show_progress: bool = False,
        verbose: bool = False,
    ):
        self.min_support = min_support
        self.min_num_vertices = min_num_vertices
        self.max_num_vertices = max_num_vertices
        self.num_threads = num_threads
        self.show_progress = show_progress
        self.verbose = verbose

        self._gbolt = GBoltWrapper(
            gbolt_path=gbolt_path,
            min_support=min_support,
            max_vertices=max_num_vertices,
            num_threads=num_threads,
            show_progress=show_progress,
            verbose=verbose,
        )

    # ----- pattern description (standalone, no gSpan dependency) -----

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

    def _pattern_to_networkx(self, pattern: Dict[str, Any]) -> nx.Graph:
        """Convert a mined pattern dict into a NetworkX graph."""
        g = nx.Graph()
        for vid, vlb in pattern.get("vertices", []):
            g.add_node(vid, label=vlb)
        for frm, to, elb in pattern.get("edges", []):
            g.add_edge(frm, to, label=elb)
        return g

    # ----- mining -----

    def run_from_graphs(self, graphs: List[nx.Graph]) -> pd.DataFrame:
        """Mine frequent subgraphs from *graphs* and return a DataFrame."""
        patterns = self._gbolt.mine_frequent_subgraphs(graphs)

        data = []
        for pattern in patterns:
            num_vertices = len(pattern["vertices"])
            if pattern["support"] < self.min_support:
                continue
            if num_vertices < self.min_num_vertices:
                continue
            if num_vertices > self.max_num_vertices:
                continue

            data.append(
                {
                    "support": pattern["support"],
                    "description": self._pattern_to_description(pattern),
                    "num_vert": num_vertices,
                    "pattern_id": pattern["pattern_id"],
                    "vertices": pattern["vertices"],
                    "edges": pattern["edges"],
                }
            )

        return pd.DataFrame(
            data,
            columns=[
                "support",
                "description",
                "num_vert",
                "pattern_id",
                "vertices",
                "edges",
            ],
        )

    def run_from_file(self, filepath: str) -> pd.DataFrame:
        """Read graphs from a gSpan-format file and mine frequent subgraphs."""
        graphs = _read_gspan_file(filepath)
        return self.run_from_graphs(graphs)


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
