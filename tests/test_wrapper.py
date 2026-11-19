"""Tests for fast_gspan standalone package."""

import json
import os
import random
import types
import warnings
from decimal import Decimal
from fractions import Fraction

import networkx as nx
import numpy as np
import pandas as pd
import pytest

from fast_gspan.gbolt_wrapper import (
    FastgSpan,
    GBoltWrapper,
    IncompleteResultWarning,
    ProgressMonitor,
    _effective_min_support,
    _find_vendor_gbolt,
    _read_gspan_file,
    _relative_support,
)


def _sample_graphs() -> list[nx.Graph]:
    """Create sample graphs for testing."""
    graphs = []

    # Triangle
    g1 = nx.Graph()
    g1.add_node(0, label=1)
    g1.add_node(1, label=1)
    g1.add_node(2, label=2)
    g1.add_edge(0, 1, label=1)
    g1.add_edge(1, 2, label=1)
    g1.add_edge(0, 2, label=1)
    graphs.append(g1)

    # Square
    g2 = nx.Graph()
    g2.add_node(0, label=1)
    g2.add_node(1, label=1)
    g2.add_node(2, label=2)
    g2.add_node(3, label=2)
    g2.add_edge(0, 1, label=1)
    g2.add_edge(1, 2, label=1)
    g2.add_edge(2, 3, label=1)
    g2.add_edge(3, 0, label=1)
    graphs.append(g2)

    # Path
    g3 = nx.Graph()
    g3.add_node(0, label=1)
    g3.add_node(1, label=1)
    g3.add_node(2, label=1)
    g3.add_edge(0, 1, label=1)
    g3.add_edge(1, 2, label=1)
    graphs.append(g3)

    return graphs


def _duplicate_graphs(n: int = 10) -> list[nx.Graph]:
    """Create *n* copies of each sample graph so min_support thresholds work."""
    base = _sample_graphs()
    return base * n


def _clique(n: int) -> nx.Graph:
    """Complete graph on *n* vertices with uniform labels (many embeddings)."""
    g = nx.complete_graph(n)
    nx.set_node_attributes(g, 1, "label")
    nx.set_edge_attributes(g, 1, "label")
    return g


def _edge_graph(label: int) -> nx.Graph:
    """A single edge between two vertices labelled *label*."""
    g = nx.Graph()
    g.add_node(0, label=label)
    g.add_node(1, label=label)
    g.add_edge(0, 1, label=1)
    return g


def _miner_without_gbolt(min_support) -> FastgSpan:
    """A FastgSpan that needs no gBolt binary, as long as nothing is mined."""
    fgs = object.__new__(FastgSpan)
    fgs._gbolt = types.SimpleNamespace(min_support=min_support)
    return fgs


def _rare_edge_graphs() -> list[nx.Graph]:
    """300 graphs: one edge found in all, one in 2 and one in 1 of them."""
    common = _edge_graph(1)
    return (
        [common] * 297
        + [nx.disjoint_union(common, _edge_graph(2))] * 2
        + [nx.disjoint_union(common, _edge_graph(3))]
    )


def _random_graphs(
    seed: int,
    n_graphs: int = 12,
    n_nodes: int = 6,
    n_edges: int = 7,
    n_vertex_labels: int = 2,
    n_edge_labels: int = 2,
) -> list[nx.Graph]:
    """Create small random labelled graphs (deterministic for a given seed)."""
    rng = random.Random(seed)
    pairs = [(i, j) for i in range(n_nodes) for j in range(i + 1, n_nodes)]
    graphs = []
    for _ in range(n_graphs):
        g = nx.Graph()
        for v in range(n_nodes):
            g.add_node(v, label=rng.randrange(n_vertex_labels))
        for u, v in rng.sample(pairs, n_edges):
            g.add_edge(u, v, label=rng.randrange(n_edge_labels))
        graphs.append(g)
    return graphs


