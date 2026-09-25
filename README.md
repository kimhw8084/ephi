# EPHI

Engineering decision support for equipment, process and measurement-system health.

**Status: canonical repository baseline present; behavioral, browser, scientific and production qualification are not complete.** This Git repository is the sole executable source of truth for the new canonical EPHI implementation. It is intentionally a small package/application boundary, not a reconstruction of the historical audited application.

EPHI's intended workflow is **Detect → Explain → Prioritize → Contain → Investigate → Act → Verify recovery → Learn → Prove value**. Manufacturing actions remain human controlled in approved external systems.

## Start here

- [Design overview](00_START_HERE.md): product choices, scope and provenance boundaries.
- [Package review and readiness](13_Package_Review.md): current repository state and qualification limits.
- [Developer handoff](11_Developer_Start.md): install, import, self-check and the first authorized slice.
- [Downstream integration ABI](docs/Downstream_Integration_ABI.md): one-way private integration, provider contracts and conformance preflight.
- [Delivery gates](09_Delivery_and_Gates.md): acceptance criteria from baseline through a qualified family release.
- [W0 baseline specification](environment/w0_repo_baseline.json): machine-readable canonical identity and checks.

## Contents

| Document | Purpose |
|---|---|
| [01 · Product and architecture](01_Product_and_Architecture.md) | Requirements, boundaries and invariants |
| [02 · Historical source audit](02_Source_Audit.md) | Observed strengths and F01–F14 findings |
| [03 · Application contracts](03_Application_Contracts.md) | Queries, commands, revisions and concurrency |
| [04 · Data and runtime](04_Data_and_Runtime.md) | Persistence, time, jobs and recovery |
| [05 · UX and interaction](05_UX_and_Interaction_Design.md) | Navigation, workspaces and degraded states |
| [06 · Investigation and recovery logic](06_Investigation_and_Recovery_Logic.md) | Planner and scientific recovery rules |
| [07 · NiceGUI Base integration](07_NiceGUI_Base_Integration.md) | Pinned framework and integration boundaries |
| [08 · Security, notifications and AI operations](08_Security_Notifications_AI_Operations.md) | Authorization and operational controls |
| [09 · Delivery and gates](09_Delivery_and_Gates.md) | Tests, migration, rollout and rollback |
| [10 · Traceability and decisions](10_Traceability_and_Decisions.md) | Requirements, decisions and unresolved bindings |
| [11 · Developer start](11_Developer_Start.md) | First implementation slice |
| [12 · Sources and evidence](12_Sources_and_Evidence.md) | Provenance and reproducibility limits |
| [13 · Package review](13_Package_Review.md) | Repository preparation and corrections |
| [Downstream Integration ABI v1](docs/Downstream_Integration_ABI.md) | One-way private integration and conformance boundary |

The numbered chapters are the maintained design. [manifest.json](manifest.json) records current file hashes. [Historical evidence](evidence/README.md) is preserved separately from [review evidence](evidence/review/package_review.json).

## Install and identify the canonical application

Use a supported Python interpreter (`>=3.11,<3.14`) from a fresh Git checkout. Installation resolves the exact CHG-105 framework authority and NiceGUI pin declared in `pyproject.toml`; dependency/bootstrap execution is separate from offline repository tests.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m ephi --self-check --json
```

The self-check reports package identity, runtime/config identity, the pinned framework identity and F02–F05 capability availability. It does not claim historical source identity, byte identity, algorithm equivalence or historical-test equivalence; executed F02–F05 evidence comes only from the integrity runner below.

## Validate the repository offline

No chat attachment, local source artifact, staged-source directory, application credential or network access is required for the normal repository checks:

```bash
python3 tools/check_package.py
python3 tools/w0_repo_baseline.py
python3 -m unittest discover -s tests -v
git diff --check
```

`w0_repo_baseline.py` records Git commit/tree/worktree facts, the supported Python requirement, exact declared dependency identities, package/import identity, manifest integrity and deterministic test results. Its historical `275 passed / 1 skipped` value is `REFERENCE_ONLY` and is never used as a current canonical result.

## CHG-109 integrity semantics

The active [CHG-109 contract](evidence/review/w0_integrity_regression_contract.json) now targets `src/ephi`. Run:

```bash
python3 tools/w0_integrity_regressions.py
```

The runner verifies preserved historical probe identities, runs the canonical self-check, and executes fresh F02/F03/F04/F05 scenarios against `src/ephi`. F05 uses only the bounded in-memory qualified-recovery API and a deterministic W0 regression policy; it is not family production qualification. The old staged-source behavior remains only behind the explicit `--legacy-source` compatibility option for historical fixture coverage.

## CHG-121 O2 reference transaction substrate

The first reusable O2 slice is available under `ephi.application` and
`ephi.infrastructure`: typed server-derived command context, deterministic
semantic payload hashing, and a file-backed `sqlite3` reference adapter with
versioned aggregate state, command receipts, append-only audit events and a
durable outbox. Focused evidence can be run with:

```bash
python3 -m unittest tests.test_o2_transactions -v
```

This adapter is development/integration evidence for local durability and
compare-and-set semantics only. It rejects `:memory:` and has no fallback
store. CHG-123 adds a storage-neutral command UoW and an explicit-DSN
`PostgreSQLReferenceTransactionAdapter` under the `postgres` optional extra,
with numbered migration `migrations/001_o2_command_core.sql` and
DSN-gated tests in `tests/test_o2_postgresql.py`:

```bash
python3 -m pip install 'psycopg[binary]==3.3.6'
EPHI_TEST_POSTGRES_DSN='postgresql://user:password@host:5432/database' \
  python3 -m unittest tests.test_o2_postgresql -v
