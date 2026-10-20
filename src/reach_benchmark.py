"""Reachability experiment: a recursive rule that is expensive to evaluate AND hard to maintain.

Scenario: a network of routers connected by directed links, changing over time (links are added
and removed). Rule:   reach(x,y) :- link(x,y).    reach(x,y) :- link(x,z), reach(z,y).
Query: "which routers can router s reach?"  (sources are skewed: a few routers are asked about a lot)

Everything is plain Python (no rdflib), so differences come from WHEN the work is done, not from a tool.

Strategies (all see the same updates and queries, answers are cross-checked):
    virtual_bfs   links only; each query runs a graph search at query time
    virtual_sql   links in SQLite; each query is a WITH RECURSIVE query (query rewriting)
    mat_recompute full closure stored; rebuilt from scratch after every batch of updates
    mat_dred      full closure stored and maintained incrementally:
                    insertion: every router reaching u now reaches v and everything v reaches
                    deletion : DRed = over-delete every pair that MIGHT have used the link,
                               then re-derive the ones that still have another derivation
    hybrid_hot    closure stored ONLY for the k most-queried routers (selective materialisation);
                  every other router is answered like virtual_bfs

Why deletion is hard: a pair (x,y) can be reachable via several paths, so removing one link does not
tell us whether (x,y) is still true. Derivation counting breaks on cycles, hence DRed.

Cost model: cost per batch of updates with Q queries = update_ms + Q * query_ms. The break-even Q
between two strategies is where these costs cross (if they do).

Usage (project root):
    python src/reach_benchmark.py
    python src/reach_benchmark.py --nodes 1000 --degree 1.5 --batches 20 --tag _n1000
"""
import argparse
import csv
import gc
import os
import random
import sqlite3
import statistics
import sys
import time
import tracemalloc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
RESULTS = os.path.join(ROOT, "results")
sys.path.insert(0, SRC)

from streaming_benchmark import breakeven_text  # noqa: E402  (pure Python, no rdflib needed)


# ------------------------------------------------------------------ graph search
def reach_from(out, s):
    """Nodes reachable from s via >= 1 link (s itself only if it lies on a cycle)."""
    seen = set(out[s])
    stack = list(seen)
    while stack:
        x = stack.pop()
        for y in out[x]:
            if y not in seen:
                seen.add(y)
                stack.append(y)
    return seen


# ------------------------------------------------------------------ strategies
class VirtualBFS:
    name = "virtual_bfs"

    def __init__(self, n, edges, hot):
        self.n = n
        self.out = [set() for _ in range(n)]
        for u, v in edges:
            self.out[u].add(v)

    def apply(self, ops):
        for kind, u, v in ops:
            if kind == "ins":
                self.out[u].add(v)
            else:
                self.out[u].discard(v)

    def query(self, s):
        return reach_from(self.out, s)

    def stored(self):
        return 0

    def close(self):
        pass


class VirtualSQL:
    name = "virtual_sql"
    SQL = """WITH RECURSIVE r(n) AS (
                 SELECT dst FROM edge WHERE src = ?
                 UNION
                 SELECT e.dst FROM edge e JOIN r ON e.src = r.n)
             SELECT n FROM r"""

    def __init__(self, n, edges, hot):
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE TABLE edge (src INTEGER, dst INTEGER, PRIMARY KEY (src, dst)) WITHOUT ROWID")
        self.con.executemany("INSERT INTO edge VALUES (?,?)", edges)
        self.con.commit()

    def apply(self, ops):
        for kind, u, v in ops:
            if kind == "ins":
                self.con.execute("INSERT INTO edge VALUES (?,?)", (u, v))
            else:
                self.con.execute("DELETE FROM edge WHERE src = ? AND dst = ?", (u, v))
        self.con.commit()

    def query(self, s):
        return {r[0] for r in self.con.execute(self.SQL, (s,))}

    def stored(self):
        return 0

    def close(self):
        self.con.close()


