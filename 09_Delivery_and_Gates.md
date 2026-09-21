# N–Q. Performance, tests, migration and implementation sequence

## N1. Performance budgets — proposed, not measured

Initial engineering benchmark envelope: 100 concurrent authenticated browser sessions; 30 families; 10,000 open work items; 1 million archived episode records; 20 foreground read requests/second plus bursts; 100 workflow commands/minute; 50-row attention pages; at most 2,000 displayed samples/series and six simultaneous series per chart panel. Bulk raw manufacturing volume stays in source snapshots/artifacts, not the hot web path. Adjust this explicit envelope after measuring real users and source volume; do not claim the system supports an untested fab size.

Test on a recorded production-like deployment with documented CPU/memory/database/network characteristics. A proposed pilot test profile is one 4-vCPU/8-GB web node, a separate 4-vCPU/16-GB database service and independently budgeted workers, with <=50ms client round-trip. This is a benchmark assumption, not a capacity guarantee or infrastructure purchase recommendation.

### O10.2 benchmark execution contract

`tools/o10_performance_capacity.py` is the repository-owned CHG-161 harness
for the current W1 Attention/Episode path. It preserves this N1 envelope and
does not create budgets for absent product surfaces. Its percentile method is
nearest-rank (`rank=max(1,ceil(q*n))` on sorted samples), warm-up samples are
excluded, valid slow samples are retained, and expected typed conflicts are
reported outside successful latency samples. PostgreSQL fixture construction
is set-based and validates exactly 10,000 Attention/work rows, 1,000,000
`read_revision` rows and 30 scope/family partitions before timing.

The harness records real PostgreSQL plans, relation/index sizes, connection
settings, per-operation query counts, scheduled-versus-start delay, retained
secret-safe samples, browser useful-paint timings, application WebSocket frame
bytes, command receipt replay/read-your-write facts and repeated G10 scenario
results. A pure fail-closed evaluator consumes every acceptance fact before it
can return `PASS_CURRENT_SURFACE_BUDGETS`: direct PostgreSQL major 18,
10,000/1,000,000/30 fixture counts, the exact 100-session/30-second minimum
sample envelope, 100 distinct successful runtime session identities per
repetition, conflict/durability/resilience/restore gates, payload and browser
failure gates, and `production_disaster_rpo_rto_claim=NOT_ESTABLISHED`.

The executor authority must bind provenance, a measurement window, stable
non-secret web/PostgreSQL/worker resource identities, distinct resource
authorities/process-isolation domains, web/PostgreSQL limits, worker limits,
and <=50 ms RTT measured on the actual browser-client-to-web path. Exact
limits or limits within the documented lower bound and no-more-favorable upper
bound qualify; larger resources are more favorable and absurdly undersized
resources are unrepresentative. PostgreSQL 18 must be observed directly from
the benchmark database, not supplied only by an environment file. A developer
laptop, PostgreSQL 17, or an unvalidated hosted runner remains
`BLOCKED_BENCHMARK_ENVIRONMENT`; that state is not a source defect.

Unexpected fixture, SQL, browser, restore, serialization or harness failures
are `FAIL_BENCHMARK_EXECUTION`, and hard budget/acceptance failures in a
qualifying executor are `FAIL_CURRENT_SURFACE_BUDGETS`. Normal execution
returns exit 0 only for PASS, exit 2 for a blocked environment, and exit 1 for
either failure state. The explicit contract-only template is the only blocked
mode permitted to return exit 0.

The only O10.2 source repair justified by the diagnostic profile is bounded to
the existing retained-snapshot writer: snapshot members are sent through one
PostgreSQL cursor batch instead of one `execute` call per member. No new index,
cache, state authority, database product or eventual-consistency shortcut was
introduced. Existing scope, ordering, snapshot expiry, authorization and
coherence tests remain the governing regression boundary.

