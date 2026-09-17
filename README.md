# EPHI

Engineering decision support for equipment, process and measurement-system health.

**Status: reviewed design handoff. Application implementation and production qualification are pending.** This repository contains the EPHI 1.0 design, historical source-audit evidence, and executable package checks. It does not yet contain an installable EPHI application.

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

Python 3.11 or newer; no third-party packages or application credentials are needed:

```bash
python3 tools/check_package.py
python3 -m unittest discover -s tests -v
```

Checks cover file integrity, local Markdown links and code fences, JSON/Python syntax, requirement coverage, and consistency of the imported audit evidence. CI runs these package checks on Python 3.11–3.14. This is distinct from the proposed application's pinned Python 3.11–3.13 runtime.

The historical **275 passed / 1 skipped** result belongs to the original application audit. It is not this repository's test result. The application tests and behavioral probes require the absent original source.

## W0 source preflight

The first bounded W0 step is a fail-closed source restoration preflight. Place the exact, owner-supplied archive at the ignored default location below, preserving its required filename, then run one command:

```text
artifacts/source/ephi_v0.19.1_production_hardened(1).zip
```

```bash
python3 tools/source_preflight.py
```

The preflight writes `artifacts/source-preflight.json` and, only after the exact SHA-256 and archive-member safety checks pass, stages the source under `artifacts/source-staging/`. An alternate local/staging archive may be supplied with `--archive`, but its basename must still be `ephi_v0.19.1_production_hardened(1).zip`; `--stage-dir` must name a fresh directory. Paths under `evidence/` are rejected. The archive and staged source are ignored local artifacts and must never be committed.

`SOURCE_REQUIRED` (exit 2) means the exact archive is absent. `SOURCE_REJECTED` (exit 3) means filename, hash, ZIP structure, member path, special-file/symlink safety, or staging validation failed. `SOURCE_STAGED` (exit 0) records source identity and discovers Python, dependency-file, test-file and runtime reality; it does not run the original application tests. The JSON keeps the historical 275 passed / 1 skipped audit result under `historical_test_result` with `REFERENCE_ONLY` status and records current test execution separately as `NOT_RUN` until an engineer runs the verified source baseline.

This foundation does not reconstruct application modules and does not fix or claim to fix F03, F04 or F05. Those changes remain blocked on the verified original source and their W0 regression/qualification work.

## W0 baseline execution

After preflight reports `SOURCE_STAGED`, run the bounded baseline harness:

```bash
python3 tools/w0_baseline.py
```

The harness consumes `artifacts/source-preflight.json`, re-inventories the reported staged root, and runs only the test runner and test roots discovered there. It exits non-zero without executing tests for a missing/non-staged preflight, any source identity mismatch, or a missing staged root. Its ignored output is `artifacts/w0-baseline.json`; its `historical_test_result` remains `REFERENCE_ONLY` and is separate from `current_run`.

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

## Before implementation

1. Run the W0 source preflight above with the original `ephi_v0.19.1_production_hardened(1).zip`; verify SHA-256 `5e9ad8f63b3158adc530af69fc650aec606cbfa64896ba73840cdcf994a2b6e3`. The supplied design ZIP is a different artifact.
2. Reproduce that source baseline in isolation and record dependencies. Preserve existing scientific modules and fix the documented integrity defects through regression tests.
3. Resolve the installed APIs of [NiceGUI Base at the inspected commit](https://github.com/kimhw8084/nicegui-base/tree/000298562d6bcbf6df304edbd41b98b30fe4bfcf), then follow W0/W1. Company bindings and target qualification remain explicit gates.

There is no application installation or launch command yet. Publication of this design does not complete W0 or qualify a deployment.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for evidence preservation and validation. No project license has been selected.
