# Demand platform baseline decision v1

Phase 9 provides evidence and options; it does not select a new canonical
Demand baseline.

## Evidence

The formal Linux full pipeline is byte/semantic exact with the Phase 5A Linux
Demand baseline, so the new runner and runtime Partition binding add no Demand
difference. The Linux result still differs from historical Windows Demand for
251,455 orders (`0.546609%`) at overlapping equal-distance segment candidates.
Totals, services, time axis, cluster index and tensor total remain equal, but
individual assignments, OD cells and POI edges differ.

Supply and TTE reproduce their pipeline outputs exactly in independent Linux
standalone runs when both use the same full-run Demand. Phase 5B/5C separately
proved exact downstream reproduction when the historical Demand input is used.

## Option A: retain historical Windows canonical

- Keep `data/processed/fifth_ring` unchanged.
- Treat the Linux full run as a platform validation baseline only.
- Permit new pipeline execution and validation, but block real publish.
- Revalidate the pipeline in the historical Windows environment when available.

This is the current safe operating state and requires no data replacement.

## Option B: approve Linux as a new canonical

- Requires explicit user approval; Phase 9 does not grant it.
- Record the complete assignment/OD/POI difference summary.
- Regenerate Supply and TTE from Linux Demand.
- Review effects on paper results and downstream consumers.
- Require an additional publish confirmation after that review.

## Option C: develop deterministic spatial assignment v2

- This is an algorithm behavior change, not mechanical refactoring.
- Develop it as a separately versioned experiment with impact analysis.
- Compare it independently with both Windows historical and Linux current
  baselines; it is not automatically equivalent to either.
- Do not implement it by silently changing tie-breaks or tolerances in the
  current pipeline.

## Current decision state

No option has been selected by the user. Real publish of the Linux full run is
therefore blocked by `demand_platform_baseline_decision`. The formal processed
scope remains the historical baseline.
