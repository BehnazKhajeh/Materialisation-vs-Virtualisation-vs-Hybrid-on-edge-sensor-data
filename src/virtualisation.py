"""Virtualisation approach.

Nothing is materialised. The data stays in SQLite exactly as generated.
Each SPARQL query from queries/ is translated BY HAND into one SQL query that
performs the needed reasoning at query time:

    Rule 1 (class hierarchy)   -> sensor_type column; every type IS-A Sensor
    Rule 2 (transitive partOf) -> WITH RECURSIVE over room -> floor -> building
    Rule 3 (alert rule)        -> WHERE value > threshold on temperature sensors

Interface shared by all approaches:
    prepare(db_path) -> state
    q1(state), q2(state), q3(state) -> set of IDs
"""
import sqlite3

# Must match ALERT_THRESHOLD in generate_data.py
ALERT_THRESHOLD = 28.0
BUILDING = 1  # the building used in q1 and q3 (:building_1 in the SPARQL files)

# Rule 2: transitive closure of partOf, computed on the fly.
# IDs collide across tables (room 1 vs floor 1), so places are typed: 'room:1', 'floor:1'.
PART_OF_CTE = """
WITH RECURSIVE
direct(child, parent) AS (
    SELECT 'room:'  || room_id,  'floor:'    || floor_id    FROM room
    UNION ALL
    SELECT 'floor:' || floor_id, 'building:' || building_id FROM floor
),
part_of(child, ancestor) AS (
    SELECT child, parent FROM direct
    UNION
    SELECT d.child, p.ancestor
    FROM direct d JOIN part_of p ON d.parent = p.child
)
"""

# q1: all sensors (any type, via rule 1) located in building 1 (via rule 2)
SQL_Q1 = PART_OF_CTE + """
SELECT s.sensor_id
FROM sensor s
JOIN part_of p ON p.child = 'room:' || s.room_id
WHERE p.ancestor = 'building:' || ?
"""

# q2: all temperature sensors (rule 1 only, a plain filter)
SQL_Q2 = "SELECT sensor_id FROM sensor WHERE sensor_type = 'temperature'"

# q3: alert readings (rule 3) from sensors in building 1 (rule 2)
SQL_Q3 = PART_OF_CTE + """
SELECT r.reading_id
FROM reading r
JOIN sensor s ON s.sensor_id = r.sensor_id
JOIN part_of p ON p.child = 'room:' || s.room_id
WHERE s.sensor_type = 'temperature'
  AND r.value > ?
  AND p.ancestor = 'building:' || ?
"""


def prepare(db_path):
    """Virtualisation has (almost) no setup cost: just open the database."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA query_only = ON")
    return con


def q1(con):
    return {row[0] for row in con.execute(SQL_Q1, (BUILDING,))}


def q2(con):
    return {row[0] for row in con.execute(SQL_Q2)}


def q3(con):
    return {row[0] for row in con.execute(SQL_Q3, (ALERT_THRESHOLD, BUILDING))}


if __name__ == "__main__":
    import sys
    import time

    db = sys.argv[1] if len(sys.argv) > 1 else "data/sensors_10000.db"
    con = prepare(db)
    for name, fn in (("q1", q1), ("q2", q2), ("q3", q3)):
        t0 = time.perf_counter()
        res = fn(con)
        print(f"{name}: {len(res):>7} results in {(time.perf_counter() - t0) * 1000:8.2f} ms")
