"""Streaming experiment: what does it cost to KEEP the answers up to date as data arrives?

Readings arrive in batches. Only the most recent --window readings are kept (count-based
sliding window). After every batch we (1) update the store, then (2) answer q3
("alert readings in building 1"). Four strategies are compared in lock-step on the SAME stream:

    virtual          readings in SQLite, alert rule evaluated at query time
    hybrid           small materialised static graph + readings in SQLite (as hybrid.py)
    hybrid_cached    like hybrid, but the static sub-answer (sensors in building 1) is computed once
    mat_incremental  full RDF graph; for each batch only NEW readings are added and the alert
                     rule is applied to them only; expired readings are removed
    mat_recompute    full RDF graph rebuilt from scratch and all rules re-run after every batch
                     (the naive way to keep a materialisation correct)

Why removal is easy here: every :Alert triple is derived from exactly ONE reading, so deleting
that reading's triples removes the derived fact with it. If a derived fact could be derived in
several ways (e.g. reachability over a graph), deletion would need DRed or derivation counting.

Metrics per batch: update_ms (ingest + maintenance), query_ms, graph triples / SQLite rows.
Summary (steady state, i.e. once the window is full): mean update and query cost, memory, and
the cost per batch of a workload with Q queries per batch:   cost(Q) = update + Q * query.
The break-even point between two strategies is the Q where their costs cross (if they do).

Usage (from the project root):
    python src/streaming_benchmark.py
    python src/streaming_benchmark.py --batches 60 --batch-size 1000 --window 20000
    python src/streaming_benchmark.py --strategies virtual hybrid     # no rdflib needed for 'virtual'
    python src/streaming_benchmark.py --no-memory
Outputs: results/stream_batches.csv, results/stream_summary.csv and stream_*.png plots.
"""
import argparse
import csv
import gc
import os
import random
import sqlite3
import statistics
import subprocess
import sys
import time
import tracemalloc
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
DATA = os.path.join(ROOT, "data")
RESULTS = os.path.join(ROOT, "results")
sys.path.insert(0, SRC)

import generate_data as gd  # noqa: E402
import virtualisation as virt  # noqa: E402  (no rdflib needed)


# ------------------------------------------------------------------ helpers
def ensure_db(size=10_000):
    path = os.path.join(DATA, f"sensors_{size}.db")
    if not os.path.exists(path):
        subprocess.run([sys.executable, os.path.join(SRC, "generate_data.py"),
                        "--readings", str(size), "--out", path], check=True)
    return path


def make_stream(sensor_types, n_batches, batch_size, seed):
    """Pre-generate the whole stream so every strategy sees exactly the same data."""
    rng = random.Random(seed)
    items = list(sensor_types.items())
    ts, rid, batches = 1_700_000_000, 0, []
    for _ in range(n_batches):
        batch = []
        for _ in range(batch_size):
            rid += 1
            sid, stype = rng.choice(items)
            ts += rng.randint(0, 2)
            batch.append((rid, sid, ts, gd.gen_value(stype, rng)))  # = reading table columns
        batches.append(batch)
    return batches


def build_base_graph(db):
    """Ontology + static facts (places, sensors). No rules applied, no readings."""
    import hybrid as hyb
    import materialisation as m
    from rdflib import Graph
    con = sqlite3.connect(db)
    g = Graph()
    g.parse(m.ONTOLOGY, format="turtle")
    hyb.load_static_facts(g, con)
    con.close()
    return g


def add_reading(g, rid, sid, value, sensor_types, m, apply_alert):
    from rdflib import RDF, Literal
    EX = m.EX
    r = EX[f"reading_{rid}"]
    g.add((r, RDF.type, EX.Reading))
    g.add((r, EX.fromSensor, EX[f"sensor_{sid}"]))
    g.add((r, EX.hasValue, Literal(float(value))))
    if apply_alert and sensor_types[sid] == "temperature" and value > m.ALERT_THRESHOLD:
        g.add((r, RDF.type, EX.Alert))  # rule 3, applied only to this new reading


