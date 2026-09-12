# Incident 34621592918: profit-lock transient persistence failure

Baseline: `847fd91d3383caad6387767622213b8b6d29e432` (latest main at start).
The GitHub job `103336640744` failed at 2026-09-11 16:50:07 UTC,
during cycle 6/12. `profit_lock_watch -> load_strategy_runtime ->
load_strategy_parameters` raised PersistenceError on Supabase HTTP 504.
The post-audit returned PASS_PHASE6_READONLY, empty open positions and
empty reconciliation issues. The unnecessary pre-position strategy read
made an otherwise flat protection tick dependent on global persistence.

## Remediation

- Shared READ-only retry: 408/425/429/500/502/503/504 and explicitly listed
  transient httpx transport failures. Maximum three attempts, exponential
  250ms/500ms backoff plus 0–100ms jitter; HTTP phase timeout 2s. Serialized
  Linux SIGALRM enforces a hard 12s READ deadline, including slow bodies.
  Complete entry-context resolution shares that deadline. No background
  worker or retry of exchange/database writes is introduced.
- Flat watch validates the safety contract, execution arm and config, then
  checks Binance positions before constructing persistence/public/writer
  clients. It returns PASS_NO_POSITION / NOT_REQUIRED_FLAT without a
  strategy state query or exchange write.
- Open positions use existing signal evidence strategy_params, validated
  with StrategyParameters and the canonical strategy-id hash. Position,
  signal and geometry identities must match. Missing legacy snapshots are
  ineligible for dynamic ratcheting; malformed snapshots hard fail. Mutable
  global strategy parameters cannot override the entry policy. Lifecycle
  maintenance uses the same entry policy.
- Exhausted context reads preserve STOP and TP2, emit an explicit degraded
  decision and stop further dependency queries that tick. Next tick retries.
  No fallback to defaults, no widening, no cancellation on read failure.
- Runner-local atomic health state survives separate CLI processes, marks
  three consecutive degraded ticks as sustained outage, and blocks new
  execution. It stores no strategy or credentials. Structured stdout carries
  RUNNING/DEGRADED/RECOVERED. Existing DB heartbeat taxonomy is preserved:
  SCANNER_CYCLE uses BLOCKED plus management details during degradation;
  unhandled failures retain FAILED.
- New entry rechecks authoritative promotion/strategy after Fast Watch;
  changed/unavailable state or a degraded management latch prevents entry.
  Only typed READ exhaustion is handled in the managed cycle wrapper;
  integrity, unexpected safety, and write failures remain hard failures.
- Runtime concurrency group is unchanged; push no longer cancels an active
  writer. Existing Phase-6 pre/post audits and supervisor stay enabled.

## Deliberate LKG decision

No LKG management cache is introduced. During an outage, exchange-side
protectors remain authoritative; dynamic ratcheting waits for validated
entry context. Thus LKG expiry/corruption/wrong-id tests are not applicable.
This avoids creating a second policy source or accepting an uncertain trade
identity during an outage. New entries never use cached strategy state.

## Validation

Local focused fault tests and full suite are recorded in the PR. Tests cover
transient recovery/exhaustion, permanent failures, malformed payloads,
12-tick incident continuation, real protected-position no-write degradation,
entry A versus global B, monotonic tightening, unchanged TP2, hard deadline,
no write retries, and persistent health transitions. CI uses Python 3.12/3.13.
Real Demo run IDs and final Phase-6 evidence must be attached after rollout;
unit tests alone do not establish runtime acceptance.

Dedicated Supabase connector SQL access was denied for project
`rlrfnkckqxkinzgawpql`; no database writes or schema migrations were made.