| Metric | Proposed target at stated load | Mechanism / measurement |
|---|---|---|
| Warm attention query | p95 <=300ms; p99 <=750ms | indexed scoped projection, cursor, bounded result; service trace |
| Initial useful attention page | p95 <=2s | first coherent rows before extras; browser timing, not server response alone |
| Episode decision brief | p95 <=500ms server; useful page <=2s | immutable snapshot references + small workflow join |
| Evidence/comparison panel | p95 <=1s when materialized | paged evidence and bounded chart payload; visible job state otherwise |
| Filter apply | p95 <=500ms end-to-end after debounce | server pushdown, latest-request guard, cached immutable summaries |
| Workflow command | p95 <=500ms; p99 <=1s, excluding external action | short transaction with receipt/outbox and read-your-write |
| Global search | p95 <=500ms | scope-first exact-ID/indexed text, <=20 results |
| New published revision visible | p95 <=10s foreground | shared refresh coordinator; background tabs may back off |
| Health publication after eligible source availability | p95 <=60s for qualified near-real-time family | recorded ingestion/compute/publish stages; not event-time guarantee |
| Hot response sizes | <=1MB episode initial payload, <=250KB attention default | bounded columns/points; no full source tables |
| Process crash durability | 0 lost acknowledged commands/effects | transactional commit/receipts; failure injection |

Source availability lag is reported separately from EPHI processing lag. An hour-late quality system cannot support a one-minute quality decision even if the UI is fast. The system must not show “real time” when only its page refresh is real time.

Operational availability targets are family/source-specific. Do not aggregate a running web server into “all qualified capabilities available.” Use recovery and source-age SLOs alongside web availability.

## N2. Scalability rules

Limit/push down lists; use explicit sorting with canonical-ID tie breaks. An attention cursor is bound to scope/filter/sort and a stable query snapshot; expired snapshot returns a refresh instruction rather than skipped/duplicated rows. A refresh intentionally moves to a new snapshot.

Materialize stable distributions/reference comparisons; downsample charts with a documented method that preserves extrema/anomalies needed for the view. Never downsample away evidence before scientific calculation. Avoid per-row/per-chart network queries and N+1 evidence loading. Cache immutable artifacts by hash and authorized scope; bound TTL/size for mutable reads. Do not eagerly render every tab or keep unbounded browser workspaces mounted.

Keep analytical CPU out of the UI event loop; the Base adapter may isolate short I/O via its async primitives, but long durable jobs use EPHI workers. Use profiling before replacing exact similarity or introducing distributed systems.

## O. Test and qualification architecture

| Gate | What must be proven | Evidence and blocking rule |
|---|---|---|
| G00 Source identity / installation | canonical Git commit/tree/worktree, package/dependency identity, import/launch on supported Python | repository facts + reproducible install; absent required library blocks target stage |
| G01 Existing regression | 275-pass baseline maintained except explicitly justified behavior corrections | full test log/JUnit, invariant suite; unexplained regression blocks |
| G02 Temporal integrity | no future-available input; revisions immutable; historical supersession correct | F04 regression plus replay/property tests; mandatory |
| G03 Recovery integrity | low confidence, missing/stale/pipeline-suspect inputs cannot establish recovery | F05 regression, repeated-sample/context tests; mandatory |
| G04 Work continuity | technical recovery cannot hide open engineering work; closure/reopen preserved | F03 regression, terminal-state/obligation matrix; mandatory |
| G05 Durable transactions | source/checkpoint consistency; CAS, idempotency, fencing, outbox, no partial publication | PostgreSQL integration with kill/restart/timeout/interleaving tests; no mock-only PASS |
| G06 Source and scientific qualification | units/IDs/times/context/coverage; detector/recovery behavior on representative family data | existing data reality/replay/golden/shadow plus new policy IDs; per-family scope |
| G07 Application contracts/security | query/command behavior identical through API/UI/CLI; scope isolation; receipt conflicts | contract + authorization + negative tests; mandatory |
| G08 Base integration | installed APIs/patterns/tokens, DataSource pushdown, no duplicate state authority | Base agent-check/gate/runtime contract + provider conformance |
| G09 Browser/visual/accessibility | real interactions, responsive states, keyboard, no unexpected console errors | browser artifacts/screenshots + human review; no source-test substitution |
| G10 Performance/resilience | budgets at explicit load, degraded sources, worker starvation, restore | load trace and repeated failure/restore evidence |
| G11 Value integrity | claim/event uniqueness; as-of corrections; attribution; rates; negative net value honest | unit/property/integration scenarios + reviewer workflow evidence |
| G12 Release promotion | EPHI release + Base pin + family/capability + current evidence + rollback readiness | fail-closed existing control-plane record and independent approval |

