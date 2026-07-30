# Historical Supply refactor evidence

The former `step_*` and memory-probe scripts documented earlier corrections and
resource investigations. Phase 10 deleted those one-off executables after
their conclusions were mapped to maintained tests below.

Phase 5B maps their conclusions to maintained tests as follows:

- carpool overlap grouping and touching-boundary behavior: Supply pipeline
  unit tests;
- 60-minute chain boundary and cross-midnight chain continuity: Supply pipeline
  unit tests;
- half-open interval slots and cross-midnight in-service coverage: Supply
  pipeline unit tests;
- `tau_idle` independence from `max_gap`: Supply pipeline unit tests;
- origin-only fleet attribution and driver deduplication: Supply pipeline unit
  tests;
- chunked/whole-frame equality and driver block invariants: Supply pipeline and
  Phase 5B equivalence tests;
- formal filenames and exclusion of old names/intermediates: Supply contract
  tests;
- historical memory probes: retained as resource evidence only; no memory
  threshold is promoted into the Supply business contract.

No historical Supply scratch runner remains executable. Git history and the
ignored analysis reports retain the original evidence when needed.