```

Without an explicit `EPHI_TEST_POSTGRES_DSN`, PostgreSQL tests report
`NOT_RUN`; the ordinary package matrix, canonical baseline and SQLite tests
remain database-independent. This is PostgreSQL reference/integration
evidence, not a bound company production database, and does **not** complete
G05 or claim O2 complete. Worker leasing/fencing/effect semantics, retained
query snapshots, notification delivery, company identity integration and O3
product behavior remain separate work.

## CHG-126 durable worker substrate

CHG-126 adds the reusable PostgreSQL worker/job lease, fencing and bounded
LOCAL-effect substrate under `ephi.application.worker` and
`ephi.infrastructure.PostgreSQLWorkerStore`. Enqueue is idempotent by
`(scope, semantic_key, payload_hash, job_type)`; claims are short
`FOR UPDATE SKIP LOCKED` transactions with monotonically increasing fencing
epochs; lease validity is checked against PostgreSQL server time. The
documented defaults are a 120-second lease and 30-second heartbeat interval,
with tiny explicit test overrides supported.

Delivery remains at-least-once. Exactly-once behavior is limited to a bounded
local PostgreSQL mutation plus its unique `applied_effect` receipt. No
scientific handlers, worker loop, O3 Attention/claim/acknowledge behavior,
NiceGUI pages, retained query snapshots, company bindings, notifications or
external effects are implemented. The PostgreSQL worker tests are
reference/integration evidence against a real PostgreSQL 18.x service, not a
bound company deployment and not full O2/G05 completion.

## CHG-129 generic read/snapshot substrate

CHG-129 adds the storage-neutral coherent-read and retained-query contracts
under `ephi.application.read` and PostgreSQL migration
`migrations/003_o2_read_snapshot_core.sql`. The reference adapter provides
immutable read revisions with historical workflow snapshots, CAS-safe current
heads, short PostgreSQL `REPEATABLE READ` current bundles, exact historical
reads, bounded retained row-version snapshots, database-time expiry, and
integrity-checked page cursors. A current bundle combines the immutable
analytical head with the live workflow aggregate from that same snapshot and
returns an effective revision vector with the live workflow version; a
historical bundle remains pinned to the workflow snapshot stored on its read
revision. Current authorization, effective scope and security-revision
identity are revalidated for every retained page.

Its focused real-PostgreSQL evidence is additive:

```bash
EPHI_TEST_POSTGRES_DSN='postgresql://user:password@host:5432/database' \
  python3 -m unittest tests.test_o2_postgresql_reads -v
