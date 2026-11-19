# Comparing the original gBolt, the project C++ fork and fast-gspan Rust

All three executables mined the original 422-graph `Compound_422` database. The unrestricted runs compare the same search. The bounded runs compare two ways to deliver patterns of up to five or eight vertices. Times are wall-clock medians of five measured repetitions. RSS is the median native miner peak in upstream / project C++ / Rust order. Ratios divide the first named implementation’s median time by the second’s. [Raw samples, quartiles, checksums and output sizes](upstream-results.json)

## Unrestricted search

| Support | Threads | Patterns | Upstream (ms) | Project C++ (ms) | Rust (ms) | Upstream / Rust | C++ / Rust | Native RSS (MiB) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.10 | 1 | 15,966 | 388.8 | 502.1 | 373.7 | 1.04× | 1.34× | 39.8 / 23.4 / 11.2 |
| 0.10 | 8 | 15,966 | 88.5 | 108.2 | 87.4 | 1.01× | 1.24× | 47.0 / 26.1 / 17.8 |
| 0.08 | 1 | 28,551 | 613.2 | 807.9 | 584.7 | 1.05× | 1.38× | 46.8 / 30.1 / 12.0 |
| 0.08 | 8 | 28,551 | 99.2 | 126.0 | 92.4 | 1.07× | 1.36× | 51.5 / 28.6 / 19.3 |

## Bounded query at support 0.10

The original executable mines all 15,966 patterns. A Python pass reads its output shards one pattern at a time and writes shards containing the requested sizes. The upstream total adds each run’s native and filter times before taking the median. Both caps reuse that same unrestricted native run, and each filter starts from its original output. Project C++ and Rust accept `-x` directly and write only the qualifying patterns. The RSS columns cover the native process; the upstream filter holds one pattern at a time in the running benchmark driver.

| Vertex cap | Threads | Retained | Upstream + filter (ms) | Filter alone (ms) | Project C++ (ms) | Rust (ms) | Upstream / project C++ | Upstream / Rust | Native RSS (MiB) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 5 | 1 | 288 | 418.6 | 23.9 | 26.5 | 18.9 | 15.79× | 22.17× | 39.8 / 11.2 / 6.1 |
| 8 | 1 | 1,599 | 413.6 | 24.8 | 77.7 | 61.8 | 5.33× | 6.69× | 39.8 / 11.3 / 8.3 |
| 5 | 8 | 288 | 115.0 | 26.5 | 19.5 | 12.3 | 5.89× | 9.34× | 47.0 / 11.5 / 8.0 |
| 8 | 8 | 1,599 | 113.8 | 26.2 | 28.0 | 25.8 | 4.07× | 4.41× | 47.0 / 16.3 / 13.6 |

Even charging the upstream implementation zero time for filtering, the five-vertex query at one thread compares 388.8 ms for unrestricted upstream mining with 18.9 ms for Rust (20.6×). At eight threads the corresponding times are 88.5 and 12.3 ms (7.2×).

## Source changes and provenance

The upstream reference is Keren Zhou's gBolt at `83774d1`. Its `src/` and `include/` trees are identical to those in tag `v1.0`. The first fast-gspan import, `ef88542`, already contains local changes. Comparing the 15 tracked C++ source and header files from upstream with the project fork at `16017f8` gives 11 files changed, 469 lines added and 51 removed. These counts exclude build files, generated configuration, dependencies, Python and the subsequent Rust implementation. [Upstream source](https://github.com/Jokeren/gBolt/tree/83774d171c99fc22756d790c39d0158ec613e786) · [Project import](https://github.com/Masatsugar/fast-gspan/commit/ef88542faf8f2fb8843d92e6e04c3b4a6d0daba8)