class MatRecompute(VirtualBFS):
    name = "mat_recompute"

    def __init__(self, n, edges, hot):
        super().__init__(n, edges, hot)
        self.reach = [reach_from(self.out, x) for x in range(n)]

    def apply(self, ops):
        super().apply(ops)
        self.reach = [reach_from(self.out, x) for x in range(self.n)]  # from scratch, once per batch

    def query(self, s):
        return self.reach[s]

    def stored(self):
        return sum(len(r) for r in self.reach)


class MatDRed(VirtualBFS):
    name = "mat_dred"

    def __init__(self, n, edges, hot):
        super().__init__(n, edges, hot)
        self.reach = [reach_from(self.out, x) for x in range(n)]
        self.rev = [set() for _ in range(n)]          # rev[y] = all x with reach(x, y)
        for x in range(n):
            for y in self.reach[x]:
                self.rev[y].add(x)

    def apply(self, ops):
        for kind, u, v in ops:
            if kind == "ins":
                self._insert(u, v)
            else:
                self._delete(u, v)

    def _insert(self, u, v):
        self.out[u].add(v)
        sources = self.rev[u] | {u}                    # everyone who reaches u (or is u)
        targets = self.reach[v] | {v}                  # v and everything v reaches
        for x in sources:
            new = targets - self.reach[x]
            if new:
                self.reach[x] |= new
                for y in new:
                    self.rev[y].add(x)

    def _delete(self, u, v):
        self.out[u].discard(v)
        sources = self.rev[u] | {u}
        targets = self.reach[v] | {v}
        # 1) over-delete: every pair (x,y) with x in sources, y in targets MIGHT have used link (u,v)
        pending = {}
        for x in sources:
            dead = targets & self.reach[x]
            if dead:
                self.reach[x] -= dead
                for y in dead:
                    self.rev[y].discard(x)
                pending[x] = dead
        # 2) re-derive: keep a pair if a link x->y exists, or some link x->z leads to a z that reaches y
        changed = True
        while changed:
            changed = False
            for x, dead in pending.items():
                if not dead:
                    continue
                back = {y for y in dead
                        if y in self.out[x] or any(y in self.reach[z] for z in self.out[x])}
                if back:
                    dead -= back
                    self.reach[x] |= back
                    for y in back:
                        self.rev[y].add(x)
                    changed = True

    def query(self, s):
        return self.reach[s]

    def stored(self):
        return sum(len(r) for r in self.reach)


class HybridHot(VirtualBFS):
    name = "hybrid_hot"

    def __init__(self, n, edges, hot):
        super().__init__(n, edges, hot)
        self.hot = set(hot)
        self.reach_hot = {x: reach_from(self.out, x) for x in self.hot}
        self.hits = self.queries = 0

    def apply(self, ops):
        for kind, u, v in ops:
            if kind == "ins":
                self.out[u].add(v)
                affected = [x for x in self.hot if x == u or u in self.reach_hot[x]]
                if affected:
                    tv = reach_from(self.out, v) | {v}
                    for x in affected:
                        self.reach_hot[x] |= tv
            else:
                self.out[u].discard(v)
                for x in self.hot:
                    if x == u or u in self.reach_hot[x]:
                        self.reach_hot[x] = reach_from(self.out, x)   # recompute only affected hot sources

    def query(self, s):
        self.queries += 1
        if s in self.hot:
            self.hits += 1
            return self.reach_hot[s]
        return reach_from(self.out, s)

    def stored(self):
        return sum(len(r) for r in self.reach_hot.values())


STRATEGIES = {c.name: c for c in (VirtualBFS, VirtualSQL, MatRecompute, MatDRed, HybridHot)}


# ------------------------------------------------------------------ workload
def make_workload(n, degree, n_batches, n_ins, n_del, n_queries, n_hot, zipf_s, seed):
    rng = random.Random(seed)
    edge_set, edge_list = set(), []

    def add_random_edge():
        while True:
            u, v = rng.randrange(n), rng.randrange(n)
            if u != v and (u, v) not in edge_set:
                edge_set.add((u, v))
                edge_list.append((u, v))
                return u, v

    for _ in range(int(n * degree)):
        add_random_edge()
    initial = list(edge_list)

    ranking = list(range(n))
    rng.shuffle(ranking)                                   # ranking[0] is the most-queried router
    weights = [1.0 / (i + 1) ** zipf_s for i in range(n)]
    hot = ranking[:n_hot]

    batches = []
    for _ in range(n_batches):
        ops = []
        for _ in range(n_del):
            i = rng.randrange(len(edge_list))
            edge_list[i], edge_list[-1] = edge_list[-1], edge_list[i]
            u, v = edge_list.pop()
            edge_set.discard((u, v))
            ops.append(("del", u, v))
        for _ in range(n_ins):
            u, v = add_random_edge()
            ops.append(("ins", u, v))
        batches.append((ops, rng.choices(ranking, weights, k=n_queries)))
    return initial, batches, hot