```

At the CHG-129 baseline, this was generic PostgreSQL read/snapshot foundation
evidence only. It did not implement `ListAttention`, `GetEpisodeBrief`, Claim/Acknowledge commands,
NiceGUI pages, browser state, company identity adapters, scientific source
queries, notifications or production readiness. O3 will bind these primitives
to the first durable Attention → Episode UI slice. The CI PostgreSQL 18.x job
runs this suite together with the existing command and worker suites; the
normal package matrix, full suite and W0 canonical baseline remain
database-independent.

## CHG-134 O3.1 durable Attention → Episode W1 slice

CHG-134 binds the O2 command and CHG-129 read authorities into the smallest
real O3 slice: a bounded, stably ordered PostgreSQL Attention projection; a
coherent current/historical Episode brief; and durable `ClaimEpisode` and
`AcknowledgeEpisode` transitions with expected-version CAS, viewed revisions,
authorization-aware receipt replay, immutable audit and outbox writes. Retained
Attention pages use the existing durable snapshot/cursor contract, expire by
database time and revalidate current scope/capability/security revision on
every continuation. The UI uses only the installed NiceGUI Base public root
contract and one EPHI DataSource provider; it has no memory, SQLite or demo
fixture production fallback.

Focused application evidence is database-independent:

```bash
python3 -m unittest tests.test_o3_application -v
```

The real PostgreSQL evidence is additive and DSN-gated:

```bash
EPHI_TEST_POSTGRES_DSN='postgresql://user:password@host:5432/database' \
  python3 -m unittest tests.test_o3_postgresql -v
```

This slice deliberately does not bind company identity, scientific metrology
families, WIP/exposure, investigation/RCA, recovery, closure/reopen,
outcomes/value, notifications, production credentials or release
qualification. Missing PostgreSQL, identity, source, browser or Artifact
Bridge infrastructure remains an explicit NOT_RUN/BLOCKED evidence result.

## CHG-144 O4.1 bounded source ingress

The O4.1 boundary adds only typed validation for one declared metrology
family/capability, immutable PostgreSQL `source_snapshot` manifests,
truthful `source_capability` state, and the existing scoped artifact/hash
precondition. Raw telemetry is not copied into PostgreSQL. The canonical
application has no company metrology adapter, so composition fails closed
without an explicit observer adapter binding; historical `metrology/` names
remain reference-only.

Run the secret-safe reality preflight with:

```bash
python3 -m ephi --source-reality-preflight --json
```

The expected checkout result is `BLOCKED_REAL_SOURCE` / `NOT_RUN` with
`UNAVAILABLE` capability state until an approved adapter, one family and its
mapping/reference facts are supplied. Focused PostgreSQL evidence is additive
and DSN-gated:

```bash
EPHI_TEST_POSTGRES_DSN='postgresql://user:password@host:5432/database' \
  python3 -m unittest tests.test_o4_source_ingress_postgresql -v
