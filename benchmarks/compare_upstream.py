"""Benchmark the original gBolt, the project C++ fork, and the Rust port on Linux."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

from benchmarks.compare import fingerprint, summarize


LIMITS = (5, 8)
SUPPORTS = ("0.10", "0.08")


def records(paths, bound):
    """Reconstruct full DFS tuples from vertex labels in every output dialect."""
    for path in paths:
        labels = {}
        code = []
        support = None
        with path.open("rb") as stream:
            for line in stream:
                fields = line.split()
                if not fields:
                    continue
                if fields[0] == b"t":
                    if support is not None:
                        raise ValueError(f"missing occurrences before {line!r}")
                    support = int(fields[4])
                    labels = {}
                    code = []
                elif fields[0] == b"v":
                    labels[int(fields[1])] = int(fields[2])
                elif fields[0] == b"e":
                    edge = tuple(map(int, fields[1:]))
                    if len(edge) == 3:
                        u, v, label = edge
                    elif len(edge) == 5:
                        u, v, _, label, _ = edge
                    else:
                        raise ValueError(f"invalid edge: {line!r}")
                    step = (u, v, labels[u], label, labels[v])
                    if len(edge) == 5 and step != edge:
                        raise ValueError(f"edge conflicts with vertex labels: {edge}")
                    code.append(str(step))
                elif fields[0] == b"x:" and support is not None and code:
                    if bound and len(labels) > bound:
                        raise ValueError(f"output exceeds {bound} vertices")
                    yield "\n".join(code), support, list(map(int, fields[1:])), None
                    support = None
                else:
                    raise ValueError(f"unexpected or partial output: {line!r}")
            if support is not None:
                raise ValueError(f"truncated result in {path}")


def write_bounded(paths, directory, limit):
    """Copy small patterns in the original native format, buffering one at a time."""
    directory.mkdir()
    for path in paths:
        with path.open("rb") as src, (directory / path.name).open("wb") as dest:
            block = []
            vertices = 0
            for line in src:
                if line.startswith(b"t #"):
                    if vertices <= limit:
                        dest.writelines(block)
                    block = []
                    vertices = 0
                if line.startswith(b"v "):
                    vertices += 1
                block.append(line)
            if vertices <= limit:
                dest.writelines(block)


def measure(binary, database, support, threads, bound, postfilter_limits):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        prefix = root / "output"
        rss_path = root / "rss_kib"
        command = [
            "/usr/bin/time", "-f", "%M", "-o", rss_path, binary,
            "-i", database, "-s", support, "-o", prefix, "-d",
        ]
        if bound:
            command.extend(["-x", str(bound)])
        start = time.perf_counter()
        subprocess.run(command, env={**os.environ, "OMP_NUM_THREADS": str(threads)},
                       stdout=subprocess.DEVNULL, check=True)
        native_time = time.perf_counter() - start
        native_rss = int(rss_path.read_text()) / 1024
        paths = sorted(root.glob("output.t*"))

        outputs = {(support, bound): (paths, 0.0)}
        for limit in postfilter_limits:
            kept = root / f"retained-{limit}"
            start = time.perf_counter()
            write_bounded(paths, kept, limit)
            filter_time = time.perf_counter() - start
            outputs[support, limit] = (sorted(kept.iterdir()), filter_time)
        results = {}
        for key, (files, filter_seconds) in outputs.items():
            pattern_count, digest = fingerprint(records(files, key[1]))
            results[key] = {
                "patterns": pattern_count, "sha256": digest,
                "native_seconds": native_time, "postfilter_seconds": filter_seconds,
                "seconds": native_time + filter_seconds, "peak_rss_mib": native_rss,
                "result_bytes": sum(path.stat().st_size for path in files),
            }
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("upstream", "cpp", "rust", "database", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    binaries = {name: getattr(args, name).resolve(strict=True) for name in ("upstream", "cpp", "rust")}
    data = args.database.read_bytes()
    headers = [line for line in data.splitlines() if line.startswith(b"t # ")]
    graph_count = sum(line.split()[2] != b"-1" for line in headers)
    if not graph_count:
        parser.error("--database must contain gSpan transactions")
    report = {
        "database": args.database.name, "input_sha256": hashlib.sha256(data).hexdigest(),
        "graphs": graph_count, "system": platform.platform(), "python": platform.python_version(),
        "binaries": {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in binaries.items()},
        "repetitions": 5, "cases": [],
    }

    expected = {}
    for support in SUPPORTS:
        operations = [(engine, 0) for engine in binaries]
        bounds = [0]
        if support == "0.10":
            operations.extend((engine, limit) for limit in LIMITS for engine in ("cpp", "rust"))
            bounds.extend(LIMITS)
        for threads in (1, 8):
            samples = {bound: {engine: [] for engine in binaries} for bound in bounds}
            for repeat in range(-1, report["repetitions"]):
                # The first lap is a warm-up. Rotate which operation runs first.
                offset = max(0, repeat) % len(operations)
                for engine, bound in operations[offset:] + operations[:offset]:
                    filtered = bounds[1:] if engine == "upstream" else []
                    rows = measure(binaries[engine], args.database, support, threads, bound, filtered)
                    for key, row in rows.items():
                        signature = (row.pop("patterns"), row.pop("sha256"))
                        if expected.setdefault(key, signature) != signature:
                            raise ValueError(f"{engine}, {threads} threads, {key}: {signature} != {expected[key]}")
                        if repeat >= 0:
                            samples[key[1]][engine].append(row)
                print(f"support={support} threads={threads} round={repeat} verified", file=sys.stderr, flush=True)
            for bound, runs in samples.items():
                report["cases"].append({
                    "relative_support": support, "min_support": int(float(support) * len(headers)),
                    "threads": threads, "max_vertices": bound, "patterns": expected[support, bound][0],
                    "result_sha256": expected[support, bound][1], "samples": runs,
                    "summary": {engine: summarize(rows) for engine, rows in runs.items()},
                })
            args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
