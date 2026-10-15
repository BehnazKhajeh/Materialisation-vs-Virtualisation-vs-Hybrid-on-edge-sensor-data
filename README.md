# Materialisation vs Virtualisation vs Hybrid on edge sensor data

## Summary
**Question.** For knowledge-graph reasoning on a resource-constrained device, which inferred facts should be stored
(materialised) and which should be computed when a query arrives (virtual)?

**What was done.** Materialised, virtual and hybrid strategies (RDFLib, SQLite and plain Python), evaluated on
(1) a synthetic sensor scenario with cheap rules, loaded once and as a sliding-window stream,
(2) a recursive reachability rule on a changing network, and
(3) a comparison of a general OWL 2 RL reasoner with hand-selected rules. In every run the answers of all strategies were cross-checked.

**Main findings.**
1. With cheap, non-recursive rules, virtual evaluation was cheapest on every measure. Fully materialising the data cost memory linear in
   the data (about 1.6 GB at 500k readings) although under 1% of the triples were inferred.
2. With a recursive rule, the cheapest strategy depended on the number of queries per batch of updates: virtual at low rates, materialising only
   the most-queried facts in a middle range, the full closure at very high rates. A simple DRed implementation was slower than recomputing.
3. At 5,000 readings a general OWL 2 RL reasoner materialised 64x more triples than the three rules the queries needed; most of the extra were
   generic facts about every individual.

**Main limitations.** Synthetic data and random graphs, one machine (absolute times vary about 2x between runs), pure-Python tools (RDFLib, owlrl),
and no study of real streams or of where to compute (edge, fog or cloud).

A one-week mini project exploring a core question for knowledge graphs on edge devices:
**which inferences should be materialised, and which can stay virtual?**

Scenario: a smart-building gateway holds sensor readings and must answer queries that need
reasoning (class hierarchy, transitive `partOf`, an alert rule), under tight resource limits.

| Approach | Idea |
|---|---|
| Materialisation | Load everything as RDF triples (RDFLib) and store all inferred triples up front |
| Virtualisation | Keep data in SQLite; each query is translated by hand to SQL (`WITH RECURSIVE`) and reasons at query time |
| Hybrid | Materialise only the small static part (places, sensors, class hierarchy, `partOf` closure) as a graph; keep readings in SQLite and evaluate the alert rule at query time |