| Stage | Behavior |
|---|---|
| Upstream gBolt | Mines frequent connected patterns by growing a canonical DFS code along its rightmost path. Uses projected embeddings and OpenMP tasks. Reports support and graph IDs. |
| fast-gspan C++ and Python | Adds a vertex limit, avoids building forward extensions when the limit is reached, and still tests backward edges that can close cycles. Checks candidate minimality before retaining large embedding lists and releases the raw input before mining. Adds exact absolute support in Python, NetworkX/pandas interfaces, explicit stop reasons and resuming from a saved DFS frontier. |
| Current Rust engine | Preserves the DFS codes, support, graph IDs and resume protocol. Uses scoped workers with per-worker output, releases finished patterns during mining, and checks graph support before canonicality for small candidate lists. Candidates crossing 64 embeddings get an early canonicality test. |

The early fast-gspan fork also introduced a silent 500,000-embedding cutoff. Later fast-gspan removed that default and made the guard explicit and resumable. Original upstream gBolt has no such cutoff. [Completeness fix](https://github.com/Masatsugar/fast-gspan/commit/c2c311c) · [Resume support](https://github.com/Masatsugar/fast-gspan/commit/c6844f6) · [Exact support](https://github.com/Masatsugar/fast-gspan/commit/57fc3b6) · [Rust miner](../native/src/miner.rs)

The project's C++ commit `750faab` also records a different experiment on 200 dense, 30-vertex graphs with a seven-vertex limit and eight threads. The reported mining time fell from 113 s to 19.5 s, and peak RSS from 7.2 GB to 2.5 GB. Those figures compare two historical fork revisions and come from the commit message. The tables above use fresh measurements of the three named engines. [Historical C++ measurements](https://github.com/Masatsugar/fast-gspan/commit/750faabec7706d163818d7d145d989f2b25cbf4c)

## Measurement details

- Input is upstream’s original file, byte-for-byte, with 422 transaction headers and no terminal sentinel. The same raw support fractions `-s 0.10` and `-s 0.08` give absolute thresholds of 42 and 33 using integer truncation. The input SHA-256 is `4744a9d8e749d61139e129b7736fa1ac0cbafff481f7209b0b3d1a8dd19897f9`.
- Each binary receives the same input, `-o` prefix, `-d`, and `OMP_NUM_THREADS=1` or `8`. Each native timer includes launch, input, mining and writing output. Runs are sequential, with one warm-up per condition and five further rounds rotating execution order. Input generation, validation and digesting are outside the timer.
- Upstream writes three edge fields; the other two binaries write five. The verifier reconstructs each ordered five-field DFS code from vertex labels and edge rows, checks any explicit endpoint labels, and matches the code, support and sorted graph IDs. Duplicate patterns, incomplete records and mismatches fail the benchmark. Worker-local IDs and shard order can vary. Output checksums cover normalized pattern records.
- Ubuntu 22.04.5, Linux 6.8.0-136-generic, Intel Core i9-14900 (24 cores, 32 logical CPUs), Python 3.13.13. Both C++ builds use GCC 11.4.0, `-O3 -g -fopenmp -std=c++11` and `GBOLT_PERFORMANCE`, with the system allocator. The upstream build adds `-include limits` for the standard C++ declaration required by GCC 11. Rust uses rustc 1.100.0 and Cargo release settings.

## Reproduce on Linux

From this repository, with the package’s Python dependencies, GNU time, CMake, GCC with OpenMP and Cargo installed:

```bash
work=$(mktemp -d)
git clone https://github.com/Jokeren/gBolt.git "$work/upstream"
git -C "$work/upstream" checkout 83774d171c99fc22756d790c39d0158ec613e786
git -C "$work/upstream" submodule update --init extern/cxxopts
cmake -S "$work/upstream" -B "$work/build-upstream" -DCMAKE_CXX_FLAGS='-include limits'
cmake --build "$work/build-upstream" --parallel
git archive 16017f8 fast_gspan/vendor/gbolt | tar -x -C "$work"
cmake -S "$work/fast_gspan/vendor/gbolt" -B "$work/build-cpp"
cmake --build "$work/build-cpp" --parallel
cargo build --locked --release --manifest-path native/Cargo.toml
python -m benchmarks.compare_upstream \
  --upstream "$work/build-upstream/gbolt" --cpp "$work/build-cpp/gbolt" \
  --rust native/target/release/gbolt --database "$work/upstream/extern/data/Compound_422" \
  --output upstream-results.json
```
