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
