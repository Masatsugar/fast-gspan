"""Tests for fast_gspan standalone package."""

import warnings

import networkx as nx
import pandas as pd
import pytest

from fast_gspan.gbolt_wrapper import (
    FastgSpan,
    GBoltWrapper,
    _find_vendor_gbolt,
    _read_gspan_file,
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


# ---- unit tests (no gBolt binary required) ----


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
        fgs = object.__new__(FastgSpan)
        fgs.min_support = 2
        fgs.min_num_vertices = 1
        fgs.max_num_vertices = 0
        fgs._gbolt = None
        df = fgs.run_from_graphs([])
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0
        assert "graph_ids" in df.columns


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
