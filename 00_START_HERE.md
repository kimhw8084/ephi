# EPHI 1.0 — Engineering Decision System
## Master design and implementation handoff

**Design date:** 2026-09-15 America/Chicago. **Status:** maintained target design; the repository now has a canonical package baseline but no production certification.

**Repository revision 0.2:** the supplied design pack is maintained directly in the `ephi` repository root. Begin with [README.md](README.md) for current package checks and [13_Package_Review.md](13_Package_Review.md) for the review. The canonical application is a new implementation under `src/ephi`; historical application results below remain reference-only.

**Decision:** retain EPHI's analytical investment, repair the verified integrity/product gaps, and build one complete engineer decision loop on the installed NiceGUI Base platform. The primary product is a work queue plus a canonical episode workspace, not a collection of dashboards.

Read `01_Product_and_Architecture.md` for the one-page architecture and requirements. Implement using `03_Application_Contracts.md` through `09_Delivery_and_Gates.md`. `02_Source_Audit.md` contains actual findings and evidence limitations. `10_Traceability_and_Decisions.md` maps the mission's A–S outputs and all requirements. `11_Developer_Start.md` is the execution handoff. `12_Sources_and_Evidence.md` provides pinned references, reproducibility instructions and unexecuted gates.

## Historical provenance (REFERENCE_ONLY; not a repository prerequisite)

- Historical EPHI input filename: `ephi_v0.19.1_production_hardened(1).zip`.
- SHA-256: `5e9ad8f63b3158adc530af69fc650aec606cbfa64896ba73840cdcf994a2b6e3`.
- Archive source root: `ephi_v0.19.1_production_hardened_release/`.
- NiceGUI Base inspected candidate: `kimhw8084/nicegui-base` at `000298562d6bcbf6df304edbd41b98b30fe4bfcf`.
- Framework contract identifies `nicegui_base 3.0.0a8` and exactly `nicegui==3.15.0`, Python 3.11–3.13. This is a compatibility constraint, not a suggestion to install the latest NiceGUI.
- Product mission: the user's attached `붙여넣은 마크다운(1)(1).md`.

## What was actually verified

EPHI source inventory: 237 core Python files / 27,778 lines; 18 company-port Python files / 776 lines; 147 test Python files / 5,624 lines. Lines include comments and blank lines. Fresh portable suite: **275 passed, 1 skipped**; optional PyArrow integration skipped. Four additional local synthetic probes are included. NiceGUI Base was inspected through its pinned framework contract, construction manifest, public exports, README and golden workspace example; its full test suite and browser were not run here.

The earlier 274-pass count is superseded. “Production hardened” is a package name and intent, not proof that company data, durable stores, identity or the UI are operational. The test environment also lacks NiceGUI, Polars and PyArrow; a production installation/lockfile gate remains necessary even though the portable tests pass.

## Priority decisions

1. Correct false recovery, disappearing open work, historical ledger supersession and source/checkpoint consistency before production use.
2. Use one product application layer, durable transactions, published read models and immutable evidence. Do not duplicate existing domain algorithms.
3. Use NiceGUI Base public authorities, registered semiconductor visuals and shared workspace state. Do not fork the template or copy demo calculations.
4. Make metrology the default first family, subject to source/action readiness. Deliver honest limited capabilities rather than fabricate WIP or ROI.
5. Start with a deterministic check-selection planner. Hypothesis support scores are not calibrated probabilities.
6. Keep equipment holds, routing changes and disposition in approved human-controlled systems. EPHI records proposals, acknowledgments and observed actions.

## Scope of completeness

This pack defines product requirements, interfaces, schemas, interaction behavior, algorithms, operational policies, acceptance gates and implementation sequence. Company endpoints, approved database/object-store credentials, real source mappings, actual intervention authority and family-specific scientific thresholds are not present in the input. Their integration contracts and required evidence are defined; their values are not invented. Source-stage and target-stage release gates remain distinct.

**Begin with W0 in `09_Delivery_and_Gates.md`. Do not start by drawing seven pages or rewriting the detectors.**