# ------------------------------------------------------------------ experiment
def run(names, n, initial, batches, hot):
    strategies, init_ms = [], {}
    for name in names:
        t0 = time.perf_counter()
        strategies.append(STRATEGIES[name](n, initial, hot))
        init_ms[name] = (time.perf_counter() - t0) * 1000
    rows, warned = [], set()
    for i, (ops, queries) in enumerate(batches, 1):
        answers = {}
        for s in strategies:
            t0 = time.perf_counter()
            s.apply(ops)
            update_ms = (time.perf_counter() - t0) * 1000
            res, qtime = [], 0.0
            for src in queries:
                t0 = time.perf_counter()
                r = s.query(src)
                qtime += time.perf_counter() - t0
                res.append(r)
            answers[s.name] = res
            rows.append({"strategy": s.name, "batch": i, "update_ms": round(update_ms, 3),
                         "query_ms": round(qtime * 1000 / len(queries), 4), "stored": s.stored()})
        ref = answers[names[0]]
        for s in strategies:
            ok = all(a == b for a, b in zip(answers[s.name], ref))
            rows_for = [r for r in rows if r["strategy"] == s.name and r["batch"] == i][0]
            rows_for["correct"] = ok
            if not ok and s.name not in warned:
                print(f"  WARNING: {s.name} disagrees with {names[0]} at batch {i}!")
                warned.add(s.name)
        if i % 10 == 0 or i == len(batches):
            print(f"  batch {i}/{len(batches)}", flush=True)
    hit_rates = {s.name: s.hits / s.queries for s in strategies if hasattr(s, "hits") and s.queries}
    for s in strategies:
        s.close()
    return rows, init_ms, hit_rates


def measure_memory(name, n, initial, batches, hot):
    gc.collect()
    tracemalloc.start()
    s = STRATEGIES[name](n, initial, hot)
    for ops, queries in batches:
        s.apply(ops)
        for src in queries:
            s.query(src)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    s.close()
    del s
    gc.collect()
    return round(peak / 1e6, 1)


def summarise(rows, names, init_ms, hit_rates, peaks):
    out = []
    for nme in names:
        rs = [r for r in rows if r["strategy"] == nme]
        u = statistics.mean(r["update_ms"] for r in rs)
        q = statistics.mean(r["query_ms"] for r in rs)
        out.append({"strategy": nme, "init_ms": round(init_ms[nme], 1), "update_ms": round(u, 3),
                    "query_ms": round(q, 4), "stored_pairs": rs[-1]["stored"],
                    "hot_hit_rate": round(hit_rates[nme], 2) if nme in hit_rates else "",
                    "peak_python_mb": peaks.get(nme, ""),
                    "correct_all": all(r["correct"] for r in rs)})
    return out


