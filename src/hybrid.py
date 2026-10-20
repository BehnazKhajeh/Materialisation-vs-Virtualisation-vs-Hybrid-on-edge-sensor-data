"""Hybrid approach.

Idea: materialise only what is SMALL, STATIC and reused by many queries; keep the
BIG, FAST-CHANGING data virtual.

    Materialised (small RDFLib graph, a few hundred triples):
        places, sensors, ontology  +  rule 1 (class hierarchy)  +  rule 2 (transitive partOf)
    Virtual (stays in SQLite, never copied into the graph):
        readings, and rule 3 (alert) which is evaluated at query time

    q1, q2 : SPARQL over the small materialised graph (same query files as materialisation)
    q3     : ask the graph which temperature sensors are in building 1,
             then let SQLite filter their readings with value > threshold.

Interface shared by all approaches:
    prepare(db_path) -> state
    q1(state), q2(state), q3(state) -> set of IDs
"""
import sqlite3

from rdflib import RDF, Graph

import materialisation as m
from materialisation import EX, SENSOR_CLASS


def load_static_facts(g, con):
    """Same as materialisation.load_base_facts, but WITHOUT the readings."""
    for (b,) in con.execute("SELECT building_id FROM building"):
        g.add((EX[f"building_{b}"], RDF.type, EX.Building))

    for f, b in con.execute("SELECT floor_id, building_id FROM floor"):
        g.add((EX[f"floor_{f}"], RDF.type, EX.Floor))
        g.add((EX[f"floor_{f}"], EX.partOf, EX[f"building_{b}"]))

    for r, f in con.execute("SELECT room_id, floor_id FROM room"):
        g.add((EX[f"room_{r}"], RDF.type, EX.Room))
        g.add((EX[f"room_{r}"], EX.partOf, EX[f"floor_{f}"]))

    for s, stype, r in con.execute("SELECT sensor_id, sensor_type, room_id FROM sensor"):
        g.add((EX[f"sensor_{s}"], RDF.type, SENSOR_CLASS[stype]))
        g.add((EX[f"sensor_{s}"], EX.locatedIn, EX[f"room_{r}"]))


def prepare(db_path):
    """Materialise only the static part; open SQLite for the readings."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA query_only = ON")

    g = Graph()
    g.parse(m.ONTOLOGY, format="turtle")
    load_static_facts(g, con)
    base = len(g)

    n_class = m.rule_subclass(g)
    n_part = m.rule_transitive_part_of(g)

    return {
        "graph": g,   # used by m.q1 / m.q2 (they read state["graph"])
        "con": con,
        "base_triples": base,
        "inferred_triples": len(g) - base,
        "inferred_breakdown": {"class_hierarchy": n_class, "part_of": n_part},
    }


def q1(state):
    return m.q1(state)


def q2(state):
    return m.q2(state)


def _alerts_for(state, sensors):
    """SQLite applies the alert rule to the readings of the given sensors."""
    result = set()
    for i in range(0, len(sensors), 500):     # chunk to stay under SQLite's variable limit
        chunk = sensors[i:i + 500]
        marks = ",".join("?" * len(chunk))
        sql = f"SELECT reading_id FROM reading WHERE sensor_id IN ({marks}) AND value > ?"
        result |= {row[0] for row in state["con"].execute(sql, (*chunk, m.ALERT_THRESHOLD))}
    return result


def q3(state):
    """Alerts in building 1: the graph picks the sensors (SPARQL on every call), SQLite applies the rule."""
    return _alerts_for(state, sorted(q1(state) & q2(state)))


def q3_cached(state):
    """Same answer, but the static sub-answer (temperature sensors in building 1) is computed once.

    The static graph never changes, so its answer can itself be materialised."""
    if "static_sensors" not in state:
        state["static_sensors"] = sorted(q1(state) & q2(state))
    return _alerts_for(state, state["static_sensors"])


if __name__ == "__main__":
    import sys
    import time
    import tracemalloc

    import virtualisation  # reference answer

    db = sys.argv[1] if len(sys.argv) > 1 else "data/sensors_10000.db"

    tracemalloc.start()
    t0 = time.perf_counter()
    state = prepare(db)
    prep = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"prepare: {prep:.2f} s, peak memory {peak / 1e6:.1f} MB")
    print(f"triples: {state['base_triples']} base + {state['inferred_triples']} inferred "
          f"{state['inferred_breakdown']}")

    ref = virtualisation.prepare(db)
    for name, fn, ref_fn in (("q1", q1, virtualisation.q1),
                             ("q2", q2, virtualisation.q2),
                             ("q3", q3, virtualisation.q3)):
        t0 = time.perf_counter()
        res = fn(state)
        ms = (time.perf_counter() - t0) * 1000
        print(f"{name}: {len(res):>7} results in {ms:8.2f} ms | matches virtualisation: {res == ref_fn(ref)}")