Tests must include units/null/NaN/infinite handling; DST/offset timestamps; late corrected events; no/weak/conflicting reference; contemporaneous/future control eligibility; duplicate material through rework; missing WIP; unknown alternatives; overlapping episodes/claims; new source mapping; stale plan; canceled check; concurrent assignment; old worker completing after lease loss; crash between artifact and publication; crash after local effect before external delivery; model/LLM outage; cross-user cache isolation; permission revocation during an open page.

The repository review adds the following explicit regression obligations. They are acceptance specifications, not executed application tests:

| Case | Expected result | Gate |
|---|---|---|
| Upstream available before cutoff, but ingested/published afterward | Excluded from AS_KNOWN; eligible SOURCE_REPLAY clearly labeled | G02 |
| New head/workflow commits between two brief reads | One coherent snapshot; historical reads never mix in current workflow | G05/G07 |
| Same command ID/payload arrives concurrently or after lost response | Exactly one local effect/audit/outbox set; same committed receipt after current authorization | G05/G07 |
| Same command ID with different command type, target or payload | Idempotency conflict, no second effect | G05/G07 |
| Analysis refresh races with ownership change | Current owner/work state survives publication | G04/G05 |
| Row ordering changes between attention pages | Stable retained row-version snapshot or explicit expiry; no silent skips/duplicates | G07/G10 |
| Grant revoked before receipt replay, page continuation, download or notification | No disclosure under the revoked grant | G07 |
| Correction changes value period; later approval; concurrent successor | Correct as-known totals and no branched or cyclic supersession | G02/G11 |
| Decimal rates/amounts and rounding boundaries | Exact approved rounding with no binary-float accumulation | G11 |
| Terminal job cleanup followed by retry/replay | Archived effect identity prevents duplicate application | G05/G10 |

Boundaries: domain policies use fast unit/property tests; adapters use contract tests against a real transactional backend; workers use deterministic fault injection; scientific methods use existing replay/golden qualification; UI uses Base component tests plus installed-browser interactions. An optional module skip is acceptable only for an explicitly excluded capability. Production columnar workflows require PyArrow/Polars integration qualification rather than importing successfully by accident.

## O2. Family scientific acceptance policy

Before shadow evaluation, freeze qualified context definitions, reference/peer eligibility, observation availability rules, false-alert workload budget, missed-event/lead-time requirements, recovery thresholds, source freshness and eligible actions. These are not safely inferable as one global threshold from the code. The generic product implements the contract; the first family provides values and evidence.

Do not tune on final holdout cases. Include normal/benign/regime-change/data-issue cases, not just known excursions. Report actionable precision with uncertainty, false alerts per engineer-shift, incident-level recall on reviewed eligible cases, lead time relative to actual availability, and unresolved/censored outcomes. Small samples remain “insufficient evidence,” not 100% validation. Correlated observations from one episode cannot inflate the case count.

Time-to-detect, time-to-acknowledge, active investigation effort, decision lead time and outcome maturity are different metrics. A late outcome must not appear available in an earlier replay. Root-cause correctness requires reviewed evidence, not user agreement with the displayed explanation.

## P1. Migration plan

**Baseline.** Record the canonical Git commit/tree/worktree and selected Base commit/dependency identities. Inventory supported runtime constraints and existing company configuration. Keep unrelated user work untouched. Save old API outputs and deterministic scientific fixtures. Mark demos/local stores as explicit development profiles. Historical source-audit provenance remains reference-only and is not a prerequisite for the canonical checkout.