```

This slice does not claim G02/G06, a real-family snapshot, scientific
qualification or production readiness.

## CHG-133 generic immutable-artifact foundation

CHG-133 adds storage-neutral immutable artifact contracts under
`ephi.application.artifacts`, a directory-handle-anchored file-backed
reference blob adapter under `ephi.infrastructure.artifacts`, and a separate
scoped PostgreSQL catalog in migration
`migrations/004_o2_artifact_catalog.sql`. Artifact identity is the canonical
lowercase SHA-256 of exact bytes plus exact byte size. Catalog records are
scoped and immutable; the same physical content hash may have separate records
in separate scopes. Current Principal, AccessScope and read-capability checks
run before metadata or byte disclosure, and verified reads recompute the hash
and size. Publish-precondition verification fails closed for missing, corrupt,
or wrong-scope references.

The filesystem adapter requires an explicit absolute root and has a bounded
16 MiB default maximum suitable only for tests/reference evidence. It is not
the approved company immutable object-store binding. This change does not add
scientific source artifacts, Episode evidence or upload UI, browser download
transport, external object-store SDKs, credentials, signed URLs, retention
policy binding or production-readiness claims. Those remain external
deployment/qualification work. PostgreSQL artifact evidence is additive and
DSN-gated; the ordinary package/full-suite/W0 paths remain database-independent.

## Pinned framework authority

## CHG-150 O8.1 current-authorization freshness boundary

O8.1 adds one storage-neutral `CurrentAuthorizationAuthority` at the
application boundary. Protected command/receipt replay, Attention source and
query/count/snapshot operations, current and historical Episode reads, and
artifact metadata/byte/publish-precondition disclosure re-resolve the current
server-side Principal before protected lookup. Exact subject,
`auth_session_revision`, `security_revision`, scope and capability checks fail
closed with bounded permission errors; a newer Principal is never silently
substituted for the presented one. Retained snapshots still expire across an
effective security-revision change and remain subject/scope/cursor bound.

The development/test UI binds this authority to the existing environment-backed
identity provider at operation time. Non-development composition remains fail
closed until a real company identity/current-authorization adapter is supplied.
Focused unit/ordering evidence is `tests.test_o8_current_authorization`; real
PostgreSQL 18.x evidence is DSN-gated in `tests.test_o8_postgresql` and is run
in the PostgreSQL CI lane after the optional PostgreSQL dependency is installed.
This slice does not qualify company identity/session transport, browser
CSRF/WebSocket-origin controls, upload/export/sanitization or deployment.

## CHG-167 O5.1 unified decision-loop core

O5.1 extends the existing O3 `episode_workflow` aggregate with a bounded
`decision_loop` payload containing work cycles, checks, external action
records, locked recovery plans/observations, closure records and reopen
history. It does not create an `ephi_decision_loop` aggregate or a second
human-work state. O3 and O5 commands share the existing versioned command
executor, receipt, audit, outbox and authorization boundaries; every
decision-sensitive command uses the same expected/viewed workflow version.

Confirmed-issue closure names one locked PASS recovery plan bound to a
current-cycle action with an explicit successful reconciliation and post-action
evidence. Technical recovery PASS never closes human work. Reopen preserves
the prior cycle and resets ownership deterministically for the new open cycle.
The focused offline evidence is `tests.test_o5_decision_loop`; real PostgreSQL
18.x evidence is DSN-gated in `tests.test_o5_postgresql` and is run in the
existing PostgreSQL CI lane. This remains reference/integration evidence and
does not establish family qualification, human pilot, UI qualification or
production readiness.

## CHG-169 O5.2 decision snapshots and authorized handoff delivery

O5.2 adds bounded immutable decision snapshots bound to one Episode cycle,
one exact `episode_workflow` version and the caller's viewed `RevisionVector`.
Handoff intents are projected only from committed O2 `outbox_event` rows and
deduplicated by material change, current recipient, channel and policy
identity. Delivery jobs use the existing PostgreSQL worker lease/fencing and
`applied_effect` receipt authority; an expired lease or ambiguous external
outcome becomes `UNKNOWN` and requires explicit reconciliation.

The qualification-only deterministic in-app recipient/channel adapters are
not company notification integrations. `tests.test_o5_handoff` covers the
offline immutable/read boundary; `tests.test_o5_handoff_postgresql` is the
additive real-PostgreSQL-18 evidence in the existing integration lane. Real
company notification channels, real-family qualification, WIP/exposure,
human pilot, UI qualification and production readiness remain unimplemented
or not run, and exactly-once external delivery is not claimed.

The O5.2 projector derives classification and material-change identity from
the committed O2 outbox envelope. Its explicit eligible mapping is limited to
`ClaimEpisode` (assignment handoff), `RecordExternalAction` (action recorded),
`CloseEpisode` (closure), and `ReopenEpisode` (reopen); unsupported or
malformed events fail closed. Retained caller classification/signature
arguments are checked against the derived facts and cannot create another
logical intent.

## CHG-171 O6.1 deterministic next-check planner core

`ephi.application.planner` adds a typed, versioned curated `CheckTemplate`,
planner-policy and bounded current-facts contract. `NextCheckPlannerService`
reads through the existing O5 `DecisionLoopCommandService` and
`CurrentAuthorizationAuthority`, requires one exact active Episode cycle,
workflow version and viewed `RevisionVector`, and rechecks that view before
disclosure. It hashes the canonical facts, durable O5 workflow state, policy
and catalog into immutable plan/input identities; planning does not persist or
mutate workflow/check state.

V1 uses the versioned ordinal utility
`U = 0.45*D + 0.20*N + 0.20*F - 0.10*E - 0.15*R`, stable typed exclusion
reasons and structured explanation traces. Scores are ordinal ranking values,
not posterior probabilities, causal probabilities or scientific confidence.
Unknown effort and turnaround receive conservative ranking treatment; a
supported deadline excludes work that cannot be shown timely. Existing
completed evidence is reused only for the same target/context while its
validity and reuse window hold. Human or external work remains under O5's
separate commands and is never changed by replanning.

Focused evidence is `tests.test_o6_planner`; the PostgreSQL 18 restart and
nonmutation regression is `tests.test_o6_planner_postgresql` and requires an
explicit `EPHI_TEST_POSTGRES_DSN`. Templates are supplied as curated catalog
configuration; this slice adds no production-family catalog, capability
qualification, source binding, UI, plan persistence or W4 qualification claim.

## CHG-147 O9.1 operations and restore rehearsal

The CLI-only O9.1 boundary is documented in [operations](operations/README.md).
It exposes separate secret-safe operational axes, deterministic PostgreSQL
logical-backup identity, immutable-artifact verification, isolated restore, and
post-cutoff reconciliation through the existing O2/O3/O4 authorities. Run:

```bash
python3 tools/o9_operations.py status --json
```

Local rehearsal timing is evidence only. The tooling always records
`production_disaster_rpo_rto_claim = NOT_ESTABLISHED`; it does not claim the
E7 target RPO/RTO or authenticate a real source family.

## CHG-205 U2.2 / O7.1 generic Outcomes and value review

The canonical `/ephi/outcomes` route adds a bounded, authorized view and
revision-bound reviewer transition over the existing F04 value records and
O2 PostgreSQL aggregate/receipt/audit/outbox authorities. One scoped
economic-event key identifies one claim group; linked Episodes, decisions,
actions and contributors are attribution references and never multiply its
amount. Corrections remain append-only, select the active leaf at the
knowledge cutoff before filtering the event period, and do not inherit review.
PostgreSQL stores immutable value revisions in `outcome_value_revision` with
`amount NUMERIC`; monetary amounts are not duplicated in aggregate JSONB. The
revision insert and the O2 aggregate CAS, receipt, audit and outbox commit in
one transaction. Reads return exact `Decimal` values and decimal strings in
JSON. Explicit void revisions have no amount, supersede the active leaf, and
remain visible after their known-at cutoff without restoring a predecessor.
Currency summaries remain separate because no versioned conversion policy is
configured.

The route separates estimated opportunity, observed operational outcomes and
independently reviewed benefit/net cost. Reviewers cannot edit claim science;
approval binds the exact value revision, evidence identity, model identities
and cutoff, and a later-known review remains absent from earlier `AS_KNOWN`
results. Read results expose deterministic `query_identity` and
`result_identity` values over canonical filters and ordered revision facts.
The only demonstration facts are synthetic. This slice does not
qualify ROI or savings, company monetary data/models/adapters, real-family
qualification, W3–W5 human pilots, Operations expansion, G12, Port Gate,
release or Production.

Focused offline evidence is `tests.test_value_integrity`,
`tests.test_outcomes_workflow` and `tests.test_o2_transactions`. PostgreSQL
restart and concurrent successor evidence is DSN-gated in
`tests.test_outcomes_postgresql`. The predecessor browser evidence remains
under `evidence/u2/chg-205-u2.2-o7.1/`; the repair continuation report and
1440×900 / 390×844 screenshots are under
`evidence/u2/chg-205-u2.2-o7.1/review-fix1/`. The continuation was requalified
against local PostgreSQL 18.6; CI keeps its PostgreSQL 18.x Outcomes lane.

CHG-105 pins `nicegui-base` to Git commit `000298562d6bcbf6df304edbd41b98b30fe4bfcf`, framework version `3.0.0a8`, exactly `nicegui==3.15.0`, and Python `>=3.11,<3.14`. Application code uses public `from nicegui_base import ...` authorities only; it does not use direct `nicegui.ui` or private `nicegui_base.integrations.nicegui_*` APIs. The machine-readable runtime specification is [environment/nicegui_base_runtime.json](environment/nicegui_base_runtime.json).

For an isolated dependency/bootstrap qualification, use the existing CHG-105 tool separately:

```bash
python3.11 tools/w0_runtime.py bootstrap
python3.11 tools/w0_runtime.py check
python3.11 tools/w0_runtime.py discover
python3.11 tools/w0_runtime.py qualify
```

Those commands may install dependencies and are not required by the offline repository test path.

## Historical compatibility tools

`tools/source_preflight.py` and `tools/w0_baseline.py` are retained as explicitly legacy, optional historical-source compatibility utilities for CHG-85/CHG-104/CHG-109 fixture tests. They are not invoked by package validation, the canonical baseline, application tests or implementation gates. Preserved historical evidence remains reference-only and is not rewritten.

## CHG-182 U1 one-way downstream integration ABI

The versioned ABI is `org.ephi.downstream` `1.0.0`, independent of the EPHI
distribution version. Its six required provider categories compose the
existing current-authorization, bounded source-observer, immutable-artifact,
committed handoff, typed policy and runtime/PostgreSQL authorities. Discovery
accepts only one explicit `module:factory` entrypoint. The canonical safe
manifest contains public contract/version/capability metadata only and has a
deterministic canonical SHA-256 identity.

The supported synthetic private-style package is under
`examples/synthetic_downstream`; run
`python -m ephi.downstream --entrypoint examples.synthetic_downstream.provider:build_bundle --json`
for secret-safe compatibility and PostgreSQL composition preflight. The
boundary check covers this example's public imports and location, not
arbitrary private code. Successful U1 conformance does not imply real family
source/science G02/G06, company identity/TLS, performance G10, G12, Port Gate
or Production qualification.

## CHG-234 U2.4 bounded Asset 360 and history

The generic `/ephi/assets` list projects only assets present in authorized
current Episode heads with a supported `InvestigationProfile`; it is not a
company asset-master inventory. Exact family/site/context/characteristic
filters and asset-ID tie-breaking use O8 scope and the existing retained O2
query snapshots/cursors. `/ephi/assets/{id}` composes immutable O3 Episode
read revisions, O5 workflow/action/recovery facts, and bounded read-only U1
observations pinned to an immutable O4 source revision. It adds no Asset table,
source adapter, raw telemetry persistence, material/WIP authority or workflow
authority.

The Asset 360 read binds one asset and knowledge cutoff to exact Episode/read
revision IDs, workflow versions, the latest immutable exact-binding O4
snapshot eligible at that cutoff, selected characteristic/unit, and the exact
revision-pinned observation identity set. Observation event time and
source-available time must both qualify; exact binding and
asset/context/characteristic/unit mismatches fail closed. U1 v1 providers keep
their existing live bounded-read ABI; the additive revision-pinned read is
optional globally and required only for historical Asset measurements. Asset
360 reports a typed unavailable limitation when it is missing and never falls
back to live rows. O4 freshness policy is retained with each new immutable
snapshot; legacy rows without it report that historical freshness cannot be
reconstructed.
Comparable history requires the existing explicitly qualified compatible
population. Gaps remain unconnected in the trend, and O5 actions/recovery are
shown as recorded observations without causal interpretation. Material
context is explicitly unavailable without a qualified source. The synthetic
fixture and exercised limits are documented in
[the CHG-234 slice note](docs/CHG-234-U2.4-asset-360.md).

Focused reference integration evidence requires a real PostgreSQL 18 service:

```bash
EPHI_TEST_POSTGRES_DSN='postgresql://user:password@host:5432/database' \
  python3 -m unittest tests.test_assets_postgresql -v
