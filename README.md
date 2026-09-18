# EPHI

Engineering decision support for equipment, process and measurement-system health.

**Status: canonical repository baseline implemented; application features and production qualification are pending.** This repository contains the EPHI 1.0 design, preserved historical source-audit evidence, and the smallest installable repo-native EPHI application foundation. It does not claim identity, byte identity, algorithm equivalence, or historical-test equivalence with an earlier application artifact.

EPHI's intended workflow is **Detect → Explain → Prioritize → Contain → Investigate → Act → Verify recovery → Learn → Prove value**. Manufacturing actions remain human controlled in approved external systems.

## Start here

- [Design overview](00_START_HERE.md): product choices, scope and original source identity.
- [Package review and readiness](13_Package_Review.md): what was checked, what changed, and what blocks implementation.
- [Developer handoff](11_Developer_Start.md): prerequisites and the first implementation slice.
- [Delivery gates](09_Delivery_and_Gates.md): acceptance criteria from baseline through a qualified family release.

## Contents

| Document | Purpose |
|---|---|
| [01 · Product and architecture](01_Product_and_Architecture.md) | Requirements, boundaries and invariants |
| [02 · Historical source audit](02_Source_Audit.md) | Observed strengths and F01–F14 findings |
| [03 · Application contracts](03_Application_Contracts.md) | Queries, commands, revisions and concurrency |
| [04 · Data and runtime](04_Data_and_Runtime.md) | Persistence, time, jobs and recovery |
| [05 · UX and interaction](05_UX_and_Interaction_Design.md) | Navigation, workspaces and degraded states |
| [06 · Investigation and recovery](06_Investigation_and_Recovery_Logic.md) | Planner and scientific recovery rules |
| [07 · NiceGUI Base integration](07_NiceGUI_Base_Integration.md) | Pinned framework and integration boundaries |
| [08 · Security and operations](08_Security_Notifications_AI_Operations.md) | Authorization, notifications and optional AI |
| [09 · Delivery and gates](09_Delivery_and_Gates.md) | Tests, migration, rollout and rollback |
| [10 · Traceability and decisions](10_Traceability_and_Decisions.md) | Requirements, decisions and unresolved bindings |
| [11 · Developer start](11_Developer_Start.md) | First implementation slice |
| [12 · Sources and evidence](12_Sources_and_Evidence.md) | Provenance and reproducibility limits |
| [13 · Package review](13_Package_Review.md) | Repository preparation and corrections |