# ------------------------------------------------------------------ strategies
class Virtual:
    name = "virtual"

    def __init__(self, db, sensor_types, window):
        self.window = window
        src = sqlite3.connect(db)
        self.con = sqlite3.connect(":memory:")
        src.backup(self.con)  # copies static tables and the indexes on reading
        src.close()
        self.con.execute("DELETE FROM reading")
        self.con.commit()

    def ingest(self, batch):
        self.con.executemany("INSERT INTO reading VALUES (?,?,?,?)", batch)
        self.con.execute("DELETE FROM reading WHERE reading_id <= ?", (batch[-1][0] - self.window,))
        self.con.commit()

    def query(self):
        return {r[0] for r in self.con.execute(virt.SQL_Q3, (virt.ALERT_THRESHOLD, virt.BUILDING))}

    def size(self):
        rows = self.con.execute("SELECT COUNT(*) FROM reading").fetchone()[0]
        pages = self.con.execute("PRAGMA page_count").fetchone()[0]
        psize = self.con.execute("PRAGMA page_size").fetchone()[0]
        return {"triples": 0, "rows": rows, "sqlite_mb": round(pages * psize / 1e6, 2)}

    def close(self):
        self.con.close()


class Hybrid(Virtual):
    name = "hybrid"

    def __init__(self, db, sensor_types, window):
        super().__init__(db, sensor_types, window)
        import hybrid as hyb
        self.hyb = hyb
        self.state = hyb.prepare(db)       # materialises the static graph only
        self.state["con"].close()
        self.state["con"] = self.con       # readings window lives in the in-memory SQLite

    def query(self):
        return self.hyb.q3(self.state)

    def size(self):
        s = super().size()
        s["triples"] = len(self.state["graph"])
        return s


class HybridCached(Hybrid):
    name = "hybrid_cached"

    def query(self):
        return self.hyb.q3_cached(self.state)


class MatIncremental:
    name = "mat_incremental"

    def __init__(self, db, sensor_types, window):
        import materialisation as m
        self.m = m
        self.window = window
        self.sensor_types = sensor_types
        self.g = build_base_graph(db)
        m.rule_subclass(self.g)             # rules 1 and 2 once: static facts never change
        m.rule_transitive_part_of(self.g)
        self.ids = deque()
        self.state = {"graph": self.g}

    def ingest(self, batch):
        for rid, sid, ts, value in batch:
            add_reading(self.g, rid, sid, value, self.sensor_types, self.m, apply_alert=True)
            self.ids.append(rid)
        cutoff = batch[-1][0] - self.window
        while self.ids and self.ids[0] <= cutoff:
            # removing the reading's triples also removes its derived :Alert (single derivation)
            self.g.remove((self.m.EX[f"reading_{self.ids.popleft()}"], None, None))

    def query(self):
        return self.m.q3(self.state)

    def size(self):
        return {"triples": len(self.g), "rows": 0, "sqlite_mb": 0}

    def close(self):
        pass


class MatRecompute:
    name = "mat_recompute"

    def __init__(self, db, sensor_types, window):
        import materialisation as m
        self.m = m
        self.window = window
        self.sensor_types = sensor_types
        self.base = build_base_graph(db)
        self.readings = deque()
        self.g = None
        self.state = {"graph": None}

    def ingest(self, batch):
        from rdflib import Graph
        m = self.m
        self.readings.extend(batch)
        cutoff = batch[-1][0] - self.window
        while self.readings and self.readings[0][0] <= cutoff:
            self.readings.popleft()
        g = Graph()
        g += self.base                       # start again from the base facts
        for rid, sid, ts, value in self.readings:
            add_reading(g, rid, sid, value, self.sensor_types, m, apply_alert=False)
        m.rule_alert(g)                      # re-run every rule over everything
        m.rule_subclass(g)
        m.rule_transitive_part_of(g)
        self.g = g
        self.state["graph"] = g

    def query(self):
        return self.m.q3(self.state)

    def size(self):
        return {"triples": len(self.g) if self.g is not None else 0, "rows": 0, "sqlite_mb": 0}

    def close(self):
        pass