def _same_label(a: dict, b: dict) -> bool:
    return a["label"] == b["label"]


def _isomorphic(a: nx.Graph, b: nx.Graph) -> bool:
    return (
        a.number_of_nodes() == b.number_of_nodes()
        and a.number_of_edges() == b.number_of_edges()
        and nx.is_isomorphic(a, b, node_match=_same_label, edge_match=_same_label)
    )


def _brute_force_frequent(
    graphs: list[nx.Graph], min_support: int, max_vertices: int
) -> list[tuple[nx.Graph, int]]:
    """Reference miner, independent of gBolt.

    Enumerates every connected subgraph (as an edge subset) of every graph,
    groups them by isomorphism and counts the graphs each group occurs in.
    """
    classes: list[tuple[nx.Graph, set[int]]] = []
    for gid, g in enumerate(graphs):
        seen = set()
        frontier = [frozenset([frozenset(e)]) for e in g.edges()]
        while frontier:
            edges = frontier.pop()
            if edges in seen:
                continue
            seen.add(edges)
            nodes = set().union(*edges)
            if max_vertices and len(nodes) > max_vertices:
                continue
            sub = nx.Graph(g.edge_subgraph([tuple(e) for e in edges]))
            for rep, ids in classes:
                if _isomorphic(rep, sub):
                    ids.add(gid)
                    break
            else:
                classes.append((sub, {gid}))
            for e in map(frozenset, g.edges(nodes)):
                if e not in edges:
                    frontier.append(edges | {e})
    return [(rep, len(ids)) for rep, ids in classes if len(ids) >= min_support]


def _results(df: pd.DataFrame) -> dict:
    """Map each pattern (its DFS code text) to its support and graph ids."""
    assert df["description"].is_unique
    return {
        row.description: (row.support, tuple(row.graph_ids))
        for row in df.itertuples()
    }


# ---- unit tests (no gBolt binary required) ----

def test_progress_counts_append_only_shards_across_partial_writes(tmp_path):
    shard = tmp_path / "patterns.t0"
    monitor = ProgressMonitor(str(tmp_path / "patterns"))
    shard.write_bytes(b"t # 0 * 1\nx: 0\nt ")
    assert monitor._count_patterns_in_file(str(shard)) == 1
    with shard.open("ab") as output:
        output.write(b"# 1 * 1\nx: 0\n")
    assert monitor._count_patterns_in_file(str(shard)) == 2
    assert monitor._count_patterns_in_file(str(shard)) == 2


class TestParseOutput:
    """Test gBolt output parsing without requiring the binary."""

    def test_parse_extended_format(self):
        text = (
            "t # 0 * 5\n"
            "v 0 1\n"
            "v 1 2\n"
            "e 0 1 1 3 2\n"
            "x: 0 1 2 3 4\n"
        )
        patterns = GBoltWrapper._parse_gbolt_thread_output(text)
        assert len(patterns) == 1
        p = patterns[0]
        assert p["support"] == 5
        assert p["vertices"] == [(0, 1), (1, 2)]
        assert p["edges"] == [(0, 1, 3)]
        assert p["dfs_codes"] == [(0, 1, 1, 3, 2)]
        assert p["graph_occurrences"] == [0, 1, 2, 3, 4]

    def test_parse_legacy_format(self):
        text = (
            "t # 0 * 3\n"
            "v 0 1\n"
            "v 1 1\n"
            "e 0 1 5\n"
        )
        patterns = GBoltWrapper._parse_gbolt_thread_output(text)
        assert len(patterns) == 1
        assert patterns[0]["edges"] == [(0, 1, 5)]
        assert "dfs_codes" not in patterns[0]

    def test_parse_multiple_patterns(self):
        text = (
            "t # 0 * 10\nv 0 1\nv 1 2\ne 0 1 1 3 2\n"
            "t # 1 * 7\nv 0 2\nv 1 2\ne 0 1 2 4 2\n"
        )
        patterns = GBoltWrapper._parse_gbolt_thread_output(text)
        assert len(patterns) == 2
        assert patterns[0]["support"] == 10
        assert patterns[1]["support"] == 7

    def test_dedup(self):
        p1 = {"vertices": [(0, 1), (1, 2)], "edges": [(0, 1, 3)], "support": 5}
        p2 = {"vertices": [(0, 1), (1, 2)], "edges": [(0, 1, 3)], "support": 8}
        result = GBoltWrapper._deduplicate_patterns([p1, p2])
        assert len(result) == 1
        assert result[0]["support"] == 8


