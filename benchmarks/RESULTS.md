# C++ and Rust gBolt benchmark

Measurements use the same Python wrapper and input graphs for both executables. The C++ baseline is commit `16017f8`, immediately before the Rust port.

## Results

On dense graphs at eight threads the optimized Rust executable took 1.185 s and 110.2 MiB, compared with 1.385 s and 123.5 MiB for C++. Timing the Python API on that input gave 2.894 s for Rust and 3.158 s for C++. Across the three workloads and both thread counts, native speedups over C++ ranged from 1.10× to 1.42×.

Wall times in seconds are shown as median [Q1, Q3]. Native peak RSS is in MiB. [Raw measurements](results.json)

| Workload | Threads | Entry point | C++ (s) | Optimized Rust (s) | Speedup | Peak RSS (C++ / Rust) |
|---|---:|---|---:|---:|---:|---:|
| Sparse | 1 | CLI | 0.0956 [0.0859, 0.0962] | 0.0787 [0.0755, 0.0882] | 1.21× | 10.8 / 6.3 |
| Sparse | 1 | API | 0.1116 [0.1018, 0.1170] | 0.0951 [0.0888, 0.0986] | 1.17× | — |
| Sparse | 8 | CLI | 0.0306 [0.0295, 0.0309] | 0.0216 [0.0199, 0.0226] | 1.42× | 12.0 / 10.9 |
| Sparse | 8 | API | 0.0871 [0.0843, 0.0898] | 0.0722 [0.0624, 0.0762] | 1.21× | — |
| Dense | 1 | CLI | 8.0830 [8.0624, 8.0985] | 7.3774 [7.3709, 7.8420] | 1.10× | 62.1 / 28.7 |
| Dense | 1 | API | 9.9605 [9.7644, 10.0434] | 9.2855 [9.2225, 9.3874] | 1.07× | — |
| Dense | 8 | CLI | 1.3849 [1.3640, 1.3850] | 1.1850 [1.0380, 1.1881] | 1.17× | 123.5 / 110.2 |
| Dense | 8 | API | 3.1581 [3.1064, 3.2376] | 2.8939 [2.8926, 3.0079] | 1.09× | — |
| K6 | 1 | CLI | 0.0704 [0.0699, 0.0732] | 0.0525 [0.0523, 0.0541] | 1.34× | 62.1 / 46.0 |
| K6 | 1 | API | 0.0975 [0.0972, 0.0976] | 0.0841 [0.0801, 0.0893] | 1.16× | — |
| K6 | 8 | CLI | 0.0733 [0.0731, 0.0784] | 0.0639 [0.0598, 0.0695] | 1.15× | 66.7 / 46.2 |
| K6 | 8 | API | 0.1004 [0.0993, 0.1013] | 0.0895 [0.0875, 0.0930] | 1.12× | — |

### Before and after Rust optimization

Dense native comparisons interleave the earlier Rust executable with C++ and optimized Rust in the same session. Sparse and K6 comparisons pair both Rust executables in a separate five-repetition run immediately after the main matrix. [Matched sparse and K6 measurements](optimization-trials.json)

| Workload | Threads | Native median, before → after (s) | Native speedup | Native peak RSS, before → after (MiB) | API median, before → after (s) | API speedup |
|---|---:|---:|---:|---:|---:|---:|
| Sparse | 1 | 0.0548 → 0.0539 | 1.02× | 7.2 → 6.1 | 0.0784 → 0.0784 | 1.00× |
| Sparse | 8 | 0.0172 → 0.0146 | 1.18× | 12.0 → 10.8 | 0.0433 → 0.0399 | 1.08× |
| Dense | 1 | 7.6502 → 7.3774 | 1.04× | 130.9 → 28.7 | — | — |
| Dense | 8 | 1.3703 → 1.1850 | 1.16× | 205.7 → 110.2 | — | — |
| K6 | 1 | 0.0500 → 0.0531 | 0.94× | 46.0 → 46.0 | 0.0805 → 0.0807 | 1.00× |
| K6 | 8 | 0.0540 → 0.0579 | 0.93× | 45.8 → 46.0 | 0.0925 → 0.0902 | 1.02× |

Dense peak RSS fell 78% at one thread and 46% at eight threads relative to the original Rust port. At eight threads dense native execution also became 1.16× faster. K6 native runs took 6–7% longer after optimization and their peak RSS stayed close to 46 MiB. The main table gives the dense API timings for C++ and optimized Rust. The earlier complete matrix was run separately. [Earlier measurements](results-before-optimization.json)

### Optimization trials

These trials use the dense input and write output during mining. Each candidate is paired with the streaming standard-library implementation. Times and peak RSS are medians. Every run produced the same 104,084 patterns, supports and graph IDs. [Trial samples and executable hashes](optimization-trials.json)

