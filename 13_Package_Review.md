# Package review and implementation readiness

Review date: 2026-09-15 America/Chicago. Design revision: 0.2.

## Assessment

The package is a substantial implementation design with useful separation of science, operational urgency, human workflow and economic evidence. Its strongest choices are immutable decisions, qualified affirmative recovery, durable commands and a single canonical episode workspace. Its historical audit includes concrete synthetic observations and appropriately limits the meaning of portable tests.

It is ready to serve as a versioned design repository and now contains the smallest installable canonical application boundary plus the scoped W0 F02/F03 advisory/workflow integrity slice. It is **not a qualified release**. The canonical package contains no broad UI, company adapters, production storage or scientific implementation. It is a new repository implementation; it is not a reconstruction of the historical audited application.

The repository root is `ephi`. The split chapters are the maintained design, and an absent companion master is not a build input. Historical archive/source facts remain preserved as `REFERENCE_ONLY` provenance; they are not required for checkout, installation or the canonical W0 path.

## Corrections in this review

P-identifiers below concern the package and contract review. Historical F-identifiers continue to describe the original application's findings.

| ID | Finding and correction | Requirements / gates |
|---|---|---|
| P01 | No repository entry point or repeatable package validation. Added README, contributor/agent instructions, a standard-library checker, regression tests and CI. | T4; package checks only |
| P02 | Original manifest mixed historical audit state with delivery claims and referenced an absent companion. Preserved it under `evidence/import/`, added current hashes, separated historical from current evidence, and added the canonical Git-native identity. | T1/T4; G00 canonical baseline |
| P03 | “One transaction” did not specify a coherent multi-statement read or prevent competing projection writers. Specified a single statement/repeatable-read snapshot, shared episode locking and owned-field updates. | R2/R3, I10/I12; G04/G05/G07 |
| P04 | Receipt lookup alone left concurrent first attempts and lost responses ambiguous. Added unique-receipt rollback/re-read behavior, payload identity, same-ID retry semantics, current authorization and archived deduplication identities. | T2, I10; G05/G07/G10 |
| P05 | Source availability could be confused with knowledge actually published in EPHI. Defined AS_KNOWN, SOURCE_REPLAY and RESTATED modes with a late-ingestion example and pinned historical workflow. | T1, I01/I02/I07; G02 |
| P06 | Optional expected/viewed revisions left decision-sensitive write preconditions unclear. Required versions for existing aggregates and revalidation of decision prerequisites, while preserving honest historical observations. | R5, T2; G03/G05/G07 |
| P07 | A stable cursor lacked a retained row-version mechanism. Required immutable query snapshots, current permission checks and explicit expiry without browser-long database transactions. | R2, T3; G07/G10 |
| P08 | Ledger correction rules needed period-movement, approval-time, chain and monetary precision semantics. Specified active-leaf selection before event-period filtering, constrained successors and exact decimals. | R7, I11; G02/G11 |
| P09 | Generic downstream “action” delivery could be interpreted as a manufacturing control path. Restricted V1 delivery to approved work requests/tickets/notifications and retained human external execution. | R1/R5, ADR 10; G07/G12 |
| P10 | Permission lifetime on receipts/artifact links/notifications needed explicit treatment. Required authorization at disclosure/dispatch, added concrete regression obligations, and kept framework inspection distinct from installed API qualification. | T2/T4; G07/G08/G09 |

The affected application behavior is still proposed. CHG-118 adds only the bounded canonical F05 recovery integrity slice; it does not qualify a production family or implement the broader recovery plan.

## Evidence and verification

| Check | Result and scope |
|---|---|
| ZIP integrity and original manifest | PASS: archive CRC check; all 22 listed files matched size and SHA-256 before edits |
| Original evidence preservation | PASS: all nine historical artifacts retain their imported hashes |
| Historical audit consistency | PASS: 409 inventory records; 237 core / 18 company / 147 test Python files with reported line counts; 53 explicit API routes; JUnit/log agree on 275 passed / one skipped |
| NiceGUI Base pin | PASS for source inspection: pinned public commit and metadata confirm version 3.0.0a8, NiceGUI 3.15.0 and Python >=3.11,<3.14; file hashes saved in the review evidence |
| Current package checks | PASS when run on a supported interpreter: `python3 tools/check_package.py`; integrity, syntax, local links/fences, traceability presence and historical consistency |
| Checker regression suite | PASS: 12 tests via `python3 -m unittest discover -s tests -v`; changed/missing/extra files, damaged evidence, invalid syntax, broken links and other rejection paths |
| Canonical application self-check and repository tests | PASS/IMPLEMENTED capability for F02/F03/F04/F05; fresh F02/F03/F04/F05 execution is separately reported by `tools/w0_integrity_regressions.py` |
| Installed Base, browser, company sources, persistence and scientific qualification | NOT_RUN: no implementation/target environment in this package |
| Original mission completeness | NOT_VERIFIABLE: original attachment absent; internal A–S and requirement traceability retained |