class TestStopReasonParsing:
    """Test detection of the patterns gBolt reported but did not extend."""

    def test_parse_stop_reason(self):
        text = (
            "t # 0 * 5\n"
            "u: time_limit\n"
            "v 0 1\nv 1 2\ne 0 1 1 3 2\nx: 0 1 2 3 4\n"
            "t # 1 * 4\n"
            "v 0 1\nv 1 1\ne 0 1 1 3 1\nx: 0 1 2 3\n"
        )
        patterns = GBoltWrapper._parse_gbolt_thread_output(text)
        assert [p["stop_reason"] for p in patterns] == ["time_limit", None]

    def test_row_dfs_code(self):
        row = {"vertices": [(0, 1), (1, 2), (2, 1)], "edges": [(0, 1, 3), (1, 2, 4)]}
        assert FastgSpan._row_dfs_code(row) == [(0, 1, 1, 3, 2), (1, 2, 2, 4, 1)]


class TestGraphConversion:
    def test_roundtrip(self, tmp_path):
        graphs = _sample_graphs()
        filepath = str(tmp_path / "graphs.txt")
        wrapper = object.__new__(GBoltWrapper)  # avoid __init__ (needs binary)
        wrapper._graphs_to_gbolt_file(graphs, filepath)

        loaded = _read_gspan_file(filepath)
        assert len(loaded) == len(graphs)
        for orig, loaded_g in zip(graphs, loaded):
            assert loaded_g.number_of_nodes() == orig.number_of_nodes()
            assert loaded_g.number_of_edges() == orig.number_of_edges()


class TestPatternDescription:
    def test_with_dfs_codes(self):
        pattern = {
            "dfs_codes": [(0, 1, 1, 3, 2), (1, 2, 2, 4, 1)],
            "edges": [(0, 1, 3), (1, 2, 4)],
        }
        desc = FastgSpan._pattern_to_description(pattern)
        assert "(0, 1, 1, 3, 2)" in desc
        assert "(1, 2, 2, 4, 1)" in desc

    def test_without_dfs_codes(self):
        pattern = {"edges": [(0, 1, 3)]}
        desc = FastgSpan._pattern_to_description(pattern)
        assert "(0, 1, 3)" in desc


class TestPatternToGraph:
    def test_converts_to_networkx(self):
        pattern = {
            "vertices": [(0, 1), (1, 2), (2, 3)],
            "edges": [(0, 1, 5), (1, 2, 6)],
        }
        g = FastgSpan.pattern_to_graph(pattern)
        assert isinstance(g, nx.Graph)
        assert g.number_of_nodes() == 3
        assert g.number_of_edges() == 2
        assert g.nodes[0]["label"] == 1
        assert g[0][1]["label"] == 5


class TestEmptyInput:
    def test_empty_graphs_wrapper(self):
        """GBoltWrapper.mine_frequent_subgraphs returns [] for empty input."""
        wrapper = object.__new__(GBoltWrapper)
        wrapper._gbolt_path = "/nonexistent"
        wrapper.min_support = 2
        wrapper.max_vertices = 0
        wrapper.num_threads = 0
        wrapper.timeout = None
        wrapper.show_progress = False
        wrapper.verbose = False
        assert wrapper.mine_frequent_subgraphs([]) == []

    def test_empty_graphs_fastgspan(self):
        """FastgSpan.run_from_graphs returns empty DataFrame for empty input."""
        fgs = _miner_without_gbolt(min_support=2)
        fgs.min_num_vertices = 1
        fgs.max_num_vertices = 0
        df = fgs.run_from_graphs([])
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0
        assert "graph_ids" in df.columns