**Corrective core changes.** Add regression tests for F03/F04/F05 before fixes. Replace recovery eligibility with a versioned qualified rule; implement as-of supersession; separate work visibility from technical state. Preserve legacy behavior only for comparison/replay where required, not as production fallback. Record scientific behavior differences and rerun affected qualification.

**Application insertion.** Extract/use existing advisory/history/control/value mappings through new typed use cases. Introduce query/command DTOs, principal/scope, UoW and receipt contract without moving scientific packages. v1 delegates via compatibility facade; v2 adds availability/cursors/revisions. Add import-boundary tests.

**Durable adapters.** Add migrations and production repository adapters. Import existing checkpoints/read sources into a staging namespace with original IDs as aliases and original version/evidence metadata. Do not derive missing provenance from current timestamps. Quarantine incomplete imports. Dry-run reconciliation checks counts, canonical IDs, hashes, workflow/closure state, ledger claims and temporal totals.

**Read comparison.** Run new projections in shadow from immutable input snapshots and compare old/new DTOs. Approved differences (e.g. recovered-but-open work) are named explicitly. Use one authority for user writes; avoid dual-writing two independent workflow stores. Transactional outbox can feed transitional read projections.

**Single-family cutover.** With a documented pause/high-water mark, finish in-flight writes, reconcile command receipts, switch the authoritative repository for the selected family and enable the new UI. No silent fallback to memory on database failure. Keep replay/old readers available for diagnostics only.

**Retire transitional code.** Delete legacy production mutation paths only after caller inventory, compatibility tests and cutover evidence. Keep demo fixtures. Do not delete mature scientific or qualification modules merely to shorten the tree. Remove duplicate state/cache authorities and dead API adapter code when no remaining caller depends on them.

## P2. Rollback

Feature flags operate per family/capability and separate visibility from compute. Rollback may disable new UI/actions and continue read-only access to last trustworthy state. Preserve accepted commands, evidence and ledger revisions. A scientific rollback creates a new versioned result from an approved prior engine; it never rewrites history. Schema rollback must not drop accepted new events. Prefer expand/contract migrations and forward correction. Do not roll back to a known false-recovery or false-as-of behavior in production.

## O9.1 CHG-147 operations boundary

The O9.1 candidate may add only the typed operations-health boundary, backup
identity/verification, isolated PostgreSQL restore rehearsal, deterministic
post-cutoff reconciliation, and operator runbooks. It reads the existing O2
command/receipt/audit/outbox, worker lease/effect, read/snapshot, artifact
catalog/filesystem, O3 workflow, and O4 source snapshot/capability authorities;
it does not create a parallel state authority, copy raw telemetry, switch
traffic, mutate Notion, or broaden into O11 release work.

Health is exposed as separate process/transport, PostgreSQL, immutable-artifact,
source capability/freshness, durable worker/job, and evidence/qualification
axes. Source absence remains `UNAVAILABLE` / `BLOCKED_REAL_SOURCE`. The backup
manifest records a PostgreSQL server-time cutoff/high-water and only reports
`VERIFIED` when required schema, dump identity, and every referenced immutable
artifact hash/size verify. Restore is isolated and never automatic.

Reconciliation reports durable identities accepted after the cutoff without
claiming they are in the restored snapshot. Local timing is labeled
`LOCAL_RESTORE_REHEARSAL`; `production_disaster_rpo_rto_claim` remains
`NOT_ESTABLISHED` until a later target-environment qualification.

## Q. Implementation waves in dependency/ROI order

Each wave ends with a healthy repository, a runnable vertical result, exact commands/status/evidence and a concise change log. No arbitrary sprint duration is asserted.