```

Candidate-bound desktop/phone browser screenshots and clean console/page/
request inventories, including READY, stale, unavailable, history-gap,
compatible/blocked comparison and Episode origin-preservation states, are
recorded in
[the R2 continuation evidence](evidence/u2/chg-234-u2.4-asset-360/review-fix1/qualification.json).
This slice does not claim company asset-master completeness, real-family
G02/G06, causal/RCA or predictive health, production capacity/G10, Operations
destination implementation, G12, Port Gate, or Production readiness.

## CHG-252 U2.5 / O9.2 Operations cockpit

The authorized `/ephi/operations` destination reports six independent O9
platform axes through a small query layer over the existing PostgreSQL,
WorkerJobPort, O4 source, immutable-artifact and O7 qualification authorities.
`process_transport=READY` means only that the application/query path responded;
the UI exposes no overall health score. Source freshness, bounded worker
details, artifact integrity, evidence expiration and the generic/local O9.1
rehearsal contract remain separate. Production RPO/RTO is always
`NOT_ESTABLISHED` without deployment-owned qualified evidence. Consequential
controls stay disabled unless a future approved runtime binds an explicitly
authorized, auditable capability.

Candidate-bound synthetic PostgreSQL 18 and desktop/phone browser evidence is
recorded in
[the CHG-252 qualification](evidence/u2/chg-252-u2.5-o9.2/qualification.json).
The fixture covers mixed axes, stale/unavailable source, qualification
expiration and unbound authority, missing/corrupt artifact bytes, worker queue
and failure states, and disabled controls. It does not qualify production,
manufacturing/tool health, real-family G02/G06, G10, G12, Port Gate, or
production disaster recovery. Focused query regressions are in
`tests.test_operations_application` and real PostgreSQL evidence is in
`tests.test_operations_postgresql`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for evidence preservation and validation. No project license has been selected.