class TestRelativeSupport:
    @staticmethod
    def _gbolt_threshold(min_support: int, n_graphs: int) -> int:
        """The threshold gBolt derives from the -s value passed to it."""
        s = float(str(_relative_support(min_support, n_graphs)))
        assert 0.0 < s <= 1.0
        # gBolt: nsupport_ = graphs.size() * support_, the size counting "t # -1"
        return int((n_graphs + 1) * s)

    def test_exact_for_every_small_database(self):
        for n_graphs in range(1, 301):
            for min_support in range(1, n_graphs + 1):
                assert self._gbolt_threshold(min_support, n_graphs) == min_support

    @pytest.mark.parametrize("n_graphs", [249_456, 10**7, 2**31 - 2])
    def test_exact_for_large_databases(self, n_graphs):
        for min_support in (1, 2, 3, 100, n_graphs // 100 - 1, n_graphs - 1, n_graphs):
            assert self._gbolt_threshold(min_support, n_graphs) == min_support


class TestMinSupportRecord:
    @pytest.mark.parametrize(
        "min_support, expected",
        [
            (3, 3),
            (0, 1),
            (-5, 1),
            (2.5, 3),
            (np.int64(4), 4),
            (np.float16(2.5), 3),
            # Not exactly representable as a float
            (np.int64(2**53 + 3), 2**53 + 3),
            (Fraction(5, 2), 3),
            (Decimal("2.5"), 3),
        ],
    )
    def test_effective_min_support(self, min_support, expected):
        threshold = _effective_min_support(min_support)
        assert threshold == expected
        assert type(threshold) is int

    @pytest.mark.skipif(
        np.finfo(np.longdouble).eps >= np.finfo(np.float64).eps,
        reason="np.longdouble is no more precise than float here",
    )
    def test_effective_min_support_keeps_extended_precision(self):
        just_above_two = np.nextafter(np.longdouble(2), np.longdouble(3))
        assert float(just_above_two) == 2.0
        assert _effective_min_support(just_above_two) == 3
        # float() would round this up to 2**53 + 4
        assert _effective_min_support(np.longdouble(2**53 + 3)) == 2**53 + 3
        # NumPy 1 cannot compare this with a Python int
        assert _effective_min_support(np.longdouble("1e20")) == 10**20

    def test_rejects_previous_without_record(self):
        with pytest.raises(ValueError, match="0.1.4 and earlier"):
            FastgSpan._check_min_support(pd.DataFrame(), 1)

    def test_compares_effective_thresholds(self):
        previous = pd.DataFrame()
        previous.attrs["fast_gspan"] = {"min_support": 1}
        FastgSpan._check_min_support(previous, 1)
        with pytest.raises(ValueError, match="mined with min_support=1, not 2"):
            FastgSpan._check_min_support(previous, 2)

    def test_failed_run_is_not_marked(self, tmp_path):
        """Its empty result must not pass for a complete one when resumed."""
        failing = tmp_path / "gbolt"
        failing.write_text("#!/bin/sh\nexit 1\n")
        failing.chmod(0o755)
        fgs = FastgSpan(gbolt_path=str(failing), min_support=2)
        with pytest.warns(RuntimeWarning, match="exited with code 1"):
            df = fgs.run_from_graphs(_sample_graphs())
        assert df.empty
        assert df.attrs == {}
        with pytest.raises(ValueError, match="failed runs"):
            fgs.resume_from_graphs(_sample_graphs(), df)

    def test_records_plain_int(self):
        """NumPy scalars in attrs would break DataFrame.to_parquet()."""
        fgs = _miner_without_gbolt(min_support=np.int64(3))
        fgs.min_num_vertices = 1
        fgs.max_num_vertices = 0
        df = fgs.run_from_graphs([])
        assert type(df.attrs["fast_gspan"]["min_support"]) is int
        json.dumps(df.attrs)


# ---- integration tests (require gBolt binary) ----


def _gbolt_available() -> bool:
    if _find_vendor_gbolt():
        return True
    import os

    for p in ["./gBolt/build/gbolt", "../gBolt/build/gbolt"]:
        if os.path.exists(p):
            return True
    return False


@pytest.mark.skipif(not _gbolt_available(), reason="gBolt binary not built")
class TestIntegration:
    def test_mine_returns_dataframe(self):
        graphs = _duplicate_graphs(10)
        fgs = FastgSpan(min_support=2, max_num_vertices=5, verbose=False)
        df = fgs.run_from_graphs(graphs)
        assert isinstance(df, pd.DataFrame)
        assert "support" in df.columns
        assert "description" in df.columns
        assert "graph_ids" in df.columns
        assert len(df) > 0

    def test_unlimited_vertices(self):
        graphs = _duplicate_graphs(10)
        df = FastgSpan(min_support=2).run_from_graphs(graphs)
        assert len(df) > 0

    def test_max_vertices_filtering(self):
        graphs = _duplicate_graphs(10)
        df_small = FastgSpan(min_support=2, max_num_vertices=2).run_from_graphs(graphs)
        df_large = FastgSpan(min_support=2, max_num_vertices=5).run_from_graphs(graphs)
        assert len(df_small) <= len(df_large)

    def test_timeout_parameter(self):
        graphs = _duplicate_graphs(10)
        fgs = FastgSpan(min_support=2, max_num_vertices=5, timeout=60.0)
        df = fgs.run_from_graphs(graphs)
        assert len(df) > 0

    def test_run_from_file(self, tmp_path):
        graphs = _duplicate_graphs(10)
        filepath = str(tmp_path / "test_db.txt")
        with open(filepath, "w") as f:
            for i, g in enumerate(graphs):
                f.write(f"t # {i}\n")
                for n in sorted(g.nodes()):
                    f.write(f"v {n} {g.nodes[n].get('label', 0)}\n")
                for u, v in g.edges():
                    f.write(f"e {u} {v} {g[u][v].get('label', 0)}\n")
            f.write("t # -1\n")

        df = FastgSpan(min_support=2, max_num_vertices=5).run_from_file(filepath)
        assert len(df) > 0

    def test_pattern_to_graph_integration(self):
        graphs = _duplicate_graphs(10)
        fgs = FastgSpan(min_support=2, max_num_vertices=5)
        df = fgs.run_from_graphs(graphs)
        assert len(df) > 0
        pattern = {"vertices": df.iloc[0]["vertices"], "edges": df.iloc[0]["edges"]}
        g = FastgSpan.pattern_to_graph(pattern)
        assert isinstance(g, nx.Graph)
        assert g.number_of_nodes() == df.iloc[0]["num_vert"]

    def test_large_projection_is_mined_completely(self):
        """Patterns with more than 500,000 embeddings must not be dropped.

        Replicating a database k times and scaling min_support by k must not
        change the set of frequent patterns. Each K6 holds 6 * 5 * 4 = 120
        embeddings of the 3-vertex path, so 4,500 copies give 540,000
        embeddings, above the limit gBolt used to apply silently.
        """
        base = [_clique(6)] + _sample_graphs()
        path = "(0, 1, 1, 1, 1)\n(1, 2, 1, 1, 1)"
        triangle = path + "\n(2, 0, 1, 1, 1)"

        supports = {}
        for k in (10, 4500):
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                df = FastgSpan(min_support=k, max_num_vertices=3).run_from_graphs(
                    base * k
                )
            assert df["support"].map(lambda s, k=k: s % k == 0).all()
            supports[k] = dict(zip(df["description"], df["support"] // k))

        assert supports[4500] == supports[10]
        assert supports[4500][path] == 2
        assert supports[4500][triangle] == 1

    @pytest.mark.parametrize("min_support", [1, 2, 3, 300, 301])
    def test_min_support_is_exact_for_rare_patterns(self, min_support):
        """min_support is applied as given, even far below 1% of the graphs.

        It used to be raised to 1% of the graphs without a warning: here
        that is 3, so the edge found in 2 of the 300 graphs was dropped.
        """
        graphs = _rare_edge_graphs()
        supports = {
            "(0, 1, 1, 1, 1)": 300,
            "(0, 1, 2, 1, 2)": 2,
            "(0, 1, 3, 1, 3)": 1,
        }
        expected = {d: s for d, s in supports.items() if s >= min_support}

        df = FastgSpan(min_support=min_support).run_from_graphs(graphs)
        assert dict(zip(df["description"], df["support"])) == expected

        # gBolt itself uses the exact threshold, with no filtering afterwards
        patterns = GBoltWrapper(min_support=min_support).mine_frequent_subgraphs(
            graphs
        )
        assert sorted(p["support"] for p in patterns) == sorted(expected.values())

    def test_min_support_changed_after_construction_is_mined_with(self):
        fgs = FastgSpan(min_support=3)
        fgs.min_support = 2
        df = fgs.run_from_graphs(_rare_edge_graphs())
        assert sorted(df["support"]) == [2, 300]
        assert df.attrs["fast_gspan"] == {"min_support": 2}

    def test_projection_limit_warns_instead_of_dropping_silently(self):
        graphs = [_clique(6)] * 20  # 20 * 120 = 2,400 embeddings of the path

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            full = FastgSpan(min_support=2, max_num_vertices=3).run_from_graphs(graphs)

        with pytest.warns(IncompleteResultWarning, match="1 frequent pattern"):
            limited = FastgSpan(
                min_support=2, max_num_vertices=3, max_projection_size=100
            ).run_from_graphs(graphs)

        # The oversized path is still reported; only its extension (the
        # triangle) is lost.
        edge = "(0, 1, 1, 1, 1)"
        path = edge + "\n(1, 2, 1, 1, 1)"
        triangle = path + "\n(2, 0, 1, 1, 1)"
        assert set(full["description"]) == {edge, path, triangle}
        assert full["stop_reason"].isna().all()
        assert set(limited["description"]) == {edge, path}
        assert limited["support"].tolist() == [20, 20]
        stop_reasons = dict(zip(limited["description"], limited["stop_reason"].fillna("")))
        assert stop_reasons == {edge: "", path: "max_projection_size"}

    def test_projection_limit_can_be_escalated_to_error(self):
        graphs = [_clique(6)] * 20
        fgs = FastgSpan(min_support=2, max_num_vertices=3, max_projection_size=100)
        with warnings.catch_warnings():
            warnings.simplefilter("error", IncompleteResultWarning)
            with pytest.raises(IncompleteResultWarning):
                fgs.run_from_graphs(graphs)

    @pytest.mark.parametrize("max_vertices", [0, 3, 4])
    @pytest.mark.parametrize(
        "graph_options",
        [
            {"seed": 0},
            {"seed": 1},
            # Uniform labels: many automorphisms and duplicate DFS codes
            {"seed": 2, "n_vertex_labels": 1, "n_edge_labels": 1},
        ],
    )
    def test_matches_brute_force_reference(self, graph_options, max_vertices):
        """The mined patterns and supports equal an independent enumeration."""
        graphs = _random_graphs(**graph_options)
        reference = _brute_force_frequent(graphs, 3, max_vertices)
        assert len(reference) >= 3  # edge, path and triangle at the very least

        df = FastgSpan(min_support=3, max_num_vertices=max_vertices).run_from_graphs(
            graphs
        )
        cpp_binary = os.environ.get("FAST_GSPAN_CPP_REFERENCE")
        if cpp_binary:
            assert os.access(cpp_binary, os.X_OK), cpp_binary
            legacy = FastgSpan(
                gbolt_path=cpp_binary, min_support=3, max_num_vertices=max_vertices
            ).run_from_graphs(graphs)
            assert _results(df) == _results(legacy)

        unmatched = list(reference)
        for row in df.itertuples():
            mined = FastgSpan.pattern_to_graph(
                {"vertices": row.vertices, "edges": row.edges}
            )
            for i, (rep, support) in enumerate(unmatched):
                if _isomorphic(rep, mined):
                    assert row.support == support
                    del unmatched[i]
                    break
            else:
                pytest.fail(f"not frequent, or mined twice: {row.description!r}")
        assert not unmatched, f"{len(unmatched)} frequent patterns were not mined"

    def test_time_limit_returns_partial_result_and_resume_completes_it(self):
        graphs = _random_graphs(seed=0)
        full = FastgSpan(min_support=3).run_from_graphs(graphs)

        # A budget this small is used up before mining starts: every
        # single-edge pattern is reported and none is extended.
        with pytest.warns(IncompleteResultWarning, match="by time_limit"):
            partial = FastgSpan(min_support=3, time_limit=1e-9).run_from_graphs(graphs)
        assert (partial["stop_reason"] == "time_limit").all()
        assert _results(partial) == _results(full[full["num_vert"] == 2])

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            resumed = FastgSpan(min_support=3).resume_from_graphs(graphs, partial)
        assert resumed["stop_reason"].isna().all()
        assert _results(resumed) == _results(full)
        # The input is left untouched
        assert (partial["stop_reason"] == "time_limit").all()

    @pytest.mark.parametrize(
        "graph_options",
        [
            {"seed": 0},
            {"seed": 1},
            {"seed": 2, "n_vertex_labels": 1, "n_edge_labels": 1},
        ],
    )
    def test_resume_in_several_steps_matches_single_run(self, graph_options):
        graphs = _random_graphs(**graph_options)
        full = FastgSpan(min_support=3).run_from_graphs(graphs)

        result = None
        unextended = []
        for limit in (4, 12, 40, 0):  # 0 = no limit
            fgs = FastgSpan(min_support=3, max_projection_size=limit)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", IncompleteResultWarning)
                if result is None:
                    result = fgs.run_from_graphs(graphs)
                else:
                    result = fgs.resume_from_graphs(graphs, result)
            unextended.append(int(result["stop_reason"].notna().sum()))

        assert unextended[0] > 0  # the first step really was partial
        assert unextended[-1] == 0
        assert _results(result) == _results(full)

    def test_resume_without_progress_says_so(self):
        graphs = [_clique(6)] * 20
        fgs = FastgSpan(min_support=2, max_num_vertices=3, max_projection_size=100)
        with pytest.warns(IncompleteResultWarning):
            partial = fgs.run_from_graphs(graphs)

        # Same limit again: the oversized pattern still cannot be extended
        with pytest.warns(IncompleteResultWarning, match="extended nothing"):
            again = fgs.resume_from_graphs(graphs, partial)
        assert _results(again) == _results(partial)
        pd.testing.assert_frame_equal(again, partial)

    def test_resume_cut_short_keeps_what_is_left_to_do(self):
        """Running out of time before reaching the unextended patterns loses nothing."""
        graphs = _random_graphs(seed=0)
        full = FastgSpan(min_support=3).run_from_graphs(graphs)
        with pytest.warns(IncompleteResultWarning):
            partial = FastgSpan(min_support=3, max_projection_size=4).run_from_graphs(
                graphs
            )
        # The unextended patterns lie below patterns that were already extended
        assert (partial.loc[partial["stop_reason"].notna(), "num_vert"] > 2).all()

        with pytest.warns(IncompleteResultWarning, match="extended nothing"):
            again = FastgSpan(min_support=3, time_limit=1e-9).resume_from_graphs(
                graphs, partial
            )
        pd.testing.assert_frame_equal(again, partial)

        resumed = FastgSpan(min_support=3).resume_from_graphs(graphs, again)
        assert _results(resumed) == _results(full)

    def test_resume_keeps_small_unextended_patterns(self):
        """Rows below min_num_vertices are kept for as long as they are unextended."""
        graphs = _random_graphs(seed=1)
        full = FastgSpan(min_support=3, min_num_vertices=3).run_from_graphs(graphs)
        assert (full["num_vert"] >= 3).all()

        with pytest.warns(IncompleteResultWarning):
            partial = FastgSpan(
                min_support=3, min_num_vertices=3, time_limit=1e-9
            ).run_from_graphs(graphs)
        assert len(partial) > 0
        assert (partial["num_vert"] == 2).all()

        resumed = FastgSpan(min_support=3, min_num_vertices=3).resume_from_graphs(
            graphs, partial
        )
        assert _results(resumed) == _results(full)

    def test_resume_rejects_other_graphs(self):
        with pytest.warns(IncompleteResultWarning):
            partial = FastgSpan(min_support=3, time_limit=1e-9).run_from_graphs(
                _random_graphs(seed=0)
            )
        with pytest.raises(ValueError, match="does not match"):
            FastgSpan(min_support=3).resume_from_graphs(
                _random_graphs(seed=1), partial
            )

    def test_results_record_min_support(self):
        graphs = _random_graphs(seed=0)
        with pytest.warns(IncompleteResultWarning):
            partial = FastgSpan(min_support=3, time_limit=1e-9).run_from_graphs(graphs)
        resumed = FastgSpan(min_support=3).resume_from_graphs(graphs, partial)
        assert partial.attrs["fast_gspan"] == {"min_support": 3}
        assert resumed.attrs["fast_gspan"] == {"min_support": 3}

    def test_resume_rejects_other_min_support(self):
        """A lower min_support would silently miss the patterns in between."""
        graphs = _random_graphs(seed=0)
        with pytest.warns(IncompleteResultWarning):
            partial = FastgSpan(min_support=3, time_limit=1e-9).run_from_graphs(graphs)
        with pytest.raises(ValueError, match="mined with min_support=3, not 2"):
            FastgSpan(min_support=2).resume_from_graphs(graphs, partial)

    def test_resume_rejects_results_without_record(self):
        """Like those of 0.1.4, which mined these graphs at 3 instead of 2.

        Resuming such a result could not find the edge in 2 of the graphs,
        yet would clear every stop_reason.
        """
        graphs = _rare_edge_graphs()
        with pytest.warns(IncompleteResultWarning):
            partial = FastgSpan(min_support=2, time_limit=1e-9).run_from_graphs(graphs)
        partial.attrs.clear()
        for graphs_to_resume in (graphs, []):
            with pytest.raises(ValueError, match="0.1.4 and earlier"):
                FastgSpan(min_support=2).resume_from_graphs(graphs_to_resume, partial)

    def test_resume_without_graphs_keeps_previous(self):
        graphs = _random_graphs(seed=0)
        with pytest.warns(IncompleteResultWarning):
            partial = FastgSpan(min_support=3, time_limit=1e-9).run_from_graphs(graphs)
        unchanged = FastgSpan(min_support=3).resume_from_graphs([], partial)
        pd.testing.assert_frame_equal(unchanged, partial)
        assert unchanged.attrs == partial.attrs

    def test_resume_of_complete_result_changes_nothing(self):
        graphs = _duplicate_graphs(10)
        fgs = FastgSpan(min_support=2, max_num_vertices=5)
        full = fgs.run_from_graphs(graphs)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            again = fgs.resume_from_graphs(graphs, full)
        pd.testing.assert_frame_equal(again, full)
