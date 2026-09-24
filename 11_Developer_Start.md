# Developer start

CHG-111 R3 establishes the canonical application directly in this Git repository. A fresh checkout is the normal W0 starting point. This is a new implementation boundary, not a reconstruction of the historical audited application; do not claim source identity, byte identity, algorithm equivalence or historical-test equivalence.

## Install and identify

Use Python `>=3.11,<3.14` and the exact dependency declarations in [pyproject.toml](pyproject.toml): NiceGUI Base commit `000298562d6bcbf6df304edbd41b98b30fe4bfcf`, framework version `3.0.0a8`, and `nicegui==3.15.0`.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m ephi --self-check --json
```

The package exposes `ephi.app:main`, `python -m ephi`, deterministic runtime/config identity and a self-check that does not require a database, company binding or framework import. Framework construction, when authorized by a later slice, must use public `from nicegui_base import ...` authorities only.

## Canonical W0 checks

Run the standard-library checks from the checkout without a chat file, archive, staged source or network:

```bash
python3 tools/check_package.py
python3 tools/w0_repo_baseline.py
python3 -m unittest discover -s tests -v
git diff --check
```

The [machine-readable baseline specification](environment/w0_repo_baseline.json) binds package name/version, Python range, source/package root, entrypoint and dependency identities. The baseline tool records Git commit/tree/worktree state, manifest identities, import identity and current test results. The historical 275/1 audit result remains `REFERENCE_ONLY` and is not included in current test totals.

## CHG-109 boundary

Run `python3 tools/w0_integrity_regressions.py` to verify preserved CHG-109 evidence, exercise the canonical entry self-check and run fresh F02/F03/F04/F05 scenarios against `src/ephi`. F05 is limited to the deterministic W0 recovery regression policy and does not qualify a production family. The runner's PASS is based on executed observations, not the static self-check labels.

The staged-source behavior is retained only as an explicit legacy fixture path:

```bash
python3 tools/w0_integrity_regressions.py --legacy-source --preflight <legacy-result>
```

It is not required by the package checker, canonical baseline, application tests or future implementation gates. The retained `tools/source_preflight.py` and `tools/w0_baseline.py` modules are optional historical-source compatibility utilities only.

## CHG-121 reference transaction adapter

The reusable O2 transaction substrate is `ephi.application` plus
`ephi.infrastructure.sqlite.SQLiteReferenceTransactionAdapter`. It is a
file-backed stdlib SQLite reference adapter for offline/integration evidence;
it rejects in-memory paths and is not PostgreSQL/G05 qualification. Use the
focused tests to exercise command receipts, authorization-aware replay,
compare-and-set conflicts, concurrent first attempts and atomic audit/outbox
rollback:

```bash
python3 -m unittest tests.test_o2_transactions -v
```

Do not extend this slice into NiceGUI pages or ClaimEpisode/Acknowledge
product behavior. CHG-123 adds PostgreSQL reference/integration evidence only:
select it with an explicit `EPHI_TEST_POSTGRES_DSN` and the `postgres`
optional dependency. It does not bind a company production database or
complete G05/O2. Worker fencing, retained query snapshots and later O3 use
use cases remain separately authorized work. CHG-126 now provides only the
generic PostgreSQL worker/job lease, fencing and bounded LOCAL-effect
substrate. Its PostgreSQL 18.x evidence is reference/integration evidence,
not a company deployment or full O2/G05 completion.

## CHG-129 generic read/snapshot substrate

The generic read foundation is exposed through `ephi.application.read` and
`PostgreSQLReferenceTransactionAdapter.read_store()` (also forwarded on the
adapter). It provides immutable read revisions and current heads, coherent
current bundles in a short PostgreSQL `REPEATABLE READ` transaction, exact
historical workflow snapshots, bounded retained row-version query snapshots,
database-time expiry and fail-closed cursor/authorization checks. Current
reads intentionally combine the immutable analytical head with the live
workflow aggregate and effective workflow version; historical reads retain
the stored workflow snapshot. Run its additive reference evidence with:

```bash
EPHI_TEST_POSTGRES_DSN='postgresql://user:password@host:5432/database' \
  python3 -m unittest tests.test_o2_postgresql_reads -v
```

At the CHG-129 baseline, this was generic PostgreSQL foundation evidence, not
production/company binding and not `ListAttention`, `GetEpisodeBrief`, Claim/Acknowledge,
NiceGUI/browser, company identity, scientific-source or notification
implementation. CHG-134 now binds these primitives to the first durable Attention
→ Episode UI slice. Every page is a separate bounded transaction; no browser
request carries a PostgreSQL transaction across requests.

## CHG-134 O3.1 W1 implementation boundary

The authorized O3.1 slice is now the narrow Attention → Episode path under
`ephi.application.attention`, `ephi.application.episodes`,
`ephi.application.workflow`, `ephi.infrastructure.postgresql_o3` and
`ephi.ui`. It must remain on the existing O2 command/read/snapshot authorities:
do not add a second cursor, receipt, workflow, browser-state or authorization
store. Production composition requires explicit PostgreSQL, server-resolved
identity and source bindings; the reference SQLite adapter is test/development
evidence only. Run focused O3 tests, the DSN-gated PostgreSQL O3 suite, the
installed Base runtime contract/qualification commands and browser evidence
when those bindings are available. Stop after W1; do not extend into
metrology-family binding, WIP/exposure, investigation/RCA, recovery,
closure/reopen, outcomes/value, notifications or release qualification.

## Scope boundary

The canonical package contains the narrow W1 UI and durable reference/runtime
composition in addition to the W0/O2/O2-read foundations. It still does not
contain company adapters, production credentials, speculative science modules
or fake product behavior. Continue from [09_Delivery_and_Gates.md](09_Delivery_and_Gates.md)
and stop at the authorized wave exit criteria.

## CHG-205 U2.2 / O7.1 generic Outcomes workflow

The bounded Outcomes slice extends `src/ephi/value/model.py`,
`repository.py`, and `service.py` while preserving the existing F04
active-leaf-as-of behavior. PostgreSQL writes use the existing O2
versioned aggregate command executor and atomic receipt/audit/outbox path;
`migrations/009_o7_outcome_group_identity.sql` adds only scoped economic-event
deduplication. Review is a separate immutable, revision-bound transition and
must be reauthorized when queried or committed. Do not combine currencies
without an explicit versioned conversion policy or infer validated savings
from estimates/observations.

For this slice, run `tests.test_value_integrity`,
`tests.test_outcomes_workflow`, and `tests.test_o2_transactions` offline. Run
`tests.test_outcomes_postgresql` with `EPHI_TEST_POSTGRES_DSN` for restart,
concurrency, uniqueness and O2 receipt evidence. Browser qualification uses
the synthetic-only `tools/o7_outcomes_browser_qualification.py` and records
the tested PostgreSQL server version. Keep its report/screenshots in
`evidence/u2/chg-205-u2.2-o7.1/`. Stop at generic U2/O7 behavior; do not add
company monetary data, conversion values, family qualification, human pilot
completion, Port Gate, G12 or Production claims.