Exact import/review identities are recorded in [the review evidence](evidence/review/package_review.json). Current file identities are in [manifest.json](manifest.json). CI evaluates package checks for the exact commit; it does not run the historical application's test suite. Source inspection cannot establish installed constructor compatibility or browser quality.

## Implementation blockers and next slice

1. **Keep the canonical baseline reproducible.** Run the Git-native baseline, package checks and offline tests from a fresh checkout; do not add an artifact prerequisite.
2. **Resolve framework and dependency bindings.** Use the pinned catalog, exact dependency declarations and the existing CHG-105 runtime tool; installation/bootstrap is separately reported from offline repository checks.
3. **Continue beyond the bounded F05 slice.** Add one scoped durable claim/acknowledge path through the actual Base UI with conflict/restart evidence. Do not treat the W0 recovery fixture as family qualification or multiply screens before that works.
4. **Bind company-specific evidence before a pilot.** Identity, backend/artifact store, source mappings, family thresholds, action authority and operational policies remain the gates already defined in the design.

No browser, scientific or production-readiness claim follows from publishing this repository. The next authorized implementation can proceed from [11_Developer_Start.md](11_Developer_Start.md) against the canonical package.

## CHG-123 PostgreSQL reference evidence

CHG-123 adds a storage-neutral command transaction boundary, an optional
`psycopg[binary]==3.3.6` PostgreSQL adapter, the narrow numbered command-core
migration and DSN-gated real-connection tests. The integration path is
reference/integration evidence only: it is not a bound company database, does
not complete G05/O2, and does not add worker leasing/fencing, retained query
snapshots, notification delivery, company identity integration or O3 product
behavior. The ordinary package matrix and canonical W0 baseline remain
database-independent.

## CHG-126 PostgreSQL worker reference evidence

CHG-126 adds the storage-neutral durable worker/job contract, PostgreSQL
`job`/`applied_effect` migration, short `SKIP LOCKED` claims, server-time
leases, fencing epochs, bounded retry/defer/cancel transitions and atomic
LOCAL effect receipts. Its real PostgreSQL 18.x tests prove takeover,
stale-worker publish rejection, crash/retry reconciliation, database-time
eligibility and overlapping claims on separate connections. This is
reference/integration evidence only: it is not a bound company production
deployment, does not complete full O2/G05, and does not implement job
handlers, O3 Attention/claim/acknowledge behavior, NiceGUI pages, retained
query snapshots, company bindings, notifications or production readiness.
CHG-129 adds the separate generic read/snapshot foundation described below;
this worker evidence remains independently green and reference/integration
scoped.

## CHG-129 PostgreSQL read/snapshot reference evidence

CHG-129 adds only the generic read foundation needed before O3: storage-neutral
immutable revision/current-head/bundle contracts, PostgreSQL `read_revision`,
`read_head`, `query_snapshot` and `query_snapshot_row` tables, immutable-row
constraints, CAS publication, repeatable-read coherent current reads, exact
historical workflow snapshots, bounded retained row-version pages, database
clock expiry and integrity-checked cursors. Current reads combine the
immutable analytical head with the live workflow aggregate and effective
workflow version, while historical reads remain pinned to their stored
workflow snapshot. Focused PostgreSQL 18.x evidence covers separate-connection
read races, revision/history immutability, stale head rejection, snapshot
isolation under mutable fixture changes, current authorization/security-
revision revalidation, tamper/identity failures, missing retained members and
restart-query expiry.

At the CHG-129 baseline, this was generic PostgreSQL read/snapshot foundation
evidence only. It did not implement `ListAttention`, `GetEpisodeBrief`, Claim/Acknowledge
commands, NiceGUI pages, browser state, company identity adapters, scientific
source queries, notifications or production readiness. O3 will bind these
primitives to the first durable Attention → Episode UI slice. Historical
evidence remains `REFERENCE_ONLY`, and PostgreSQL evidence remains additive to
the database-independent package/full-suite/W0 checks.

## CHG-133 generic immutable-artifact foundation

CHG-133 adds the narrow immutable-artifact boundary required by the maintained
contracts: exact-byte SHA-256/size identity, scoped metadata/reference
contracts, separate blob/catalog/service ports, atomic file-backed reference
storage, an idempotent PostgreSQL scoped catalog, current-authorization
retrieval, verified reads and publish preconditions. The file adapter rejects
implicit or memory roots, caller-selected paths and unsafe symlink/path
substitution, and uses a bounded reference-only size limit. The PostgreSQL
catalog stores no unrestricted filesystem path, public download URL or signed
URL; its server-recorded rows are immutable and scoped.

