# Implementation notes for the patent specification and project report

**Confidential — pre-filing.** This maps the draft claims (IDF item 9 / report §5.3) to code. It also lists
what changed during implementation, so the complete specification can describe the invention as built.

## Claim-to-code map

| Draft claim element | Implementation |
|---|---|
| (a) origin, destination, departure time, vehicle profile with passability threshold | `api.PlanReq`, `vehicles.PROFILES` |
| (b) calibrated probability per hazard class, grid cell and horizon | `hazard_model.TwoStageCalibrated`, `HazardModel.predict`, `train.py` |
| (c) class-specific terrain transform → time-sliced polygons with validity interval, severity, intensity | `geometry.generate_slices` |
| (d) spatio-temporal index keyed on bounding box and validity interval | `slices.SpatioTemporalIndex` (time buckets × STRtree, prepared geometries) |
| (e) candidate routes from a preprocessed-graph engine; per-segment arrival interval | `routing/osrm.py`, `routing/local_graph.py`, `feasibility.chunk_route` |
| (f) retrieve only slices overlapping the arrival interval and bbox; compare intensity with threshold | `feasibility.FeasibilityEvaluator.assess` |
| (g) all segments feasible and route-level probability ≥ α | same, `p_safe` over distinct risky slices |
| (h) transmit route and safety margin | `service.plan` → console verdict "N min to spare" |
| Claim 2: via-waypoints outside buffered blocking polygon, no graph modification | `detour.DetourSynthesizer` |
| Claim 3: reverse index, changed region, re-evaluate only affected trips, hysteresis | `trips.TripRegistry.affected`, `slices.HazardStore._raster_parts`, `service.on_new_version` |
| Claim 4: class-specific transforms | `geometry.py` (see change 1 below) |
| Claim 5: CAP + crowd + sensor fusion with time-decaying confidence | `fusion.EvidenceFusion`, `fusion.apply_evidence` |
| Claim 6: two-wheeler / car / emergency profiles | `vehicles.py` |
| Claim 7: offline region pack, on-device evaluation with staleness penalty | `service.offline_pack`, `offline.py`, `stale=True` in `assess` |
| Claim 8: system | whole repository |
| Claim 9: verified conversational guidance | `assistant/tools.py`, `assistant/gate.py`, `assistant/__init__.py` |
| Claim 10: LLM free text → pending hazard polygon | `gate.verify` → `hazard_report` → `service.report(via="llm")` |

## Changes made during implementation (update the specification)

1. **Flood intensity is now a set of depth-exceedance probabilities.** The first version used a
   physics depth estimate as the slice intensity. Evaluation showed this missed deep water for high-clearance
   vehicles, because the estimate under-predicts. M1 therefore has one calibrated head per depth level
   (0.15 / 0.25 / 0.40 / 0.60 m), and M2 builds one polygon layer per level. Each polygon carries
   P(depth > level), and its intensity is the next level up. A vehicle with wading limit W is thus tested
   against the probability of exceeding the largest level not above W. This is a stronger, more specific
   claim element, and it should become a dependent claim:
   *"wherein the hazard model outputs, for each of a plurality of depth levels, a calibrated probability
   that water depth exceeds the level, and the feasibility test uses the probability for the largest level
   not exceeding the vehicle's wading limit."*
2. **Elevated segments.** Bridges and flyovers (`Route.elevated`) are exempt from ponding but not from wind
   or closures. Without this, river floodplain flooding blocked every crossing. Suggested dependent claim.
3. **Each risky slice is counted once per route** in P_safe. Counting it per chunk double-counted risk,
   which a unit test caught.
4. **Changed region is computed on the model raster** (cell-wise band codes per time bucket) rather than by
   polygon symmetric difference. This is exact and about 10–20× faster, and it is equivalent in effect.
5. **Conservative default operating point:** P_MIN 0.10, p_hard 0.30, α 0.97, chosen on the
   calibration storm. The specification should present the safety/availability trade-off (E2b) rather than
   a single number.
6. **Capacity-aware selection (anti-herding)** is implemented: segment-hour load counters plus the cost
   term γ. It is a candidate for a further dependent claim or a separate filing.

## Honest limits to state in the report

- All quantitative results are from **simulation**: synthetic terrain, storms and ground truth. Field
  validation with Indian data is still required (README "Moving from simulation to real data").
- OSRM does not return bridge flags by default, so elevated-segment handling currently works with the
  built-in engine only.
- Hindi and Tamil templates and SOP paraphrases need native-speaker and official-text review.
- The independence assumption in P_safe is a simplification.
