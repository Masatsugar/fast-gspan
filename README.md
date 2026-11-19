# fast-gspan

[![CI](https://github.com/Masatsugar/fast-gspan/actions/workflows/ci.yml/badge.svg)](https://github.com/Masatsugar/fast-gspan/actions)
[![PyPI](https://img.shields.io/pypi/v/fast-gspan.svg)](https://pypi.org/project/fast-gspan/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: BSD-2-Clause](https://img.shields.io/badge/license-BSD--2--Clause-green.svg)](https://opensource.org/licenses/BSD-2-Clause)

fast-gspan mines frequent subgraphs with a parallel Rust implementation of [gBolt](https://github.com/Jokeren/gBolt).

The Python API accepts NetworkX graphs or gSpan files and returns a pandas DataFrame.

## Performance

On gBolt’s `Compound_422` database, the three miners returned identical patterns, supports and graph IDs at `-s 0.10` (minimum support of 42 out of 422 graphs). The table gives median wall time over five runs on an Intel Core i9-14900. For bounded queries, upstream gBolt mined all 15,966 patterns and a post-processing pass wrote the qualifying ones. Both fast-gspan versions applied the vertex cap during mining.

| Max vertices | Threads | Patterns | Upstream gBolt | fast-gspan C++ (`16017f8`) | fast-gspan 0.1.5 (Rust) |
|---|---:|---:|---:|---:|---:|
| Unlimited | 1 | 15,966 | 388.8 ms | 502.1 ms | 373.7 ms |
| Unlimited | 8 | 15,966 | 88.5 ms | 108.2 ms | 87.4 ms |
| 5 | 1 | 288 | 418.6 ms | 26.5 ms | 18.9 ms |
| 5 | 8 | 288 | 115.0 ms | 19.5 ms | 12.3 ms |
| 8 | 1 | 1,599 | 413.6 ms | 77.7 ms | 61.8 ms |
| 8 | 8 | 1,599 | 113.8 ms | 28.0 ms | 25.8 ms |

At eight threads, Rust ran the five-vertex query 9.34× as fast as upstream mining plus filtering. Unrestricted mining took about the same time in upstream gBolt and Rust, with peak resident memory of 47.0 MiB upstream, 26.1 MiB in the C++ fork, and 17.8 MiB in Rust. [Upstream comparison and reproduction steps](https://github.com/Masatsugar/fast-gspan/blob/v0.1.5/benchmarks/UPSTREAM.md) · [C++ and Rust timings through the CLI and Python API](https://github.com/Masatsugar/fast-gspan/blob/v0.1.5/benchmarks/RESULTS.md)

## Installation

The release workflow builds wheels for Linux x86_64 and macOS arm64. Each wheel includes the mining executable.

```bash
pip install fast-gspan
```

### Building from source

Install a stable Rust toolchain with Cargo and a native linker to build from source. The package installer compiles the Rust executable during installation.

```bash
pip install git+https://github.com/Masatsugar/fast-gspan.git
```

To rebuild the executable from a checkout or an installed package, run `python -m fast_gspan build`. The wheel includes the Rust source for this command. Use `python -m fast_gspan clean` to remove the built executable.

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
| `num_threads` | `int` | `0` | Number of mining threads (0 uses the backend default). |
| `timeout` | `float \| None` | `None` | Hard limit in seconds: gBolt is killed and an error is raised, without a result (None = unlimited). |
| `time_limit` | `float \| None` | `None` | Time budget in seconds: when it is used up, the patterns found so far are returned (None = unlimited). See [Time limit and resume](#time-limit-and-resume). |
| `show_progress` | `bool` | `False` | Show progress during mining. |
| `verbose` | `bool` | `False` | Print debug information. |
| `max_projection_size` | `int` | `0` | Opt-in memory guard (0 = unlimited). See [Completeness and memory](#completeness-and-memory). |

Methods:

- **`run_from_graphs(graphs)`** -- Mine from a list of `nx.Graph`. Returns `pd.DataFrame`.
- **`run_from_file(filepath)`** -- Read a gSpan-format file and mine. Returns `pd.DataFrame`.
- **`resume_from_graphs(graphs, previous)`** -- Continue from an incomplete result `previous`. Returns the merged `pd.DataFrame`.
- **`resume_from_file(filepath, previous)`** -- The same for a gSpan-format file.
- **`pattern_to_graph(pattern)`** -- Convert a pattern dict (from raw results) to `nx.Graph`. Static method.

### `GBoltWrapper`

Low-level wrapper around the gBolt binary. Use this if you need direct access to raw pattern dicts.

- **`mine_frequent_subgraphs(graphs, resume_from=None)`** -- Returns `list[dict]` with keys: `pattern_id`, `support`, `vertices`, `edges`, `dfs_codes`, `graph_data`, `stop_reason`. `resume_from` takes the DFS codes of the patterns to continue from.

## Completeness and memory

By default every pattern is extended, however many embeddings it has, so no pattern is lost for being too frequent.

Versions up to 0.1.3 silently dropped any pattern of two or more edges with more than 500,000 embeddings, together with all of its supergraphs. This affects large databases, where it removed even the most frequent patterns (275 of 398 on 249,456 molecules), and also small databases of dense or label-poor graphs (1,120 of 25,462 patterns on 200 graphs of 30 vertices). Results obtained with an earlier version on such data should be recomputed.

Memory grows with the number of threads, because each thread holds the embedding lists of the patterns it is extending. If mining runs out of memory, lower `num_threads` first: the result stays complete and only the run time grows. In a benchmark with the previous C++ backend (200 graphs, 30 vertices, average degree 6, `max_num_vertices=7`), 32 threads took 11 s and 7.3 GB, 8 threads 20 s and 2.5 GB, and 1 thread 104 s and 0.6 GB.

If that is not enough, `max_projection_size` bounds the embedding lists at the cost of completeness:

```python
import warnings
from fast_gspan import FastgSpan, IncompleteResultWarning

fgs = FastgSpan(min_support=10, max_num_vertices=8, max_projection_size=500_000)
df = fgs.run_from_graphs(graphs)  # warns with IncompleteResultWarning if the limit is hit

# Treat an incomplete result as an error instead
warnings.simplefilter("error", IncompleteResultWarning)
```

A pattern of two or more edges with more embeddings than the limit is still reported, with its exact support, but is not extended, so its supergraphs may be missing. Whenever this happens an `IncompleteResultWarning` states how many patterns were affected. Single-edge patterns are always extended.

## Time limit and resume

`time_limit` stops the search after a number of seconds and returns what was found until then. Every returned pattern and support is exact. The patterns that were reached but not extended are flagged in the `stop_reason` column, and `resume_from_graphs` continues from them:

```python
import warnings
from fast_gspan import FastgSpan, IncompleteResultWarning

warnings.simplefilter("ignore", IncompleteResultWarning)

fgs = FastgSpan(min_support=10, max_num_vertices=8, time_limit=3600)
df = fgs.run_from_graphs(graphs)
while df["stop_reason"].notna().any():
    df.to_pickle("checkpoint.pkl")  # optional: lets another process continue later
    df = fgs.resume_from_graphs(graphs, df)
```

Once no `stop_reason` is left, the result is the same as that of a single uninterrupted run.

- **A partial result is not "all the small patterns".** The search is depth-first, so some branches have been explored deeply and others not at all. With several threads, which patterns are found before the limit varies from run to run.
- **Each resume has a start-up cost.** The embeddings are not stored: gBolt re-reads the input and works its way back to the unextended patterns before it finds new ones. `time_limit` has to be well above that. A resume that extended nothing says so in its warning.
- **Use the same graphs and settings.** The graphs must be the same, in the same order, with the same `min_support` and `max_num_vertices`. If the unextended patterns are not found again with the same support, `ValueError` is raised. Each result records its `min_support` in `DataFrame.attrs` (kept by `to_pickle`), and resuming with another one raises `ValueError`. A larger `max_num_vertices` is not detected: the patterns that were already extended under the old limit are not grown further.
- **Results of 0.1.4 and earlier cannot be resumed.** Those versions raised a `min_support` below 1% of the graphs to 1% without a warning, and resuming cannot find the patterns this left out. They have no `attrs` record, and a result without one is rejected with `ValueError`. Run `run_from_graphs` again instead.
- **`max_projection_size` works the same way.** Patterns it stopped are flagged too, and stay unextended until you resume with a larger limit or none.
- **Small unextended patterns stay in the result.** They are kept even below `min_num_vertices`, because they are needed to resume, and are dropped once they have been extended.
- **A killed run cannot be resumed.** `time_limit` ends the run in an orderly way. If gBolt is killed instead, by `timeout` or for lack of memory, nothing is returned ([#1](https://github.com/Masatsugar/fast-gspan/issues/1)).

The call can take longer than `time_limit`. Each worker finishes or marks its current pattern and flushes its output. Python then reads the results into a DataFrame.

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
| `stop_reason` | Missing if the pattern was extended, otherwise why it was not: `"time_limit"` or `"max_projection_size"` |

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

## Comparing backend performance

From a source checkout, build the Rust backend and the C++ reference in separate directories. The benchmark uses GNU time on Linux to record peak memory. Install the Python package and its dependencies before running it.

```bash
python -m fast_gspan build
cmake -S fast_gspan/vendor/gbolt -B /tmp/fast-gspan-cpp
cmake --build /tmp/fast-gspan-cpp --parallel
FAST_GSPAN_CPP_REFERENCE=/tmp/fast-gspan-cpp/gbolt python -m pytest -k matches_brute_force_reference
python -m benchmarks.compare --cpp /tmp/fast-gspan-cpp/gbolt \
  --rust fast_gspan/vendor/gbolt/build/gbolt --output comparison.json
```

The differential test checks small graphs against both the C++ reference and an independent NetworkX enumeration. The benchmark verifies canonical DFS codes, supports and graph IDs for each complete run on sparse graphs, dense graphs and uniformly labeled cliques.

## Changes from upstream gBolt

The Rust backend implements the command-line and mining extensions from this project’s [gBolt](https://github.com/Jokeren/gBolt) fork. To use an existing C++ build for comparison, pass its executable path as `gbolt_path`.

### New features

- **`-x, --max-vertices` option** -- Limits the maximum number of vertices in mined patterns. Allows early pruning during DFS exploration, reducing both runtime and memory usage.
- **`--max-projection-size` option** -- Opt-in memory guard, off by default. Patterns of two or more edges with more embeddings than the limit are reported but not extended, and gBolt prints how many were affected to stderr. (Up to 0.1.3 this was a fixed, silent limit of 500,000.)
- **`--time-limit` option** -- Stops extending patterns after the given number of seconds, measured from the start of gBolt, and saves what was found.
- **`--resume` option** -- Continues a previous run. Takes a file with the DFS codes of the patterns that run reported but did not extend, and mines only below them.

### Memory and speed

These changes do not alter the mined patterns:

- The miner stops adding vertices at `--max-vertices` and prunes nonminimal DFS codes.
- The parsed input and the unpruned graphs are released before mining starts.
- Each worker writes a pattern when its support and stop reason are final. The native process can release completed patterns while it continues the search. The Python API still returns the full result in memory.

### Output format change

- The DFS-code output (`-d` flag) now emits the full tuple:
  ```
  e <from> <to> <from_label> <edge_label> <to_label>
  ```
  The upstream format only emitted `e <from> <to> <edge_label>`. The extended format enables exact reconstruction of canonical DFS codes in the Python wrapper.
- A pattern that was reported but not extended carries an extra line, `u: time_limit` or `u: max_projection_size`.

## License

Masatsugar maintains fast-gspan. Xifeng Yan and Jiawei Han introduced the gSpan algorithm. The Rust backend is a port of Keren Zhou’s gBolt implementation and this project’s extensions. [Original gSpan paper](https://sites.cs.ucsb.edu/~xyan/papers/gSpan.pdf)

The Rust port uses the **BSD 2-Clause License** and retains gBolt’s original 2017 copyright notice for Keren Zhou. [License and redistribution terms](https://github.com/Masatsugar/fast-gspan/blob/v0.1.5/native/LICENSE)

The Python wrapper code in this repository is also released under the BSD 2-Clause License.
