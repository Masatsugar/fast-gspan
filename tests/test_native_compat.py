"""CLI and checkpoint contracts captured from the C++ backend at 16017f8."""

import json
import os
from pathlib import Path
import re
import subprocess

import pandas as pd
import pytest

from fast_gspan import FastgSpan
from fast_gspan.gbolt_wrapper import GBoltWrapper, _find_vendor_gbolt

FIXTURES = Path(__file__).with_name("fixtures")
DATABASE = FIXTURES / "triangle_path.gspan"


def test_resume_cpp_checkpoint():
    """Resume two distinct C++ projection-limited branches in the Rust miner."""
    checkpoint = json.loads((FIXTURES / "cpp_0_1_5_checkpoint.json").read_text())
    previous = pd.DataFrame(checkpoint["records"])
    previous.attrs.update(checkpoint["attrs"])
    for col in ("vertices", "edges"):
        previous[col] = previous[col].map(lambda values: list(map(tuple, values)))

    miner = FastgSpan(min_support=1, num_threads=2)
    resumed = miner.resume_from_file(str(DATABASE), previous)
    expected = miner.run_from_file(str(DATABASE))
    assert resumed.stop_reason.isna().all()
    assert len(resumed) == 5
    triangle = resumed.loc[resumed.edges.map(len) == 3].iloc[0]
    assert triangle.support == 1 and triangle.graph_ids == [0]
    cols = ["description", "vertices", "edges", "support", "graph_ids", "num_vert"]
    pd.testing.assert_frame_equal(
        resumed[cols].sort_values("description").reset_index(drop=True),
        expected[cols].sort_values("description").reset_index(drop=True),
        check_dtype=False,
    )
    assert resumed.attrs == previous.attrs


@pytest.fixture
def binary():
    path = _find_vendor_gbolt()
    assert path, "build the bundled native executable with python -m fast_gspan build"
    return path


def test_cli_output_with_parents_nodes_and_custom_delimiter(binary, tmp_path):
    database = tmp_path / "input.csv"
    database.write_text(DATABASE.read_text().replace(" ", ","))
    prefix = tmp_path / "output"
    subprocess.run(
        [
            binary, "--input", str(database), "--output", str(prefix),
            "--support", "0.5", "--max-vertices", "3", "--parents",
            "--dfs", "--nodes", "--mark", ",",
        ],
        check=True, capture_output=True, env={**os.environ, "OMP_NUM_THREADS": "2"},
    )
    nodes = GBoltWrapper._parse_gbolt_thread_output((tmp_path / "output.nodes").read_text())
    assert {
        (tuple(p["vertices"]), p["support"], tuple(p["graph_occurrences"])) for p in nodes
    } == {(((0, 1),), 2, (0, 1)), (((0, 2),), 2, (0, 1))}

    patterns = {}
    parents = {}
    for shard in sorted(tmp_path.glob("output.t*")):
        worker = int(shard.suffix[2:])
        for block in re.split(r"(?=^t # )", shard.read_text(), flags=re.MULTILINE):
            if not block.strip():
                continue
            p, = GBoltWrapper._parse_gbolt_thread_output(block)
            key = worker, p["pattern_id"]
            assert key not in patterns
            patterns[key] = p["dfs_codes"]
            parent = re.search(r"^parent : (-?\d+)(?: thread : (\d+))?$", block, re.MULTILINE)
            assert parent, block
            if parent[1] != "-1":
                parents[key] = int(parent[2]), int(parent[1])
    assert len(patterns) == 5
    assert len(parents) == 3
    for child, parent in parents.items():
        assert patterns[parent] == patterns[child][:-1]


def test_cli_without_dfs_outputs_headers_only(binary, tmp_path):
    subprocess.run(
        [binary, "-i", str(DATABASE), "-o", str(tmp_path / "output"), "-s", "0.5"],
        check=True, capture_output=True, env={**os.environ, "OMP_NUM_THREADS": "1"},
    )
    lines = (tmp_path / "output.t0").read_text().splitlines()
    assert len(lines) == 5
    assert all(re.fullmatch(r"t # \d+ \* [12]", line) for line in lines)


def test_hard_timeout_raises_without_returning_partial_results(binary):
    # A near-zero subprocess timeout deterministically expires before native
    # output can be read, unlike an orderly --time-limit.
    miner = FastgSpan(gbolt_path=binary, min_support=1, timeout=1e-9, num_threads=1)
    with pytest.raises(RuntimeError, match="execution timed out"):
        miner.run_from_file(str(DATABASE))