STRATEGIES = {c.name: c for c in (Virtual, Hybrid, HybridCached, MatIncremental, MatRecompute)}


# ------------------------------------------------------------------ experiment
def run_lockstep(names, db, sensor_types, window, batches):
    strategies = [STRATEGIES[n](db, sensor_types, window) for n in names]
    rows, warned = [], set()
    for i, batch in enumerate(batches, 1):
        reference = None
        for s in strategies:
            t0 = time.perf_counter()
            s.ingest(batch)
            update_ms = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            result = s.query()
            query_ms = (time.perf_counter() - t0) * 1000
            if reference is None:
                reference = result
            correct = result == reference
            if not correct and s.name not in warned:
                print(f"  WARNING: {s.name} disagrees with {names[0]} at batch {i}!")
                warned.add(s.name)
            sz = s.size()
            rows.append({"strategy": s.name, "batch": i, "update_ms": round(update_ms, 3),
                         "query_ms": round(query_ms, 3), "alerts": len(result),
                         "triples": sz["triples"], "rows": sz["rows"],
                         "sqlite_mb": sz["sqlite_mb"], "correct": correct})
        if i % 10 == 0 or i == len(batches):
            print(f"  batch {i}/{len(batches)}", flush=True)
    for s in strategies:
        s.close()
    return rows


def measure_memory(name, db, sensor_types, window, batches):
    gc.collect()
    tracemalloc.start()
    s = STRATEGIES[name](db, sensor_types, window)
    for b in batches:
        s.ingest(b)
        s.query()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    s.close()
    del s
    gc.collect()
    return round(peak / 1e6, 1)


def summarise(rows, names, steady_from, peaks):
    out = []
    for n in names:
        rs = [r for r in rows if r["strategy"] == n]
        st = [r for r in rs if r["batch"] > steady_from] or rs
        u = statistics.mean(r["update_ms"] for r in st)
        q = statistics.mean(r["query_ms"] for r in st)
        out.append({"strategy": n, "update_ms": round(u, 2), "query_ms": round(q, 2),
                    "triples": st[-1]["triples"], "rows": st[-1]["rows"],
                    "sqlite_mb": st[-1]["sqlite_mb"], "peak_python_mb": peaks.get(n, ""),
                    "cost_Q1_ms": round(u + q, 1), "cost_Q10_ms": round(u + 10 * q, 1),
                    "cost_Q100_ms": round(u + 100 * q, 1),
                    "correct_all": all(r["correct"] for r in rs)})
    return out


def _le(x, y, abs_tol=1.0):
    """x <= y, treating differences under 5% (or under abs_tol ms) as ties, so noise creates no break-even."""
    return x <= y or abs(x - y) <= max(0.05 * max(x, y), abs_tol)


def breakeven_text(a, b, abs_tol=1.0):
    ua, qa, ub, qb = a["update_ms"], a["query_ms"], b["update_ms"], b["query_ms"]
    a_ok = _le(ua, ub, abs_tol) and _le(qa, qb, abs_tol)
    b_ok = _le(ub, ua, abs_tol) and _le(qb, qa, abs_tol)
    if a_ok and b_ok:
        return f"{a['strategy']} and {b['strategy']} cost about the same (differences within noise)."
    if a_ok:
        return f"{a['strategy']} is at least as cheap as {b['strategy']} for every workload (no break-even)."
    if b_ok:
        return f"{b['strategy']} is at least as cheap as {a['strategy']} for every workload (no break-even)."
    q_star = (ub - ua) / (qa - qb)
    cheaper_low = a if ua < ub else b
    cheaper_high = b if cheaper_low is a else a
    return (f"break-even at ~{q_star:.1f} queries per batch: below it {cheaper_low['strategy']} is "
            f"cheaper, above it {cheaper_high['strategy']}.")


