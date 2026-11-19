"""Compare two gbolt executables on fixed databases. Requires GNU time on Linux."""

import argparse
from dataclasses import asdict, dataclass
import hashlib
from itertools import combinations
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
import warnings

import networkx as nx

from fast_gspan import FastgSpan
from fast_gspan.gbolt_wrapper import GBoltWrapper, _relative_support


@dataclass(frozen=True)
class Workload:
    graphs: int
    vertices: int
    edges: int
    min_support: int
    max_vertices: int
    uniform_clique: bool = False


WORKLOADS = {
    "sparse": Workload(500, 30, 35, 25, 5),
    "dense": Workload(200, 30, 90, 20, 6),
    "k6": Workload(4500, 6, 15, 4500, 3, uniform_clique=True),
}
SEED = 42


def make_graphs(workload):
    if workload.uniform_clique:
        clique = nx.complete_graph(workload.vertices)
        nx.set_node_attributes(clique, 1, "label")
        nx.set_edge_attributes(clique, 1, "label")
        return [clique] * workload.graphs

    rng = random.Random(SEED)
    pairs = list(combinations(range(workload.vertices), 2))
    graphs = []
    for _ in range(workload.graphs):
        graph = nx.Graph()
        for node in range(workload.vertices):
            graph.add_node(node, label=rng.randrange(2))
        for u, v in rng.sample(pairs, workload.edges):
            graph.add_edge(u, v, label=rng.randrange(2))
        graphs.append(graph)
    return graphs


def fingerprint(records):
    """Match complete canonical codes, supports and graph ID sets across runs."""
    by_code = {}
    for description, support, ids, stop_reason in records:
        if stop_reason or len(set(ids)) != support or len(ids) != support:
            raise ValueError(f"Incomplete or invalid result for {description}")
        if description in by_code:
            raise ValueError(f"Duplicate DFS code: {description}")
        payload = json.dumps([description, support, sorted(ids)], separators=(",", ":"))
        by_code[description] = hashlib.sha256(payload.encode()).digest()
    if not by_code:
        raise ValueError("Expected a nonempty result")
    digest = hashlib.sha256(b"".join(by_code[k] for k in sorted(by_code))).hexdigest()
    return len(by_code), digest


def cli_records(directory):
    for path in directory.glob("output.t*"):
        for row in GBoltWrapper._parse_gbolt_thread_output(path.read_text()):
            yield (
                FastgSpan._pattern_to_description(row), row["support"],
                row["graph_occurrences"], row["stop_reason"],
            )


def run_cli(binary, workload, input_path, threads):
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        command = [
            binary, "-i", input_path, "-o", directory / "output", "-s",
            str(_relative_support(workload.min_support, workload.graphs)),
            "-x", str(workload.max_vertices), "-d",
        ]
        env = {**os.environ, "OMP_NUM_THREADS": str(threads)}
        peak_file = directory / "rss_kib"
        # Running GNU time as an intermediate process excludes Python's pre-exec
        # memory footprint from the native child's peak RSS on Linux.
        timed = ["/usr/bin/time", "-f", "%M", "-o", peak_file, *command]
        start = time.perf_counter()
        subprocess.run(timed, env=env, stdout=subprocess.DEVNULL, check=True)
        elapsed = time.perf_counter() - start
        rss_mib = int(peak_file.read_text()) / 1024
        return elapsed, rss_mib, fingerprint(cli_records(directory))


def run_api(miner, graphs):
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        start = time.perf_counter()
        df = miner.run_from_graphs(graphs)
        elapsed = time.perf_counter() - start
    records = ((r.description, r.support, r.graph_ids, r.stop_reason) for r in df.itertuples())
    return elapsed, None, fingerprint(records)


def summarize(samples):
    times = [s["seconds"] for s in samples]
    summary = {"median_s": statistics.median(times)}
    if len(times) > 1:
        q1, _, q3 = statistics.quantiles(times, n=4, method="inclusive")
        summary.update(q1_s=q1, q3_s=q3)
    peaks = [s["peak_rss_mib"] for s in samples if s["peak_rss_mib"] is not None]
    if peaks:
        summary["median_peak_rss_mib"] = statistics.median(peaks)
    return summary