## Setup and usage
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate.bat      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python src/benchmark.py --sizes 10000 100000 500000
```
Each approach can also be run alone, e.g. `python src/hybrid.py data/sensors_10000.db`; it
checks its answers against `virtualisation.py`.

Further experiments (details below):
```bash
python src/streaming_benchmark.py                       # sliding window over a stream
python src/reach_benchmark.py --tag _n300               # recursive reachability rule (no rdflib needed)
python src/reach_benchmark.py --nodes 1000 --degree 1.5 --batches 20 --tag _n1000
python src/reach_benchmark.py --nodes 1000 --degree 1.5 --batches 20 --inserts 1 --deletes 1 --tag _n1000_light
```

## Layout
- `ontology.ttl`: vocabulary and the 3 rules
- `queries/`: the 3 SPARQL queries (q1: sensors in a building, q2: temperature sensors, q3: alerts in a building)
- `src/generate_data.py`: synthetic data (2 buildings, 6 floors, 30 rooms, 90 sensors, N readings)
- `src/{virtualisation,materialisation,hybrid}.py`: one module per approach, same interface
- `src/benchmark.py`: runs everything, writes `results/results.csv` and plots
- `src/streaming_benchmark.py`: sliding-window stream, incremental maintenance vs recomputation
- `src/reach_benchmark.py`: recursive reachability rule on a changing network (DRed, selective materialisation)
- `ontology_sosa.ttl`, `src/kg_reasoner_comparison.py`: SOSA alignment and general OWL 2 RL reasoner vs hand-selected rules
- `src/inspect_owlrl.py`: prints what the general reasoner added (classes, `sameAs`, examples)

## Ontology
`ontology.ttl` is a small, hand-made vocabulary. Its parts and where the code uses them:

| Part | In the file | Used for |
|---|---|---|
| Sensor hierarchy | `TemperatureSensor`, `MotionSensor`, `DoorSensor` are subclasses of `Sensor` | rule 1 (`rule_subclass` reads these `rdfs:subClassOf` triples) |
| Place hierarchy | `Room`, `Floor`, `Building` are subclasses of `Place` | rule 1 also applies it (38 `Place` triples), but none of q1 to q3 uses `Place` |
| Containment | `partOf` is an `owl:TransitiveProperty`; `locatedIn` links a sensor to its room | rule 2 (`rule_transitive_part_of` is hard-coded to `partOf`) |
| Readings | `Reading`, `Alert` (subclass of `Reading`), `hasValue` | rule 3 |

Rule 3 (a reading above 28 degrees is an `Alert`) uses a numeric comparison, which OWL 2 RL cannot express, so it is
implemented in code (and as `value > threshold` in SQL). The file only documents it in a comment. The ontology has no
domain/range or disjointness axioms, and `fromSensor` is used in the data but not declared in it.

`ontology_sosa.ttl` aligns our terms with SOSA (the W3C/OGC sensor, observation, sample and actuator vocabulary; the term names were checked against the published specification): `Reading` is a subclass of
`sosa:Observation`, `fromSensor` a subproperty of `sosa:madeBySensor`, `hasValue` a subproperty of `sosa:hasSimpleResult`,
and `Sensor` a subclass of `sosa:Sensor`. It contains only these alignment axioms and minimal declarations of the SOSA terms,
not the SOSA ontology itself.

## Method
For each data size and approach: setup time (`prepare()`, no tracemalloc), peak Python memory of
`prepare()` (separate run with `tracemalloc`), and per-query latency as the median of 5 runs after
one warm-up run. All three approaches returned identical result sets in every run.

## Environment
- OS: Windows (build 10.0.26200)
- CPU: 13th Gen Intel(R) Core(TM) i5-13450HX (16 logical cores)
- RAM: 15.7 GB
- Python: 3.11.8
- Packages: rdflib 7.6.0, owlrl 7.6.2, matplotlib 3.11.2
- SQLite library: 3.43.1
- This is a laptop-class machine, not an edge device. All timings come from this single machine and absolute times varied by about 2x between runs, so compare ratios rather than absolute values.

## Results
Single machine, Python 3, RDFLib. Latencies are medians in ms. Plots: `results/memory_vs_size.png`,
`results/latency_vs_size.png`, `results/prep_time_vs_size.png`.

| Readings | Approach | Setup (s) | Peak memory (MB) | q1 (ms) | q2 (ms) | q3 (ms) |
|---:|---|---:|---:|---:|---:|---:|
| 10,000 | Virtualisation | ~0 | ~0 | 0.36 | 0.05 | 0.35 |
| 10,000 | Materialisation | 0.46 | 34.6 | 25.9 | 3.26 | 52.6 |
| 10,000 | Hybrid | 0.009 | 0.46 | 26.9 | 3.27 | 28.7 |
| 100,000 | Virtualisation | ~0 | ~0 | 0.20 | 0.05 | 0.96 |
| 100,000 | Materialisation | 5.5 | 319.7 | 27.8 | 4.11 | 500 |
| 100,000 | Hybrid | 0.009 | 0.46 | 24.6 | 3.25 | 33.6 |
| 500,000 | Virtualisation | ~0 | ~0 | 0.25 | 0.05 | 18.5 |
| 500,000 | Materialisation | 28.9 | 1,647 | 25.2 | 3.76 | 2,533 |
| 500,000 | Hybrid | 0.009 | 0.46 | 24.4 | 3.28 | 166 |

## Findings
1. **Materialisation scales linearly in memory and setup time and was the worst on every axis here.**
   At 500k readings it needs about 1.6 GB and 29 s of setup. Its q3 latency also grows roughly
   linearly with the data (53 ms, 500 ms, 2.5 s).
2. **The cost is the data, not the inference.** At 10k readings only 220 of about 30,500 triples
   were inferred (<1%). Almost all of the cost comes from copying the readings into the graph.
3. **Hybrid removes that cost.** Its graph holds about 430 triples regardless of data size, so memory
   (0.46 MB) and setup (~9 ms) are constant. At 500k readings that is roughly 3,500x less memory
   and 15x faster q3 than full materialisation.
4. **Pure virtualisation was still the fastest in this setup**, including q3 (18.5 ms vs 166 ms for
   hybrid at 500k), and q1/q2 (sub-millisecond vs about 25 ms / 3 ms for the RDFLib-based approaches).
   The hypothesis that hybrid gives the best trade-off was therefore **not confirmed** here.

## Streaming experiment (sliding window)
`python src/streaming_benchmark.py`: 40 batches of 1,000 readings arrive; only the last 10,000 are kept
(count-based window). After every batch the store is updated and q3 is answered. Five strategies
process the same stream in lock-step and returned identical answers in every batch.

- `virtual`: readings in SQLite, alert rule evaluated at query time
- `hybrid`: small materialised static graph + readings in SQLite; each q3 call runs SPARQL on the static graph
- `hybrid_cached`: like `hybrid`, but the static sub-answer (temperature sensors in building 1) is computed once
- `mat_incremental`: full RDF graph; only new readings are added (alert rule applied to them only) and expired ones removed
- `mat_recompute`: full graph and all rules rebuilt from scratch after every batch

Steady state (window full), mean per batch. Plots: `results/stream_*.png`.

| Strategy | Update (ms) | q3 (ms) | Triples | SQLite rows | Peak Python memory (MB) |
|---|---:|---:|---:|---:|---:|
| virtual | 2.87 | 0.28 | 0 | 10,000 | 0.0* |
| hybrid | 2.73 | 27.5 | 433 | 10,000 | 0.8 |
| hybrid_cached | 2.75 | 0.46 | 433 | 10,000 | 0.6 |
| mat_incremental | 71.5 | 43.8 | 30,503 | 0 | 46.7 |
| mat_recompute | 514 | 43.5 | 30,503 | 0 | 83.8 |

\* `tracemalloc` does not see SQLite's memory; the in-memory SQLite database is about 0.5 MB for the SQLite-based strategies.

Total cost per batch for Q queries per batch (update + Q x query), in ms:

| Strategy | Q=1 | Q=10 | Q=100 |
|---|---:|---:|---:|
| virtual | 3.2 | 5.7 | 31 |
| hybrid | 30 | 277 | 2,748 |
| hybrid_cached | 3.2 | 7.4 | 49 |
| mat_incremental | 115 | 510 | 4,451 |
| mat_recompute | 558 | 950 | 4,865 |

Run-to-run variation: an earlier run of the same experiment gave absolute times about 2x higher
(e.g. 5.98 ms for the `virtual` update), but the same ratios, so compare ratios rather than absolute values.

Findings:
1. **Incremental maintenance pays off against recomputation:** about 7x cheaper per batch
   (72 ms vs 514 ms) with identical answers and lower peak memory (47 MB vs 84 MB).
2. **Both materialised variants are still far more expensive than virtualisation:** about 25x on update and
   about 156x on query; at Q=100 the cost per batch is 31 ms vs 4.5 s.
3. **The slow hybrid query was an implementation choice.** Caching the static sub-answer cut q3 from 27.5 ms
   to 0.46 ms. `hybrid_cached` is still about 1.6x slower than `virtual` per query (49 ms vs 31 ms at Q=100);
   the script's tie tolerance (1 ms) labels the two as equal, so read the table, not the break-even line.
4. **No break-even where materialisation wins** in this scenario.

Why deleting expired readings was easy: every `:Alert` is derived from exactly one reading, so removing that
reading's triples removes the derived fact too. The reachability experiment below covers the case where it is not.

## Reachability experiment (recursive rule on a changing network)
The sensor rules above are cheap. To test a rule that is expensive to evaluate and hard to maintain, a second
experiment (`src/reach_benchmark.py`, plain Python, no RDFLib) uses a network of routers with directed links that
are added and removed over time, the rule `reach(x,y) :- link(x,y).  reach(x,y) :- link(x,z), reach(z,y).`, and the query
"which routers can router s reach?". Query sources are skewed (Zipf): a few routers are asked about a lot.
Every batch has the same number of link insertions and deletions, followed by 50 queries (used to measure latency).

- `virtual_bfs`: links only; each query runs a graph search
- `virtual_sql`: links in SQLite; each query is a `WITH RECURSIVE` query
- `mat_recompute`: full closure stored, rebuilt from scratch after every batch
- `mat_dred`: full closure stored and maintained incrementally. Insertion adds the new pairs; deletion uses DRed:
  over-delete every pair that might have used the link, then re-derive those that still have another derivation
  (derivation counting does not work on cyclic graphs)
- `hybrid_hot`: closure stored only for the 20 most-queried routers; all other routers are answered like `virtual_bfs`

Cost model: cost per batch with Q queries = update + Q x query. All strategies returned identical answers in every batch.

Random directed graph, 1,000 routers, 1,500 links, 5 insertions + 5 deletions per batch:

| Strategy | Update (ms/batch) | Query (ms) | Stored facts | Peak Python memory (MB) |
|---|---:|---:|---:|---:|
| virtual_bfs | 0.007 | 0.080 | 0 | 0.3 |
| virtual_sql | 0.069 | 0.548 | 0 | 0.1 |
| mat_recompute | 81.3 | 0.0003 | 364,602 | 39.7 |
| mat_dred | 2,213 | 0.0002 | 364,602 | 48.5 |
| hybrid_hot | 5.63 | 0.039 | 7,846 | 0.8 |

Cheapest strategy by queries per batch (Q), over all three configurations that were run:

| Setup | Q=1 | Q=10 | Q=100 | Q=1,000 | Q=10,000 |
|---|---|---|---|---|---|
| 300 routers, 5 ins + 5 del | virtual_bfs | virtual_bfs | virtual_bfs | mat_recompute | mat_recompute |
| 1,000 routers, 5 ins + 5 del | virtual_bfs | virtual_bfs | virtual_bfs | hybrid_hot | mat_recompute |
| 1,000 routers, 1 ins + 1 del | virtual_bfs | virtual_bfs | hybrid_hot | hybrid_hot | mat_recompute |

Break-even points from the same runs: `hybrid_hot` is cheaper than both `virtual_bfs` and `mat_recompute` between
about 139 and 1,941 queries per batch (1,000 routers, 5+5), between about 30 and 2,055 (1,000 routers, 1+1) and between about
164 and 458 (300 routers).

Findings:
1. **There is no single winner; the best strategy depends on the query rate per update.** Virtual evaluation wins
   at low rates, selective materialisation in a middle range, and the full closure only at very high rates.
2. **Selective materialisation is cheap for what it buys.** Storing the closure for 20 of 1,000 routers answered about half
   of the queries (hit rate 0.48 to 0.57) with 46x fewer stored facts than the full closure (7,846 vs 364,602) and
   about 50x less peak memory (0.8 MB vs 39.7 MB).
3. **Naive DRed was worse than recomputing from scratch.** At 1,000 routers one deletion costs about 0.4 s with DRed,
   while a full rebuild takes about 0.08 s regardless of the number of updates in the batch (about 5x worse with one
   deletion per batch, 27x worse with five). A probable reason is that a random graph with this density has a large strongly
   connected core: deleting one link rarely changes reachability, yet DRed first removes every pair in the
   (ancestors of u) x (descendants of v) rectangle and must re-derive almost all of them. This explanation was not verified.
4. **In this setup SQLite's recursive query was about 7x slower per query than the Python graph search**
   (0.55 ms vs 0.080 ms at 1,000 routers).

## General reasoner vs hand-selected rules (knowledge-graph experiment)
`python src/kg_reasoner_comparison.py` asks how much a general OWL 2 RL reasoner (`owlrl`) materialises compared with the
three rules that queries q1 to q3 actually need, and whether to materialise the SOSA view of the data
(`ontology_sosa.ttl`) or leave it virtual. Four variants on the first N readings of the same data:

- `hand_selected`: our three rules only (as in `materialisation.py`)
- `owlrl_general`: `owlrl` on the same input, plus our alert rule (a numeric comparison is not expressible in OWL RL)
- `owlrl_sosa`: like `owlrl_general`, with the SOSA alignment axioms added, so the SOSA view is materialised
- `virtual_sosa`: the `hand_selected` graph; the SOSA-phrased query is rewritten at query time with SPARQL property paths

q1 to q3 returned identical answers for `hand_selected` and `owlrl_general`, and the SOSA-phrased query returned the
same answer as q3 in both SOSA variants, at every size.

| Readings | Variant | Inferred triples | Setup (s) | SOSA query (ms) |
|---:|---|---:|---:|---:|
| 1,000 | hand_selected | 160 | 0.00 | |
| 1,000 | owlrl_general | 3,083 | 1.09 | |
| 1,000 | owlrl_sosa | 6,195 | 1.60 | 5.3 |
| 1,000 | virtual_sosa | 160 | | 6.2 |
| 2,000 | hand_selected | 165 | 0.01 | |
| 2,000 | owlrl_general | 5,464 | 2.07 | |
| 2,000 | owlrl_sosa | 11,576 | 3.18 | 8.6 |
| 2,000 | virtual_sosa | 165 | | 10.3 |
| 5,000 | hand_selected | 187 | 0.01 | |
| 5,000 | owlrl_general | 12,048 | 5.16 | |
| 5,000 | owlrl_sosa | 27,160 | 7.82 | 21.1 |
| 5,000 | virtual_sosa | 187 | | 26.5 |

Added by the general reasoner at 5,000 readings, by predicate: `type` 6,020, `sameAs` 5,917, `subClassOf` 33, `partOf` 30,
`equivalentClass` 12, `subPropertyOf` 3. With the SOSA alignment: `type` 11,110, `sameAs` 5,921, `madeBySensor` 5,000,
`hasSimpleResult` 5,000.

Findings:
1. **The hand-selected inference count hardly depends on the data (160 to 187); the general reasoner's grows linearly**
   (about 2.4 inferred triples per reading). At 5,000 readings it materialised 64x more triples, and about 98% of them were not
   needed: the 187-triple graph returned identical answers for q1 to q3.
2. **Most of the excess is generic: `type` and `sameAs` triples about every individual and every literal value.** At 1,000
   readings, `src/inspect_owlrl.py` shows that of 3,081 added triples 1,128 are `owl:Thing` types (one for each of the 1,000 readings,
   90 sensors and 38 places), 247 are `xsd:double` types (one per distinct reading value), and all 1,448 `sameAs` triples are
   reflexive (`x sameAs x`). The 90 `Sensor` and 38 `Place` types are the same 128 class-hierarchy triples that our rule 1 produces,
   so the useful part of the output is what the three rules already computed. These generic facts grow with the number of readings and
   distinct values, which is why the ratio to the hand-selected count rises from about 19x to 64x. owlrl also added one `ErrorMessage` node, which reports that the literal
   `'0.0'` "does not match its datatype" `xsd:double`. `'0.0'` is a valid `xsd:double` lexical form and no other value was flagged
   at 1,000 readings (`1.0`, `17.62`, ...), so this looks like a quirk of owlrl's datatype check (a possible cause is that it treats the value
   0.0 as missing; not verified) and not a problem in the data. It did not change any query answer.
3. **The general reasoner was also much slower:** 5.2 s vs about 0.01 s at 5,000 readings (hundreds of times; the
   hand-selected time is close to the timer resolution), roughly 1 ms per reading and growing linearly.
4. **Materialising the SOSA view costs exactly 3 triples per reading** (15,112 extra at 5,000 readings: the
   `sosa:Observation` type, `madeBySensor` and `hasSimpleResult`) and about 2.7 s of extra reasoning time. Leaving it virtual adds no
   triples and made the SOSA-phrased query 17% to 26% slower (26.5 vs 21.1 ms at 5,000 readings).
5. A rough break-even from these medians: for static data the materialised SOSA view repays its extra setup only after about 500
   SOSA-phrased queries (5.4 ms saved per query), and it would have to be maintained when the data changes. This is a back-of-the-envelope
   figure from three-run medians at one size.

## Takeaway: what to materialise where
- **Cheap, non-recursive rules (the sensor scenario):** virtualisation wins. Fully materialising the data costs memory
  linear in the data and, even with incremental maintenance, about 25x more per batch than virtual evaluation. Only the
  small static part is worth materialising, and with its answer cached (`hybrid_cached`) it is close to pure virtualisation.
- **Expensive recursive rules (reachability):** the choice depends on how many queries arrive per update batch and how
  skewed they are. Few queries: evaluate at query time. A middle range: materialise only the facts that are queried most.
  Very many queries: maintain the full closure.
- **General reasoning materialises what follows; selective materialisation computes what queries need.** At 5,000 readings
  a general OWL 2 RL reasoner produced 64x more triples than the three rules the queries required, and the excess grew with
  the data, while the selective cost stayed nearly flat.
- **Standard-vocabulary views are a materialise-or-rewrite choice too.** Materialising the SOSA view adds 3 triples per
  reading; rewriting at query time costs a modest per-query overhead.
- **The maintenance algorithm matters as much as the choice of what to materialise.** Naive incremental deletion (DRed) was
  slower than rebuilding everything on these graphs.

## Limitations
- The sensor virtualisation baseline uses **hand-written SQL**: it is a best case. A real OBDA system
  (e.g. Ontop) adds query-rewriting overhead and restricts the rules that can be expressed.
- RDFLib is a pure-Python engine; the absolute numbers for the graph-based sensor strategies reflect the tool, not
  materialisation as such. The reachability experiment avoids this by using plain Python for all strategies.
- Data is synthetic. The reachability graphs are random directed graphs, not real network topologies.
- The sensor benchmark loads data once; updates are covered by the streaming experiment (count-based window,
  a rule with a single derivation per fact).
- `hybrid_hot` picks its hot routers from the true query distribution; in practice that distribution has to be learned,
  so its advantage is optimistic. The size of the hot set (20) was not tuned. Its deletion handling recomputes the affected
  hot routers; it is not DRed.
- Query latencies of the materialised reachability strategies exclude copying the result out of the store; the
  virtual strategies return a freshly computed set.
- Memory is Python allocations measured with `tracemalloc` (SQLite memory is not counted); setup time comes from
  one run per size; one machine, not an actual edge device. Absolute times varied by about 2x between runs.
- `owlrl` is a pure-Python reference implementation run with default settings, and only sizes up to 5,000 readings were tried;
  a native OWL RL engine may behave very differently. The composition of its extra triples was inspected only at 1,000 readings.
- The ontology alignment contains only our own axioms, not the full SOSA/SSN ontology, and no `FeatureOfInterest` or
  observable-property modelling.
- The DRed result applies to this implementation on these graphs; better dynamic transitive closure algorithms exist and were not tried.
- The **where** dimension of the research question (edge, fog or cloud) is not studied at all; everything runs on one machine.

## Future work
- Learn the hot set from the query log and tune its size; compare with other selective-materialisation policies.
- Repeat the reasoner comparison with a native OWL RL engine and larger sizes, and with the full SOSA/SSN ontology loaded.
- Better maintenance for recursive rules than plain DRed, and real network topologies instead of random graphs.
- A real stream engine (e.g. RSP4J) and time-based windows.
- Repeat on a Raspberry Pi or a memory-limited container, with a native reasoner (e.g. RDFox), and split the
  computation across edge, fog and cloud.
