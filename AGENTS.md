# Repository instructions

This is the EPHI canonical application/design/evidence repository. Read README.md and 13_Package_Review.md before changing it. The historical application source is not included and the canonical implementation is new; do not claim source identity, byte identity, algorithm equivalence, historical-test equivalence or that historical application defects have been fixed.

- Preserve the original files under `evidence/` and `evidence/import/`. Add new evidence separately.
- Keep proposed behavior, historical observations and newly executed checks clearly labeled.
- Preserve requirement IDs R1–R8/T1–T4, invariants I01–I12, findings F01–F14, gates G00–G12 and waves W0–W7.
- The normal W0 path is `tools/w0_repo_baseline.py` and must use Git/package/runtime facts only. `tools/source_preflight.py` and `tools/w0_baseline.py` are optional legacy historical-source compatibility utilities.
- After edits, run `python3 tools/check_package.py --refresh-manifest`, `python3 -m unittest discover -s tests -v`, and `git diff --check`.
- Keep the canonical application narrow; do not copy NiceGUI Base internals or invent missing company bindings. Future F05 checks must target `src/ephi` and report `NOT_IMPLEMENTED`/`NOT_RUN` until real APIs exist; F02/F03/F04 checks must use fresh canonical execution.
- When application work becomes possible, follow 11_Developer_Start.md and stop at the authorized wave's exit criteria.
