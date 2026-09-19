# EPHI

Engineering decision support for equipment, process and measurement-system health.

**Status: canonical repository baseline present; behavioral, browser, scientific and production qualification are not complete.** This Git repository is the sole executable source of truth for the new canonical EPHI implementation. It is intentionally a small package/application boundary, not a reconstruction of the historical audited application.

EPHI's intended workflow is **Detect → Explain → Prioritize → Contain → Investigate → Act → Verify recovery → Learn → Prove value**. Manufacturing actions remain human controlled in approved external systems.

## Start here

- [Design overview](00_START_HERE.md): product choices, scope and provenance boundaries.
- [Package review and readiness](13_Package_Review.md): current repository state and qualification limits.
- [Developer handoff](11_Developer_Start.md): install, import, self-check and the first authorized slice.
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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for evidence preservation and validation. No project license has been selected.
