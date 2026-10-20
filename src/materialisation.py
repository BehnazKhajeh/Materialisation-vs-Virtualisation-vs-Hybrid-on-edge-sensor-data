"""Materialisation approach.

Everything is computed up front and stored in an in-memory RDFLib graph:

    1. Load base facts from SQLite as RDF triples (places, sensors, readings).
    2. Forward-chain the three rules until nothing new can be derived:
         Rule 1  rdfs:subClassOf     -> every TemperatureSensor also gets  a :Sensor  triple
         Rule 2  transitive :partOf  -> room partOf floor, floor partOf building
                                        => room partOf building   (stored explicitly)
         Rule 3  alert rule          -> every temperature reading > threshold gets  a :Alert
    3. Queries are then plain SPARQL lookups over the enlarged graph, no reasoning at query time.

The rules are applied with simple hand-written loops (not owlrl) so you can see
exactly what is derived and count it. The cost shows up in prepare(): time,
memory and the number of inferred triples.

Interface shared by all approaches:
    prepare(db_path) -> state
    q1(state), q2(state), q3(state) -> set of IDs (same format as virtualisation.py)
"""
import os
import sqlite3

from rdflib import RDF, RDFS, Graph, Literal, Namespace

EX = Namespace("http://example.org/edge#")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ONTOLOGY = os.path.join(ROOT, "ontology.ttl")
QUERY_DIR = os.path.join(ROOT, "queries")

# Must match ALERT_THRESHOLD in generate_data.py
ALERT_THRESHOLD = 28.0

SENSOR_CLASS = {
    "temperature": EX.TemperatureSensor,
    "motion": EX.MotionSensor,
    "door": EX.DoorSensor,
}


# ---------------------------------------------------------------- loading base facts
def load_base_facts(g, db_path):
    con = sqlite3.connect(db_path)

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

    # The big part: one reading = 3 triples (timestamps are left out to keep the graph smaller)
    for rid, s, value in con.execute("SELECT reading_id, sensor_id, value FROM reading"):
        reading = EX[f"reading_{rid}"]
        g.add((reading, RDF.type, EX.Reading))
        g.add((reading, EX.fromSensor, EX[f"sensor_{s}"]))
        g.add((reading, EX.hasValue, Literal(float(value))))

    con.close()


# ---------------------------------------------------------------- the three rules
def rule_alert(g):
    """Rule 3: temperature reading above threshold => :Alert."""
    new = []
    for reading, value in g.subject_objects(EX.hasValue):
        if float(value) > ALERT_THRESHOLD:
            sensor = g.value(reading, EX.fromSensor)
            if (sensor, RDF.type, EX.TemperatureSensor) in g:
                new.append((reading, RDF.type, EX.Alert))
    for t in new:
        g.add(t)
    return len(new)


def rule_subclass(g):
    """Rule 1: x a C  and  C subClassOf D  =>  x a D  (repeat until fixpoint)."""
    total = 0
    changed = True
    while changed:
        changed = False
        for sub, sup in list(g.subject_objects(RDFS.subClassOf)):
            new = [(x, RDF.type, sup) for x in g.subjects(RDF.type, sub)
                   if (x, RDF.type, sup) not in g]
            for t in new:
                g.add(t)
            if new:
                changed = True
                total += len(new)
    return total


def rule_transitive_part_of(g):
    """Rule 2: a partOf b  and  b partOf c  =>  a partOf c  (repeat until fixpoint)."""
    total = 0
    changed = True
    while changed:
        changed = False
        new = set()
        for a, b in g.subject_objects(EX.partOf):
            for c in g.objects(b, EX.partOf):
                if (a, EX.partOf, c) not in g:
                    new.add((a, EX.partOf, c))
        for t in new:
            g.add(t)
        if new:
            changed = True
            total += len(new)
    return total


# ---------------------------------------------------------------- interface
def prepare(db_path):
    """Build the graph and materialise all inferences. This is the expensive step."""
    g = Graph()
    g.parse(ONTOLOGY, format="turtle")
    load_base_facts(g, db_path)
    base = len(g)

    n_alert = rule_alert(g)          # before subclass rule so :Alert propagates to :Reading
    n_class = rule_subclass(g)
    n_part = rule_transitive_part_of(g)

    return {
        "graph": g,
        "base_triples": base,
        "inferred_triples": len(g) - base,
        "inferred_breakdown": {"alert": n_alert, "class_hierarchy": n_class, "part_of": n_part},
    }


def _run(state, filename):
    with open(os.path.join(QUERY_DIR, filename)) as f:
        return list(state["graph"].query(f.read()))


def _id(uri):
    """http://example.org/edge#sensor_42 -> 42"""
    return int(str(uri).rsplit("_", 1)[1])


def q1(state):
    return {_id(row[0]) for row in _run(state, "q1_sensors_in_building.rq")}


def q2(state):
    return {_id(row[0]) for row in _run(state, "q2_all_temperature_sensors.rq")}


def q3(state):
    return {_id(row[0]) for row in _run(state, "q3_alerts_in_building.rq")}


if __name__ == "__main__":
    import sys
    import time
    import tracemalloc

    import virtualisation  # same folder; used as the reference answer

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
