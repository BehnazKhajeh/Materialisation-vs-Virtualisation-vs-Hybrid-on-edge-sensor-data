"""Which inferences does a GENERAL reasoner materialise, compared with the few we actually need?

Four variants on the same data (first N readings of a generated database):

  hand_selected   our 3 rules only (alert, class hierarchy, transitive partOf), as in materialisation.py
  owlrl_general   a general OWL 2 RL reasoner (owlrl) on the same input, plus our alert rule
                  (a numeric comparison cannot be expressed in OWL RL)
  owlrl_sosa      like owlrl_general, but the graph also contains our SOSA alignment axioms
                  (ontology_sosa.ttl), so the standard SOSA view of the data is materialised too
  virtual_sosa    the hand_selected graph, and the SOSA-phrased query rewritten at query time with
                  SPARQL property paths (nothing extra is materialised)

Checks: q1, q2, q3 must return the same sets for hand_selected and owlrl_general; the SOSA-phrased
query must return the same set as q3 on both SOSA variants.

Usage (from the project root, in the activated virtual environment):
    pip install owlrl
    python src/kg_reasoner_comparison.py                       # sizes 1000 2000 5000
    python src/kg_reasoner_comparison.py --sizes 1000 5000 10000
owlrl is slow; start with small sizes.
Output: results/kg_reasoners.csv
"""
import argparse
import csv
import os
import sqlite3
import statistics
import subprocess
import sys
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
DATA = os.path.join(ROOT, "data")
RESULTS = os.path.join(ROOT, "results")
SOSA_ONTOLOGY = os.path.join(ROOT, "ontology_sosa.ttl")
sys.path.insert(0, SRC)

try:
    import owlrl
    from rdflib import RDF, Graph, Literal
except ImportError as e:
    sys.exit(f"Missing package ({e}). Activate the virtual environment and run: pip install rdflib owlrl")

import hybrid as hyb  # noqa: E402
import materialisation as m  # noqa: E402


def ensure_db(size):
    path = os.path.join(DATA, f"sensors_{size}.db")
    if not os.path.exists(path):
        subprocess.run([sys.executable, os.path.join(SRC, "generate_data.py"),
                        "--readings", str(size), "--out", path], check=True)
    return path


def build_graph(db, n, with_sosa):
    """Ontology (+ optional SOSA alignment) + static facts + the first n readings. No inference."""
    con = sqlite3.connect(db)
    g = Graph()
    g.parse(m.ONTOLOGY, format="turtle")
    if with_sosa:
        g.parse(SOSA_ONTOLOGY, format="turtle")
    hyb.load_static_facts(g, con)
    for rid, sid, value in con.execute(
            "SELECT reading_id, sensor_id, value FROM reading WHERE reading_id <= ?", (n,)):
        r = m.EX[f"reading_{rid}"]
        g.add((r, RDF.type, m.EX.Reading))
        g.add((r, m.EX.fromSensor, m.EX[f"sensor_{sid}"]))
        g.add((r, m.EX.hasValue, Literal(float(value))))
    con.close()
    return g


def copy_graph(g):
    g2 = Graph()
    g2 += g
    return g2


def short(p):
    return str(p).rstrip("/").rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def run_hand(g):
    t0 = time.perf_counter()
    m.rule_alert(g)
    m.rule_subclass(g)
    m.rule_transitive_part_of(g)
    return time.perf_counter() - t0, Counter()


def run_owlrl(g):
    t0 = time.perf_counter()
    m.rule_alert(g)                              # not expressible in OWL RL
    before = set(g)
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(g)
    secs = time.perf_counter() - t0
    breakdown = Counter(short(p) for _, p, _ in (set(g) - before))
    return secs, breakdown


def answers(g):
    st = {"graph": g}
    return {"q1": m.q1(st), "q2": m.q2(st), "q3": m.q3(st)}


def time_query(g, filename, repeats=3):
    st = {"graph": g}
    result = {m._id(row[0]) for row in m._run(st, filename)}     # warm-up run, also the answer
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        m._run(st, filename)
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times), result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[1000, 2000, 5000])
    args = ap.parse_args()
    os.makedirs(RESULTS, exist_ok=True)
    db = ensure_db(max(10_000, max(args.sizes)))

    rows, last_breakdowns = [], {}
    for n in args.sizes:
        print(f"\n=== {n} readings ===", flush=True)
        g0 = build_graph(db, n, with_sosa=False)
        base = len(g0)

        # 1) hand-selected rules
        ga = copy_graph(g0)
        secs, _ = run_hand(ga)
        ans_a = answers(ga)
        rows.append({"readings": n, "variant": "hand_selected", "base_triples": base,
                     "inferred": len(ga) - base, "seconds": round(secs, 2), "query_ms": "", "correct": True})
        print(f"  hand_selected : {len(ga) - base:>8} inferred in {secs:7.2f} s", flush=True)

        # 2) general OWL 2 RL reasoner
        gb = copy_graph(g0)
        print("  running owlrl (can take a while) ...", flush=True)
        secs, bd = run_owlrl(gb)
        ok = answers(gb) == ans_a
        rows.append({"readings": n, "variant": "owlrl_general", "base_triples": base,
                     "inferred": len(gb) - base, "seconds": round(secs, 2), "query_ms": "", "correct": ok})
        print(f"  owlrl_general : {len(gb) - base:>8} inferred in {secs:7.2f} s | same answers: {ok}", flush=True)
        last_breakdowns["owlrl_general"] = bd

        # 3) general reasoner + SOSA alignment (SOSA view materialised)
        gc = build_graph(db, n, with_sosa=True)
        base_c = len(gc)
        secs, bd = run_owlrl(gc)
        q_ms, res = time_query(gc, "q3_sosa.rq")
        ok = res == ans_a["q3"]
        rows.append({"readings": n, "variant": "owlrl_sosa", "base_triples": base_c,
                     "inferred": len(gc) - base_c, "seconds": round(secs, 2),
                     "query_ms": round(q_ms, 1), "correct": ok})
        print(f"  owlrl_sosa    : {len(gc) - base_c:>8} inferred in {secs:7.2f} s | "
              f"SOSA query {q_ms:7.1f} ms | same answer as q3: {ok}", flush=True)
        last_breakdowns["owlrl_sosa"] = bd

        # 4) SOSA view left virtual: property paths on the hand_selected graph
        q_ms, res = time_query(ga, "q3_sosa_virtual.rq")
        ok = res == ans_a["q3"]
        rows.append({"readings": n, "variant": "virtual_sosa", "base_triples": base,
                     "inferred": len(ga) - base, "seconds": "", "query_ms": round(q_ms, 1), "correct": ok})
        print(f"  virtual_sosa  : {len(ga) - base:>8} inferred (same graph as hand_selected) | "
              f"SOSA query {q_ms:7.1f} ms | same answer as q3: {ok}", flush=True)

    print("\nWhat the general reasoner added (by predicate), largest size:")
    for name, bd in last_breakdowns.items():
        top = ", ".join(f"{k}: {v}" for k, v in bd.most_common(6))
        print(f"  {name}: {top}")

    path = os.path.join(RESULTS, "kg_reasoners.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\nwritten: {path}")


if __name__ == "__main__":
    main()
