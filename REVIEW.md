# Review guide — Disaster-Aware Intelligent Routing System

This document is for **examiners, patent counsel, and technical reviewers**. It states what is novel, what is measured, and how to reproduce results without reading the entire codebase.

## Problem statement

During floods, landslides, and cyclonic wind, static “avoid red zone” maps fail because:

1. Hazards **move in time** (rainfall nowcast, rising water, wind bands).
2. Safety depends on **when the vehicle arrives**, not when the trip starts.
3. The same water depth is not equally dangerous for a **two-wheeler vs truck**.
4. Crowd reports and CAP alerts must **fuse with forecasts** without one false report closing a city.
5. LLM assistants **hallucinate** safe routes unless every claim is checked against live geometry.

## Architecture (modules M1–M7)

| Module | Contribution | Reviewer hook |
|--------|--------------|---------------|
| **Evidence fusion** | Time-decayed CAP/SACHET, sensors, crowd; Beta reputation; noisy-OR log-odds update of M1 | `fusion.py`, `/evidence`, crowd auto-approve at ≥3 corroborations |
| **M1** | Two-stage terrain prior + temporal GBM; **isotonic calibration**; multi-level flood exceedance heads | `hazard_model.py`, `GET /model/metrics`, evaluation **E1** |
| **M2** | Probability-parameterised polygons (depth bands, landslide cut-off, wind buffer) | `geometry.py` |
| **M3** | Spatio-temporal index + ETA intervals + vehicle thresholds + chance constraint P_safe ≥ α | `feasibility.py`, evaluation **E2** |
| **M4** | Detours via hull waypoints — **no graph re-preprocessing** | `detour.py`, **E3** |
| **M5** | Incremental trip re-validation on raster diff; anti-herding capacity term | `trips.py`, **E4–E5** |
| **M6** | LLM proposes structured claims; **deterministic gate** renders safety text from templates | `assistant/gate.py`, **E6** |
| **M7 (new)** | **Auditable decision records**: narrative + blockers + counterfactual departures + M1 attribution at blockers | `explain.py`, `GET /routes/{id}/explain`, `GET /forecast/point` |

## Novelty claims (for IP / publication)

1. **Arrival-time feasibility** — hazard slices queried only where the route segment overlaps the **predicted arrival interval**, not “hazards visible at departure” (baseline B1 in evaluation).
2. **Calibrated probabilities in routing** — hard blocks and product-form soft risk with configurable α; depth exceedance heads aligned to **per-vehicle wading limits**.
3. **Engine-agnostic detour synthesis** — iterative OSRM/local re-query with buffered obstruction hull (M4).
4. **Verified multilingual guidance** — safety-critical strings never come from the LLM; only template-rendered, gate-checked values (en/hi/ta).
5. **M7 explainability without a second ML model** — deterministic record linking M3 evidence to **local M1 feature attribution** and **counterfactual departure** scans (audit trail for regulators).

## How to run the demo (15 minutes)

```bash
cd safety-engine
pip install -r requirements.txt
uvicorn safe_engine.api:app --port 8000
# Browser: http://localhost:8000
```

1. **Route tab** — set start/destination, vehicle, click *Find a safe route*.
2. Expand **“Why this decision?”** — shows narrative, blockers, leave-later counterfactuals, baseline comparison.
3. **Ask tab** — questions are tool-grounded and gate-verified.
4. **Review tab** — approve pending crowd slices; watch trip re-validation counts in the footer.
5. Advance simulated storm (+15 / +30 min) and re-plan to see time-aware behaviour.

## Reproduce evaluation numbers

```bash
cd safety-engine
python -m safe_engine.train          # ~45 s, writes models/hazard_model.pkl
python -m evaluation.run_eval        # ~25 min, writes evaluation/results/RESULTS.md
pytest -q                            # regression tests including M7
```

All metrics in `evaluation/results/RESULTS.md` are **simulation-only** (synthetic terrain + hidden ground-truth storm). Label them as such in any thesis or report.

## API surface (production-oriented)

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Liveness |
| `GET /status` | Mode, hazard version, timings, active trips |
| `POST /plan` | Assessed routes + **decision_record** for chosen route |
| `GET /routes/{id}/explain?lang=` | Full M7 record (audit export) |
| `GET /forecast/point` | Cell forecast + attribution |
| `POST /assistant/ask` | M6 verified answers |
| Admin CAP/closures/pending | Operations |

Environment: see `safety-engine/README.md` (`ENGINE_MODE`, `ENGINE_KEY`, `DEM_PATH`, `GROQ_API_KEY`).

## Known limits (honest scope for reviewers)

- Default **scenario mode** uses synthetic storms; live mode needs DEM, OSRM, and retrained M1 on real flood labels.
- Original Spring **B3 Random Forest** on city weather CSV is retained only as a negative baseline (labels uncorrelated with features).
- Hindi/Tamil template strings should be reviewed by native speakers before public deployment.
- M1 attribution uses efficient perturbation, not SHAP — sufficient for audit demos, not for legal causality claims.

## Suggested review questions

1. Does M3 reduce **false refusals** vs B1 while keeping unsafe recommendations low? → **E2** table.
2. Does M5 re-evaluate **≪100%** of trips on a localised closure? → **E4** closure row.
3. Does the gate block **fabricated route ids**? → `tests/test_engine.py::test_gate_rejects_fabricated_and_stale_claims`.
4. Can a reviewer **audit** why a route was chosen without trusting the LLM? → M7 `decision_record` / `/explain`.