This is generic foundation evidence only. It does not bind the approved
company immutable object store, implement scientific source artifacts, Episode
evidence or upload UI, provide browser download transport, add external SDKs
or credentials, select retention policy, issue signed links or claim
production readiness. Artifact PostgreSQL tests are additive to the existing
command, worker, coherent-read and retained-snapshot integration suites; the
ordinary package/full-suite/W0 checks remain database-independent.

## CHG-134 O3.1 W1 candidate

The O3.1 candidate adds only the durable Attention → Episode W1 vertical slice.
`ephi.application.attention` validates the bounded query vocabulary and
delegates retention, cursor integrity, expiry and current authorization to the
existing read-snapshot authority. `ephi.application.episodes` constructs one
coherent Episode brief from the existing current/historical bundle contract.
`ephi.application.workflow` delegates claim/acknowledge to the existing
`VersionedAggregateCommandExecutor`, preserving expected-version CAS, viewed
revision binding, receipt replay/idempotency, audit and outbox atomicity.
PostgreSQL adds only the indexed Attention projection; workflow/read truth
remains in the O2/O2-read tables.

The installed NiceGUI Base public catalog was used for the runtime, shell,
workspace, lifecycle/stale-response, state, table/master-detail,
analysis-workspace and DataSource composition. The application does not import
`nicegui.ui`, private Base integration internals or copied framework/demo
implementations. Runtime-contract and browserless framework smoke are PASS;
application/browser/production qualification remain dependent on explicit
PostgreSQL, identity, source and browser evidence. `tests/test_o3_postgresql.py`
is the real-PostgreSQL restart/concurrency/atomicity evidence and is
`NOT_RUN` when `EPHI_TEST_POSTGRES_DSN` is absent.

## CHG-167 O5.1 unified decision-loop core

The O5.1 candidate keeps `episode_workflow` as the sole durable Episode
workflow/work-state aggregate. Its nested `decision_loop` extension stores
checks, action/reconciliation facts, locked recovery evidence, closure and
reopen history under the same version and CAS. O5 has no create-if-missing
aggregate path, parallel work-state field, second receipt/audit/outbox path or
new authorization authority. The closure contract requires an exact locked
PASS plan bound to a qualifying current-cycle action and post-action evidence;
technical PASS leaves O3 work open until `CloseEpisode` commits.

Offline unified-authority evidence is in `tests.test_o5_decision_loop`.
`tests.test_o5_postgresql` is the additive DSN-gated PostgreSQL 18.x evidence
and is explicitly executed by the existing PostgreSQL CI lane. No migration is
needed because the repair extends the existing aggregate JSON authority.

## CHG-169 O5.2 decision snapshot and handoff candidate

The O5.2 candidate adds migration 007 for immutable bounded decision snapshots,
deduplicated handoff intents, delivery status/attempt records and the narrow
indexes required to query them. It does not add a workflow, outbox, queue or
authorization authority: snapshots bind the existing `episode_workflow` row
and viewed revisions, projection reads only committed `outbox_event`, and
delivery uses the existing `job`/lease/fencing/`applied_effect` substrate.
Recipient and channel resolution is represented by qualification-only generic
ports and deterministic in-app adapters; real company email/Slack/Teams/SMS/
PagerDuty bindings are not implemented. Current authorization is checked before
snapshot/status/deep-link reads and recipient authorization is re-resolved at
dispatch. External delivery is at-least-once and ambiguous outcomes remain
UNKNOWN until explicit reconciliation; exactly-once external delivery is not
claimed.

Focused offline evidence is in `tests.test_o5_handoff`. Real PostgreSQL 18.6
evidence covers immutable snapshots, stale revision rejection, outbox
projection races, deduplication, recipient revocation, fencing, bounded retry
failure, restart durability and UNKNOWN reconciliation in
`tests.test_o5_handoff_postgresql`. No UI or NiceGUI Base file is changed.

## CHG-105 W0 runtime delta

The independent framework/dependency qualification slice is recorded separately in [the runtime evidence](evidence/review/nicegui_base_runtime_evidence.json) and linked from [the NiceGUI Base binding manifest](evidence/review/nicegui_base_binding_manifest.json). It verified an isolated Python 3.11.7 environment, the exact NiceGUI Base VCS commit/version, exact `nicegui==3.15.0`, and 21/21 CHG-104 public-root authority imports. All six requested installed discovery commands returned machine-readable output. `runtime-contract` and browserless framework `runtime-smoke --port 0` PASS; application/browser/production qualification remain NOT_RUN. The canonical package self-check and Git-native baseline are the current W0 execution targets.
