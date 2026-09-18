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

Run `python3 tools/w0_integrity_regressions.py` to verify preserved CHG-109 evidence and exercise the canonical entry self-check. F02/F03/F04/F05 are intentionally reported `NOT_IMPLEMENTED`/`NOT_RUN` because those APIs are outside this minimal slice. Future changes implement and close them against `src/ephi`.

The staged-source behavior is retained only as an explicit legacy fixture path:

```bash
python3 tools/w0_integrity_regressions.py --legacy-source --preflight <legacy-result>
```

It is not required by the package checker, canonical baseline, application tests or future implementation gates. The retained `tools/source_preflight.py` and `tools/w0_baseline.py` modules are optional historical-source compatibility utilities only.

## Scope boundary

The first canonical package contains identity, a runtime/config boundary, a deterministic entry self-check and public framework-authority lookup. It does not contain broad UI, company adapters, production storage, speculative science modules or fake product behavior. Continue from [09_Delivery_and_Gates.md](09_Delivery_and_Gates.md) and stop at the authorized wave exit criteria.
