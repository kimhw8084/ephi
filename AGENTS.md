# Repository instructions

This is the EPHI design and evidence repository. Read README.md and 13_Package_Review.md before changing it. The original application source is not included; do not claim that application defects have been fixed or that its tests were rerun.

- Preserve the original files under `evidence/` and `evidence/import/`. Add new evidence separately.
- Keep proposed behavior, historical observations and newly executed checks clearly labeled.
- Preserve requirement IDs R1–R8/T1–T4, invariants I01–I12, findings F01–F14, gates G00–G12 and waves W0–W7.
- After edits, run `python3 tools/check_package.py --refresh-manifest`, `python3 -m unittest discover -s tests -v`, and `git diff --check`.
- Do not create placeholder application modules, copy NiceGUI Base internals, or invent missing company bindings.
- When application work becomes possible, follow 11_Developer_Start.md and stop at the authorized wave's exit criteria.