def plot(rows, summary, names, tag=""):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed -> skipping plots (CSV files are still written).")
        return

    fig, ax = plt.subplots(figsize=(7, 4))
    for nme in names:
        rs = [r for r in rows if r["strategy"] == nme]
        ax.plot([r["batch"] for r in rs], [max(r["update_ms"], 0.001) for r in rs], label=nme)
    ax.set_yscale("log")
    ax.set_xlabel("batch of link updates")
    ax.set_ylabel("update cost (ms, log)")
    ax.set_title("Cost of applying one batch of link updates")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(RESULTS, f"reach_update_per_batch{tag}.png"), dpi=150)

    fig, ax = plt.subplots(figsize=(7, 4))
    qs = [10 ** (k / 4) for k in range(0, 21)]      # 1 .. 10^5 queries per batch
    for s in summary:
        ax.plot(qs, [s["update_ms"] + q * s["query_ms"] for q in qs], label=s["strategy"])
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("queries per batch of updates")
    ax.set_ylabel("total cost per batch (ms)")
    ax.set_title("Reachability: cost per batch = update + Q x query")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(RESULTS, f"reach_breakeven{tag}.png"), dpi=150)

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.bar([s["strategy"] for s in summary], [s["stored_pairs"] for s in summary])
    ax.set_ylabel("stored reach(x,y) facts")
    ax.set_title("Materialised facts at the end")
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
    fig.tight_layout()
    fig.savefig(os.path.join(RESULTS, f"reach_stored_facts{tag}.png"), dpi=150)
    print("plots written to results/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=300)
    ap.add_argument("--degree", type=float, default=1.5, help="average out-degree of the initial graph")
    ap.add_argument("--batches", type=int, default=40)
    ap.add_argument("--inserts", type=int, default=5, help="link insertions per batch")
    ap.add_argument("--deletes", type=int, default=5, help="link deletions per batch")
    ap.add_argument("--queries", type=int, default=50, help="queries per batch (used to measure latency)")
    ap.add_argument("--hot", type=int, default=20, help="number of routers materialised by hybrid_hot")
    ap.add_argument("--zipf", type=float, default=1.0, help="skew of the query sources")
    ap.add_argument("--strategies", nargs="+", default=list(STRATEGIES), choices=list(STRATEGIES))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-memory", action="store_true")
    ap.add_argument("--tag", default="", help="suffix for output files, e.g. _n1000 -> reach_summary_n1000.csv")
    args = ap.parse_args()

    os.makedirs(RESULTS, exist_ok=True)
    initial, batches, hot = make_workload(args.nodes, args.degree, args.batches, args.inserts,
                                          args.deletes, args.queries, args.hot, args.zipf, args.seed)
    names = args.strategies
    print(f"graph: {args.nodes} routers, {len(initial)} links; {args.batches} batches of "
          f"{args.inserts} inserts + {args.deletes} deletes; {args.queries} queries/batch")

    rows, init_ms, hit_rates = run(names, args.nodes, initial, batches, hot)
    peaks = {}
    if not args.no_memory:
        for nme in names:
            print(f"  measuring memory for {nme} ...", flush=True)
            peaks[nme] = measure_memory(nme, args.nodes, initial, batches, hot)
    summary = summarise(rows, names, init_ms, hit_rates, peaks)

    print("\nMean per batch / per query:")
    print(f"{'strategy':<15}{'init ms':>9}{'update ms':>11}{'query ms':>10}{'stored':>9}{'hot hits':>9}{'peak MB':>9}{'correct':>9}")
    for s in summary:
        print(f"{s['strategy']:<15}{s['init_ms']:>9}{s['update_ms']:>11}{s['query_ms']:>10}"
              f"{s['stored_pairs']:>9}{str(s['hot_hit_rate']):>9}{str(s['peak_python_mb']):>9}{str(s['correct_all']):>9}")

    print("\nCheapest strategy per workload (Q = queries per batch of updates):")
    for q in (1, 10, 100, 1000, 10000):
        costs = {s["strategy"]: s["update_ms"] + q * s["query_ms"] for s in summary}
        best = min(costs, key=costs.get)
        print(f"  Q={q:<6} -> {best:<14} ({costs[best]:.1f} ms per batch)")

    print("\nBreak-even analysis:")
    for i in range(len(summary)):
        for j in range(i + 1, len(summary)):
            print("  -", breakeven_text(summary[i], summary[j], abs_tol=0.0005))  # 0.5 microseconds

    with open(os.path.join(RESULTS, f"reach_batches{args.tag}.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(RESULTS, f"reach_summary{args.tag}.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0]))
        w.writeheader()
        w.writerows(summary)
    print(f"\nwritten: results/reach_batches{args.tag}.csv, results/reach_summary{args.tag}.csv")
    plot(rows, summary, names, args.tag)


if __name__ == "__main__":
    main()