The numbered chapters are the maintained design. [manifest.json](manifest.json) records current file hashes. [Historical evidence](evidence/README.md) is preserved separately from [this review's evidence](evidence/review/package_review.json).

## Validate this repository

Python 3.11–3.13 is supported. Offline repository checks need no third-party packages, network, credentials, chat attachment, or source archive:

```bash
python3 tools/check_package.py
python3 -m unittest discover -s tests -v
python3 tools/w0_repo_baseline.py
```

Checks cover file integrity, local Markdown links and code fences, JSON/TOML/Python syntax, requirement coverage, imported audit consistency, canonical package identity, exact dependency declarations, Git identity, and deterministic import/entry behavior. CI runs package checks on Python 3.11–3.14; the application itself supports Python 3.11–3.13.

The historical **275 passed / 1 skipped** result remains `REFERENCE_ONLY` in imported audit evidence. It is not this repository's test result. Current canonical tests are the results from `python3 -m unittest discover -s tests -v`.

## Install and identify the canonical application

The canonical application is defined by [pyproject.toml](pyproject.toml) and lives under `src/ephi`. A fresh Git checkout can install the package with the exact CHG-105 framework pins:

```bash
python3 -m pip install .
python3 -m ephi --self-check
```

The baseline entrypoint is intentionally a deterministic identity/configuration self-check. It does not start a production server, create company bindings, persist data, or implement broad UI behavior. The application is a new canonical implementation, not a reconstruction of an historical source tree.

## W0 repository baseline

The machine-readable contract is [environment/w0_repo_baseline.json](environment/w0_repo_baseline.json), and the runner is [tools/w0_repo_baseline.py](tools/w0_repo_baseline.py). It records only Git HEAD/tree/worktree facts, the supported Python range, exact declared dependencies, package/import identity, manifest membership/hashes, deterministic entrypoint output, and current canonical test results. Its ignored output is `artifacts/w0-repo-baseline.json`.

Identity or manifest corruption fails closed before canonical tests execute. Historical audit results remain a separate `REFERENCE_ONLY` field and cannot make the canonical baseline pass.

## Historical-source compatibility utilities

`tools/source_preflight.py` and `tools/w0_baseline.py` are retained as explicitly optional legacy compatibility utilities for historical-source work. They are not prerequisites for installation, package validation, the canonical W0 baseline, ordinary tests, or future implementation gates. Do not request, locate, stage, reconstruct, download, or depend on the historical source archive for the normal repository workflow. Its filename and SHA-256 are preserved only as `REFERENCE_ONLY` provenance in imported evidence and related historical records.

## W0 integrity regressions (CHG-109)

Run `python3 tools/w0_integrity_regressions.py` for the default canonical target. The preserved F02/F03/F04/F05 probe files and observations remain immutable `REFERENCE_ONLY` evidence. Because those behavioral APIs do not yet exist in the minimal `src/ephi` application, the canonical runner reports `NOT_IMPLEMENTED`/`NOT_RUN`; it does not block the repository baseline or fabricate `PASS`. The old source-bound execution is available only with the explicit `--legacy-source` option and is not part of the normal W0 path.

The CHG-104 NiceGUI Base binding record is [evidence/review/nicegui_base_binding_manifest.json](evidence/review/nicegui_base_binding_manifest.json). It records the pinned source inspection and explicitly records installed CLI discovery as unavailable when that executable is absent.

## W0 pinned framework qualification (CHG-105)

The independent W0 framework slice uses the machine-readable [runtime specification](environment/nicegui_base_runtime.json) and its exact [requirements](environment/nicegui_base_requirements.txt). It installs NiceGUI Base only from the recorded VCS commit `000298562d6bcbf6df304edbd41b98b30fe4bfcf`, requires framework version `3.0.0a8`, requires exactly `nicegui==3.15.0`, and accepts only Python `>=3.11,<3.14`. The checker verifies `direct_url.json` VCS identity and the CHG-104 public root imports before running any framework command. A missing or mismatched identity fails closed.

Use an explicitly supported interpreter to create the ignored environment and run the bounded checks:

```bash
python3.11 tools/w0_runtime.py bootstrap
python3 tools/w0_runtime.py check
python3 tools/w0_runtime.py discover
python3 tools/w0_runtime.py qualify
```

`discover` executes the six requested NiceGUI Base discovery authorities and preserves their machine-readable responses in ignored `artifacts/w0-runtime/` output. `qualify` runs `nicegui-base agent-check .`, `nicegui-base gate .`, `nicegui-base runtime-contract`, and `nicegui-base runtime-smoke --port 0`. The framework smoke is a browserless NiceGUI Base laboratory check, not EPHI application production qualification or browser qualification. The checked-in summary and its separation from CHG-104 source inspection are recorded in [the CHG-105 runtime evidence](evidence/review/nicegui_base_runtime_evidence.json) and linked from [the binding manifest](evidence/review/nicegui_base_binding_manifest.json).

## Before expanding implementation

1. Keep the canonical package/import/test baseline passing on a supported Python interpreter.
2. Resolve any additional installed APIs of [NiceGUI Base at the inspected commit](https://github.com/kimhw8084/nicegui-base/tree/000298562d6bcbf6df304edbd41b98b30fe4bfcf) through its public exports and the existing CHG-104/105 binding evidence.
3. Implement only the next authorized vertical slice against `src/ephi`, then add its contracts, tests, and qualification evidence. Company bindings and target qualification remain explicit gates.

This repository baseline does not complete W0 behavioral qualification or qualify a deployment.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for evidence preservation and validation. No project license has been selected.
