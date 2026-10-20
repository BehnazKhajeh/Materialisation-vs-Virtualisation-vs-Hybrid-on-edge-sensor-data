"""Look inside what the general OWL 2 RL reasoner (owlrl) adds, instead of only counting it.

Usage (project root, virtual environment active):
    python src/inspect_owlrl.py          # 1000 readings
    python src/inspect_owlrl.py 5000
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kg_reasoner_comparison as k  # noqa: E402
import materialisation as m  # noqa: E402
import owlrl  # noqa: E402
from rdflib import OWL, RDF  # noqa: E402

n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
g = k.build_graph(k.ensure_db(max(10_000, n)), n, with_sosa=False)
m.rule_alert(g)
before = set(g)
owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(g)
new = set(g) - before
print(f"{len(new)} triples added by owlrl on {n} readings\n")

type_classes = Counter(k.short(o) for s, p, o in new if p == RDF.type)
print("Most common classes in the added 'type' triples:")
for cls, c in type_classes.most_common(8):
    print(f"  {cls}: {c}")

same = [(s, o) for s, p, o in new if p == OWL.sameAs]
reflexive = sum(1 for s, o in same if s == o)
print(f"\nsameAs triples: {len(same)}, of which reflexive (x sameAs x): {reflexive}")

print("\nA few examples of added triples:")
shown = Counter()
for s, p, o in sorted(new, key=str):
    key = k.short(p)
    if key in ("type", "sameAs") and shown[key] < 4:
        shown[key] += 1
        print(f"  {k.short(s)}  {key}  {k.short(o)}")

errors = [s_ for s_, p_, o_ in new if p_ == RDF.type and k.short(o_) == "ErrorMessage"]
for e in errors:
    print("\nErrorMessage node and its triples:")
    for _, p_, o_ in g.triples((e, None, None)):
        print(f"  {k.short(p_)}: {o_}")
