# Historical Supply refactor evidence

The retained `step_*` and memory-probe scripts document earlier corrections and
resource investigations. They are historical evidence, not formal APIs.

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

The historical scripts are not deleted or edited in Phase 5B. Several still
reference old filenames or a former `EXECUTION_MODE` constant and therefore
must not be treated as current smoke tests without a separate archival cleanup.
