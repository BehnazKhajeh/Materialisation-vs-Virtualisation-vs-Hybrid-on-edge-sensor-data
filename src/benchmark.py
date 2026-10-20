"""Benchmark virtualisation vs materialisation vs hybrid.

Usage (from the project root):
    python src/benchmark.py                                # sizes 10000 100000, 5 repeats
    python src/benchmark.py --sizes 10000 100000 500000
    python src/benchmark.py --approaches virtualisation    # subset (skips the rdflib modules)
    python src/benchmark.py --no-memory                    # skip the (slow) tracemalloc pass

For every (size, approach) it records:
    prep_s      time of prepare()                (measured WITHOUT tracemalloc)
    peak_mb     peak Python memory of prepare()  (measured in a SEPARATE run WITH tracemalloc)
    base/inferred triples
    q1_ms, q2_ms, q3_ms   median of --repeats runs after one warm-up run
    correct     result sets identical to the first approach's?

Outputs: results/results.csv and (if matplotlib is installed) three PNG plots.
"""
import argparse
import csv
import gc
import importlib
import os
import statistics
import subprocess
import sys
import time
import tracemalloc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
DATA = os.path.join(ROOT, "data")
RESULTS = os.path.join(ROOT, "results")
sys.path.insert(0, SRC)

ALL_APPROACHES = ["virtualisation", "materialisation", "hybrid"]
QUERIES = ["q1", "q2", "q3"]


def ensure_data(size):
    path = os.path.join(DATA, f"sensors_{size}.db")
    if not os.path.exists(path):
        print(f"  generating {path} ...")
        subprocess.run([sys.executable, os.path.join(SRC, "generate_data.py"),
                        "--readings", str(size), "--out", path], check=True)
    return path


def close_state(state):
    con = state.get("con") if isinstance(state, dict) else state
    try:
        con.close()
    except Exception:
        pass


def measure_memory(mod, db):
    gc.collect()
    tracemalloc.start()
    state = mod.prepare(db)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    close_state(state)
    del state
    gc.collect()
    return peak / 1e6


def time_query(fn, state, repeats):
    result = fn(state)  # warm-up run (also gives us the result set)
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn(state)
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times), result


def run_one(name, db, repeats, do_memory):
    mod = importlib.import_module(name)
    row = {"approach": name}

    row["peak_mb"] = round(measure_memory(mod, db), 2) if do_memory else ""

    gc.collect()
    t0 = time.perf_counter()
    state = mod.prepare(db)
    row["prep_s"] = round(time.perf_counter() - t0, 3)

    if isinstance(state, dict):
        row["base_triples"] = state.get("base_triples", "")
        row["inferred_triples"] = state.get("inferred_triples", "")
    else:
        row["base_triples"] = row["inferred_triples"] = 0  # virtualisation stores nothing

    results = {}
    for q in QUERIES:
        ms, res = time_query(getattr(mod, q), state, repeats)
        row[f"{q}_ms"] = round(ms, 2)
        results[q] = res

    close_state(state)
    del state
    gc.collect()
    return row, results


def plot(rows, approaches):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed -> skipping plots (CSV is still written).")
        return

    def series(approach, key):
        # floor at 0.001 so near-zero values (virtualisation setup) can be drawn on a log axis
        pts = [(int(r["size"]), max(float(r[key]), 0.001)) for r in rows
               if r["approach"] == approach and r[key] != ""]
        return sorted(pts)

    def line_plot(ax, key, title, ylabel):
        for a in approaches:
            pts = series(a, key)
            if pts:
                ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", label=a)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("number of readings")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(True, which="both", alpha=0.3)

    fig, ax = plt.subplots(figsize=(6, 4))
    line_plot(ax, "peak_mb", "Peak memory of prepare()", "MB (log)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(RESULTS, "memory_vs_size.png"), dpi=150)

    fig, axes = plt.subplots(1, 3, figsize=(14, 4))
    for ax, q in zip(axes, QUERIES):
        line_plot(ax, f"{q}_ms", f"{q} latency (median)", "ms (log)")
    axes[0].legend()
    fig.tight_layout()
    fig.savefig(os.path.join(RESULTS, "latency_vs_size.png"), dpi=150)

    fig, ax = plt.subplots(figsize=(6, 4))
    line_plot(ax, "prep_s", "Setup time of prepare()", "seconds (log)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(RESULTS, "prep_time_vs_size.png"), dpi=150)
    print("plots written to results/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[10_000, 100_000])
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--approaches", nargs="+", default=ALL_APPROACHES, choices=ALL_APPROACHES)
    ap.add_argument("--no-memory", action="store_true")
    args = ap.parse_args()

    os.makedirs(RESULTS, exist_ok=True)
    rows = []

    for size in args.sizes:
        print(f"\n=== {size} readings ===")
        db = ensure_data(size)
        reference = None
        for name in args.approaches:
            print(f"  running {name} ...", flush=True)
            row, results = run_one(name, db, args.repeats, not args.no_memory)
            if reference is None:
                reference = results
            row["correct"] = all(results[q] == reference[q] for q in QUERIES)
            if not row["correct"]:
                print(f"  WARNING: {name} disagrees with {args.approaches[0]}!")
            row["size"] = size
            rows.append(row)
            print(f"    prep {row['prep_s']} s | mem {row['peak_mb']} MB | "
                  f"q1 {row['q1_ms']} ms | q2 {row['q2_ms']} ms | q3 {row['q3_ms']} ms | "
                  f"correct={row['correct']}")

    fields = ["size", "approach", "prep_s", "peak_mb", "base_triples", "inferred_triples",
              "q1_ms", "q2_ms", "q3_ms", "correct"]
    csv_path = os.path.join(RESULTS, "results.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"\nresults written to {csv_path}")
    plot(rows, args.approaches)


if __name__ == "__main__":
    main()