| Candidate | Threads | Measured pairs | Standard library (s) | Candidate (s) | Speedup | Peak RSS, baseline / candidate (MiB) |
|---|---:|---:|---:|---:|---:|---:|
| SmallVec 1.16.6 | 1 | 3 | 8.794 | 10.869 | 0.81× | 29.1 / 28.4 |
| SmallVec 1.16.6 | 8 | 3 | 1.758 | 1.785 | 0.99× | 102.9 / 105.1 |
| Support screening | 8 | 2 | 1.857 | 1.585 | 1.17× | 103.6 / 96.1 |

SmallVec stored up to eight rightmost-path or history entries inline. Its time increased on both tested thread counts. The final implementation uses standard-library vectors, writes results as they are found, and screens small candidate projections by graph support before canonicality checks. Larger projections reach a separate early canonicality check at 64 embeddings.

### Output verification

| Workload | Patterns | SHA-256 of canonical result records |
|---|---:|---|
| sparse | 943 | `470f8c93da052f820e45cb2942e13e1b9f9639c6e9033453f29b3ddb8b3e243f` |
| dense | 104,084 | `30602683c5ac03c344722fd3da89baac9bfaeb5b9dd2bc2d376c75043c8f0193` |
| k6 | 3 | `1cbcbca85ac7567206032ba9b1d2bc80585888772f64e71ebd7b629afb44a6d8` |

## Workloads

| Workload | Graphs | Vertices per graph | Edges per graph | Minimum support | Maximum pattern vertices |
|---|---:|---:|---:|---:|---:|
| Sparse | 500 | 30 | 35 | 25 | 5 |
| Dense | 200 | 30 | 90 | 20 | 6 |
| K6 | 4,500 | 6 | 15 | 4,500 | 3 |

Sparse and dense use independent draws from `random.Random(42)` initialized separately for each workload. Each graph samples distinct undirected edges uniformly and assigns independent binary vertex and edge labels. K6 repeats a complete graph with all labels set to 1.

## Method and environment

- Ubuntu 22.04.5, Linux 6.8.0-136-generic x86_64, Intel Core i9-14900 (24 cores, 32 logical CPUs). Both programs inherit the same CPU affinity and run with `OMP_NUM_THREADS=1` or `8`.
- C++ uses GCC 11.4.0 with CMake's `-O3 -g -fopenmp -std=c++11` and `GBOLT_PERFORMANCE`. Rust uses rustc 1.100.0 and Cargo's default release profile. Python is 3.13.13, NetworkX is 3.6.1 and pandas is 3.0.2.
- Executable SHA-256 values were `04b8ed12994a4b64d93592bf4e31045d0d8cd7f148c60411e5771916152fc542` for C++ and `bb930213621ba4f33738b7e5d8862d377d4dea7bacd58b48dad8225cb5ce4c1b` for optimized Rust and `348d56bfe4a46890ab6fcd616e6c3983029c8e5d01255a9696b1423e5d52248b` for the earlier Rust version.
- Each workload, thread count and entry point has one warm-up per executable. The next five repetitions alternate the order of C++ and optimized Rust. For dense native runs, an earlier Rust executable is also rotated through the first, middle and last positions. Runs are sequential. Times are wall-clock medians with inclusive first and third quartiles. Speedup divides the C++ median by the Rust median.
- CLI time includes process launch, reading a prewritten gSpan file, mining, and writing all output files. GNU time records the executable's peak resident memory. RSS in the table is the median of five peaks.
- API time covers `FastgSpan.run_from_graphs(graphs)` with NetworkX inputs already constructed. It includes serialization, native execution, parsing, and DataFrame construction. Generation and correctness checks happen outside both timers.
- Each run checks the canonical DFS codes, support and sorted graph IDs against the first C++ run for that workload. Duplicate codes, an incomplete pattern or an occurrence count different from support fail the run. Results from all entry points and thread counts must match.

## Reproduce on Linux

Install the package's Python dependencies, GNU time, CMake, a C++ compiler with OpenMP, and the Rust toolchain. From the repository root run the following in the same shell.

```bash
baseline_dir=$(mktemp -d)
git archive 16017f8 fast_gspan/vendor/gbolt | tar -x -C "$baseline_dir"
cmake -S "$baseline_dir/fast_gspan/vendor/gbolt" -B "$baseline_dir/build"
cmake --build "$baseline_dir/build" --parallel
cargo build --locked --release --manifest-path native/Cargo.toml
python -m benchmarks.compare \
    --cpp "$baseline_dir/build/gbolt" \
    --rust native/target/release/gbolt \
    --output results.json
```

When the earlier Rust executable is available, add `--previous-rust /path/to/previous/gbolt` to also measure its dense native time and peak RSS in the same session. The JSON includes all timings, native peak RSS samples, parameters, source input checksums and output fingerprints. To run a subset, for example, add `--workloads sparse --threads 1 --interfaces cli`.

To reproduce the additional matched Rust-only comparison, use `--reference-rust /path/to/previous/gbolt` in place of `--cpp` and add `--workloads sparse k6`.