def plot(rows, summary, names):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed -> skipping plots (CSV files are still written).")
        return

    def per_batch(key, title, ylabel, fname, log=True):
        fig, ax = plt.subplots(figsize=(7, 4))
        for n in names:
            rs = [r for r in rows if r["strategy"] == n]
            ys = [max(float(r[key]), 0.001) if log else float(r[key]) for r in rs]
            ax.plot([r["batch"] for r in rs], ys, label=n)
        if log:
            ax.set_yscale("log")
        ax.set_xlabel("batch")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(True, which="both", alpha=0.3)
        ax.legend()
        fig.tight_layout()
        fig.savefig(os.path.join(RESULTS, fname), dpi=150)

    per_batch("update_ms", "Cost of ingesting one batch (incl. maintenance)", "ms (log)",
              "stream_update_per_batch.png")
    per_batch("query_ms", "q3 latency after each batch", "ms (log)", "stream_query_per_batch.png")
    per_batch("triples", "Triples in the RDF graph", "triples", "stream_graph_size.png", log=False)

    fig, ax = plt.subplots(figsize=(7, 4))
    qs = [10 ** (k / 4) for k in range(0, 17)]  # 1 .. 10^4 queries per batch
    for s in summary:
        ax.plot(qs, [s["update_ms"] + q * s["query_ms"] for q in qs], label=s["strategy"])
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("queries per batch")
    ax.set_ylabel("total cost per batch (ms)")
    ax.set_title("Cost per batch = update + Q x query")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(RESULTS, "stream_breakeven.png"), dpi=150)
    print("plots written to results/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batches", type=int, default=40)
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--window", type=int, default=10_000, help="readings kept (count-based window)")
    ap.add_argument("--strategies", nargs="+", default=list(STRATEGIES), choices=list(STRATEGIES))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-memory", action="store_true")
    args = ap.parse_args()

    os.makedirs(RESULTS, exist_ok=True)
    db = ensure_db()
    con = sqlite3.connect(db)
    sensor_types = dict(con.execute("SELECT sensor_id, sensor_type FROM sensor"))
    con.close()

    batches = make_stream(sensor_types, args.batches, args.batch_size, args.seed)
    steady_from = args.window // args.batch_size  # batches after this have a full window
    if steady_from >= args.batches:
        print("NOTE: the window never fills; increase --batches or reduce --window. "
              "Summary will use all batches.")
        steady_from = 0

    print(f"stream: {args.batches} batches x {args.batch_size} readings, window {args.window}")
    rows = run_lockstep(args.strategies, db, sensor_types, args.window, batches)

    peaks = {}
    if not args.no_memory:
        mem_batches = batches[:min(len(batches), args.window // args.batch_size + 5)]
        for n in args.strategies:
            print(f"  measuring memory for {n} ...", flush=True)
            peaks[n] = measure_memory(n, db, sensor_types, args.window, mem_batches)

    summary = summarise(rows, args.strategies, steady_from, peaks)

    print("\nSteady state (window full):")
    hdr = f"{'strategy':<17}{'update ms':>10}{'query ms':>10}{'triples':>9}{'rows':>8}{'sqlite MB':>10}{'py peak MB':>11}{'correct':>9}"
    print(hdr)
    for s in summary:
        print(f"{s['strategy']:<17}{s['update_ms']:>10}{s['query_ms']:>10}{s['triples']:>9}"
              f"{s['rows']:>8}{s['sqlite_mb']:>10}{str(s['peak_python_mb']):>11}{str(s['correct_all']):>9}")

    print("\nCost per batch (ms) for Q queries per batch:")
    for s in summary:
        print(f"  {s['strategy']:<17} Q=1: {s['cost_Q1_ms']:>9}   Q=10: {s['cost_Q10_ms']:>9}   Q=100: {s['cost_Q100_ms']:>9}")

    print("\nBreak-even analysis:")
    for i in range(len(summary)):
        for j in range(i + 1, len(summary)):
            print("  -", breakeven_text(summary[i], summary[j]))

    with open(os.path.join(RESULTS, "stream_batches.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(RESULTS, "stream_summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0]))
        w.writeheader()
        w.writerows(summary)
    print("\nwritten: results/stream_batches.csv, results/stream_summary.csv")
    plot(rows, summary, args.strategies)


if __name__ == "__main__":
    main()