def compare(args):
    reference = "cpp" if args.cpp else "reference_rust"
    binaries = {
        reference: (args.cpp or args.reference_rust).resolve(strict=True),
        "rust": args.rust.resolve(strict=True),
    }
    if args.previous_rust:
        binaries["previous_rust"] = args.previous_rust.resolve(strict=True)
    report = {
        "system": platform.platform(), "python": platform.python_version(),
        "networkx": nx.__version__, "seed": SEED, "repetitions": args.repetitions,
        "binaries": {
            k: {"name": v.name, "sha256": hashlib.sha256(v.read_bytes()).hexdigest()}
            for k, v in binaries.items()
        },
        "cases": [],
    }
    for name in args.workloads:
        workload = WORKLOADS[name]
        graphs = make_graphs(workload)
        expected = None
        with tempfile.TemporaryDirectory() as temp:
            input_path = Path(temp) / "input.txt"
            writer = object.__new__(GBoltWrapper)
            writer._graphs_to_gbolt_file(graphs, input_path)
            input_sha = hashlib.sha256(input_path.read_bytes()).hexdigest()
            for threads in args.threads:
                miners = {
                    key: FastgSpan(
                        gbolt_path=str(binary), num_threads=threads,
                        min_support=workload.min_support, max_num_vertices=workload.max_vertices,
                    ) for key, binary in binaries.items()
                }
                for interface in args.interfaces:
                    measure_previous = "previous_rust" in binaries and name == "dense" and interface == "cli"
                    samples = {reference: [], "rust": []}
                    if measure_previous:
                        samples["previous_rust"] = []
                    # Alternate the C++/Rust pair. When comparing memory use
                    # before and after the optimization, rotate the earlier
                    # executable's position around that pair.
                    for i in range(-1, args.repetitions):
                        order = [reference, "rust"]
                        if i >= 0 and i % 2:
                            order.reverse()
                        if measure_previous:
                            index = 2 if i < 0 else (2 - i) % 3
                            order.insert(index, "previous_rust")
                        for key in order:
                            if interface == "cli":
                                result = run_cli(binaries[key], workload, input_path, threads)
                            else:
                                result = run_api(miners[key], graphs)
                            seconds, rss, signature = result
                            if expected is None:
                                expected = signature
                            if expected != signature:
                                raise ValueError(f"{name} {threads}T {interface} {key}: {signature} != {expected}")
                            print(f"{name} {threads}T {interface} {key} run={i} {seconds:.3f}s", file=sys.stderr, flush=True)
                            if i >= 0:
                                samples[key].append({"seconds": seconds, "peak_rss_mib": rss})
                    summary = {key: summarize(runs) for key, runs in samples.items()}
                    case = {
                        "workload": name, "parameters": asdict(workload), "input_sha256": input_sha,
                        "threads": threads, "interface": interface, "patterns": expected[0],
                        "result_sha256": expected[1], "samples": samples, "summary": summary,
                        "speedup": summary[reference]["median_s"] / summary["rust"]["median_s"],
                    }
                    report["cases"].append(case)
                    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--cpp", type=Path, help="C++ reference executable")
    baseline.add_argument("--reference-rust", type=Path, help="baseline executable for a Rust optimization trial")
    parser.add_argument("--rust", type=Path, required=True)
    parser.add_argument("--previous-rust", type=Path, help="include an earlier Rust executable in dense CLI comparisons")
    parser.add_argument("--output", type=Path, required=True, help="write raw samples and summaries to JSON")
    parser.add_argument("--workloads", nargs="+", choices=WORKLOADS, default=list(WORKLOADS))
    parser.add_argument("--threads", nargs="+", type=int, default=[1, 8])
    parser.add_argument("--interfaces", nargs="+", choices=["cli", "api"], default=["cli", "api"])
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    if min(args.threads) <= 0 or args.repetitions <= 0:
        parser.error("threads and repetitions must be positive")
    compare(args)


if __name__ == "__main__":
    main()