| Wave | Objective / user-visible result | Backend and frontend work | Migration / tests / exit |
|---|---|---|---|
| W0 — Trust baseline | Credible canonical repository baseline; integrity gaps remain explicit until their APIs exist | Git-native package/import/test baseline; pin Base; resolve actual catalog APIs; dependency lock; bounded W0 recovery policy; F02/F03/F04/F05 checks reported honestly | Canonical repository checks PASS; bounded F05 regression PASS without family production qualification; Base compatibility smoke when bootstrapped; no production claim |
| W1 — One durable case | Open, claim and revisit one episode in actual Base UI after restart | UoW/repos/receipts; minimal attention/read brief; Base shell/provider/context; real DB integration; scoped principal and health startup | Demo namespace only; crash/restart, concurrency, auth tests; no silent memory fallback; source unavailable states render |
| W2 — Real metrology shadow | One real family's qualified signals appear with provenance | Company read adapters, canonical snapshots, metrology pipeline workers, checkpoints/read heads, freshness; evidence/reference timeline | Data Reality/replay/golden policy; observer-only UI; record real baseline and discrepancies; target auth/durability prerequisites already satisfied |
| W3 — Complete decision loop | Engineer reviews exposure, performs checks, records action, verifies recovery, closes/reopens | WIP/route/alternative adapters if available; workflow/check/action/recovery/decision snapshots; exposure and forms; basic value observations | Family-scoped human pilot; all source-dependent capabilities explicit; F03/F05 browser tests; no autonomous controls; observed actions and recovery durable |
| W4 — Faster investigation | Top discriminating checks and useful analogues/RCA reduce reconstruction | Curated planner eligibility/ranking, history/RCA jobs, result schemas; comparison/matrix/next-check UX polish | Shadow compare planner suggestions; overlap/contradiction/invalid-source tests; measured task study; no fake probability |
| W5 — Prove and repeat | Reviewed outcomes/value and guided family qualification | Temporal ledger production repo/claim review; AutoPort and existing control-plane UI; search/handoff/notifications | F04/claim dedup/property tests; one audited real outcome or explicit pending; second-family onboarding rehearsal; avoid full admin-platform rebuild |
| W6 — Qualify V1 | Controlled production-ready first-family release within declared scope | Perf/index/queue tuning, browser/visual/accessibility closure, restore/rollback, operations runbooks and independent promotion | G00–G12 evidence current; real family source/science/target approval; remaining pending value not misreported as savings |
| W7 — Prove extensibility | Second family without generic-core edits unless a documented domain gap | New mappings/config and curated check/recovery templates; reuse all pages/services | Adapter/replay/golden/shadow/target gates; onboarding effort recorded; only now claim multi-family portability demonstrated |

W3 can include a limited metrology-only pilot when WIP is unavailable, but that pilot cannot claim the full exposure-prevention requirement or full R1-qualified V1. Likewise a dry-run claim demonstrates ledger function but is not realized manufacturing value. W6 declares only the capability scope actually supported by evidence.

### Detailed work-package rules

For every implementation task include requirement/invariant IDs, exact files to change, existing contracts to preserve, visible result, source/capability assumptions, migration, unit/integration/browser tests, evidence artifacts and rollback. A feature flag is not permission to leave production wiring unspecified. The implementation model must not mark tests PASS unless executed; absent infrastructure is NOT_RUN/PENDING, never “assumed pass.”

First implementation branch should contain W0 corrections and the W1 thin slice, not a large speculative directory tree with placeholders. Create modules when a real use case requires them. Complete one path from source/revision to UI command and durable result before multiplying pages.

## V1 exit statement

EPHI V1 is complete for a declared family/capability when a real source snapshot can produce qualified health intelligence, an explained prioritized work item and supported exposure assessment; a scoped engineer can investigate, record an approved external action, verify affirmative recovery, close/reopen with provenance and submit an outcome; another authorized reviewer can validate eligible value; state survives faults and the exact release passes source, family, UI, performance and operational gates. The outcome may legitimately be zero or negative. No invented savings are required to pass software functionality, and actual ROI requires the documented pilot evidence.
