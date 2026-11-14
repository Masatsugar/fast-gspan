# fast-gspan

[![CI](https://github.com/Masatsugar/fast-gspan/actions/workflows/ci.yml/badge.svg)](https://github.com/Masatsugar/fast-gspan/actions)
[![PyPI](https://img.shields.io/pypi/v/fast-gspan.svg)](https://pypi.org/project/fast-gspan/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: BSD-2-Clause](https://img.shields.io/badge/license-BSD--2--Clause-green.svg)](https://opensource.org/licenses/BSD-2-Clause)

A Python wrapper for frequent subgraph mining powered by the [gBolt](https://github.com/Jokeren/gBolt) C++ backend.

Provides a simple API to mine frequent subgraph patterns from NetworkX graphs, with significant speedups over pure-Python gSpan implementations.

## Installation

Pre-built wheels are available for Linux (x86_64) and macOS (arm64, x86_64):

```bash
pip install fast-gspan
```

### Building from source

If a pre-built wheel is not available for your platform, install from source:

```bash
pip install git+https://github.com/Masatsugar/fast-gspan.git
python -m fast_gspan build   # compile the C++ backend
```

Source builds require:

- CMake >= 3.10
- C++ compiler with C++11 support (GCC, Clang)
- OpenMP (optional, for parallel mining)

```bash
# Ubuntu/Debian
sudo apt-get install cmake g++ make

# macOS
brew install cmake libomp
```

## Quick start

```python
import networkx as nx
from fast_gspan import FastgSpan

# Prepare your graph database
graphs = [...]  # list of NetworkX graphs with 'label' attributes on nodes/edges

# Mine frequent subgraphs
fgs = FastgSpan(min_support=10, max_num_vertices=8)
df = fgs.run_from_graphs(graphs)

print(df[["support", "num_vert", "description"]])
```

### From a gSpan-format file

```python
from fast_gspan import FastgSpan

df = FastgSpan(min_support=10, max_num_vertices=8).run_from_file("graphs.txt")
```

### Parallel mining & progress

```python
fgs = FastgSpan(
    min_support=10,
    max_num_vertices=8,
    num_threads=4,       # 0 = all cores (default)
    show_progress=True,  # show real-time pattern count
)
df = fgs.run_from_graphs(graphs)
```

## API

### `FastgSpan`

High-level interface. Constructor parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `gbolt_path` | `str \| None` | `None` | Path to gBolt executable. Auto-detected if `None`. |
| `min_support` | `int` | `2` | Minimum absolute support threshold. |
| `min_num_vertices` | `int` | `1` | Minimum vertices in a pattern. |
| `max_num_vertices` | `int` | `0` | Maximum vertices in a pattern (0 = unlimited). |
| `num_threads` | `int` | `0` | Number of OpenMP threads (0 = all cores). |
| `timeout` | `float \| None` | `None` | Timeout in seconds for gBolt execution (None = unlimited). |
| `show_progress` | `bool` | `False` | Show progress during mining. |
| `verbose` | `bool` | `False` | Print debug information. |
| `max_projection_size` | `int` | `0` | Opt-in memory guard (0 = unlimited). See [Completeness and memory](#completeness-and-memory). |

Methods:

- **`run_from_graphs(graphs)`** -- Mine from a list of `nx.Graph`. Returns `pd.DataFrame`.
- **`run_from_file(filepath)`** -- Read a gSpan-format file and mine. Returns `pd.DataFrame`.
- **`pattern_to_graph(pattern)`** -- Convert a pattern dict (from raw results) to `nx.Graph`. Static method.

### `GBoltWrapper`

Low-level wrapper around the gBolt binary. Use this if you need direct access to raw pattern dicts.

- **`mine_frequent_subgraphs(graphs)`** -- Returns `list[dict]` with keys: `pattern_id`, `support`, `vertices`, `edges`, `dfs_codes`, `graph_data`.

## Completeness and memory

By default every pattern is extended, however many embeddings it has, so no pattern is lost for being too frequent.

Versions up to 0.1.3 silently dropped any pattern of two or more edges with more than 500,000 embeddings, together with all of its supergraphs. On large databases this removed even the most frequent patterns. If you mined tens of thousands of graphs with an earlier version, re-run with this one.

The embedding lists of very frequent patterns can be large. If mining runs out of memory, `max_projection_size` bounds them:

```python
import warnings
from fast_gspan import FastgSpan, IncompleteResultWarning

fgs = FastgSpan(min_support=10, max_num_vertices=8, max_projection_size=500_000)
df = fgs.run_from_graphs(graphs)  # warns with IncompleteResultWarning if the limit is hit

# Treat an incomplete result as an error instead
warnings.simplefilter("error", IncompleteResultWarning)
```

A pattern of two or more edges with more embeddings than the limit is still reported, with its exact support, but is not extended, so its supergraphs may be missing. Whenever this happens an `IncompleteResultWarning` states how many patterns were affected. Single-edge patterns are always extended.

**Known limitation:** a `min_support` below 1% of the number of graphs is raised to 1% without a warning, so patterns rarer than 1% are not returned.

## Output format

The returned DataFrame has the following columns:

| Column | Description |
|---|---|
| `support` | Number of graphs containing this pattern |
| `description` | DFS-code representation: `(from, to, from_label, edge_label, to_label)` per edge |
| `num_vert` | Number of vertices in the pattern |
| `pattern_id` | Pattern ID assigned by gBolt |
| `vertices` | List of `(vertex_id, label)` tuples |
| `edges` | List of `(from, to, edge_label)` tuples |
| `graph_ids` | List of graph indices where this pattern occurs (if available) |

## gSpan-format file

Input files follow the standard gSpan text format:

```
t # 0
v 0 1
v 1 2
e 0 1 3
t # 1
v 0 1
v 1 1
v 2 2
e 0 1 3
e 1 2 4
t # -1
```

## Changes from upstream gBolt

This package bundles a modified fork of [gBolt](https://github.com/Jokeren/gBolt) with the following changes:

### New features

- **`-x, --max-vertices` option** -- Limits the maximum number of vertices in mined patterns. Allows early pruning during DFS exploration, reducing both runtime and memory usage.
- **`--max-projection-size` option** -- Opt-in memory guard, off by default. Patterns of two or more edges with more embeddings than the limit are reported but not extended, and gBolt prints how many were affected to stderr. (Up to 0.1.3 this was a fixed, silent limit of 500,000.)

### Output format change

- The DFS-code output (`-d` flag) now emits the full tuple:
  ```
  e <from> <to> <from_label> <edge_label> <to_label>
  ```
  The upstream format only emitted `e <from> <to> <edge_label>`. The extended format enables exact reconstruction of canonical DFS codes in the Python wrapper.

### Build system

- CMake minimum version raised from 2.6 to 3.10.
- Added macOS (Apple Clang) fallback for OpenMP via Homebrew `libomp`.

### Bug fixes

- Added `const` qualifier to three `operator()` methods in `include/graph.h` to fix compiler warnings and ensure correctness with modern C++ standards.

## License

The bundled gBolt C++ source is licensed under the **BSD 2-Clause License** (Copyright (c) 2017, Keren Zhou). See [`fast_gspan/vendor/gbolt/LICENSE`](fast_gspan/vendor/gbolt/LICENSE) for details.

The Python wrapper code in this repository is also released under the BSD 2-Clause License.
