# Evidence provenance

The nine files below were supplied in `EPHI_1.0_Design_Pack.zip` and remain byte-for-byte unchanged:

- [audit_environment.json](audit_environment.json)
- [behavior_probes.py](behavior_probes.py), [behavior_probes.json](behavior_probes.json), [behavior_probes.log](behavior_probes.log)
- [design_pack_checks.json](design_pack_checks.json)
- [pytest.log](pytest.log), [pytest_results.xml](pytest_results.xml)
- [source_excerpts.md](source_excerpts.md), [source_inventory.json](source_inventory.json)

They describe the **earlier source audit**, not an application run in this repository. Its JUnit record contains 276 test cases: 275 passed and one optional PyArrow skip. `design_pack_checks.json` reports checks made in the original audit environment; its source-archive assertions cannot be rerun here without the original application archive.

[import/original_manifest.json](import/original_manifest.json) preserves the supplied manifest, including its historical `github_writes_performed: false` and separately delivered companion-master reference. Those statements are scoped to that import. The companion master and original EPHI source archive were not included in the design ZIP.

[review/package_review.json](review/package_review.json) records this repository review, and [review/base_reference_check.json](review/base_reference_check.json) records the pinned framework files inspected on GitHub. [review/nicegui_base_runtime_evidence.json](review/nicegui_base_runtime_evidence.json) is the newly executed CHG-105 installed-runtime summary; [review/nicegui_base_binding_manifest.json](review/nicegui_base_binding_manifest.json) links it while preserving the earlier CHG-104 source-inspection record. Framework/runtime execution remains distinct from application, browser and company qualification.

[review/w0_integrity_regression_contract.json](review/w0_integrity_regression_contract.json) is the CHG-109 W0 contract for canonical F02/F03/F04 and out-of-scope F05 regression evaluation. Its historical observations remain reference-only; the runner's fresh canonical results belong under ignored `artifacts/` runtime output.

Use the commands in [the README](../README.md) for current package checks; consult GitHub Actions for checks on a particular published commit. Do not overwrite these historical results when running a new audit.

New implementation evidence is kept separately under `evidence/u2/` and
`evidence/u3/`. The
[CHG-234 U2.4 Asset 360 report](u2/chg-234-u2.4-asset-360/qualification.json)
binds its screenshots and browser inventories to a working-tree candidate
based on the registered `main@4482233202ff2667262ff0d47ff390e72d398d3c`
target. Its fixture is synthetic and its non-claims are recorded in the
report; it does not change or extend the historical audit records above.
The R2 determinism repair is recorded separately at
[the CHG-234 review-fix1 continuation](u2/chg-234-u2.4-asset-360/review-fix1/qualification.json);
it preserves the predecessor report and qualifies cutoff-pinned O4 source
revisions, unavailable reads, and the refreshed source labels.

The CHG-258 U3.1
[release/install qualification](u3/chg-258-u3.1/qualification.json) binds the
offline CPython 3.11–3.13 installation proofs and package identity checks to
its exact candidate source commit/tree. Its evidence remains packaging-only;
it leaves provider composition separate and keeps company and Production
qualification boundaries explicit.

The final generic U2 breadth slice, CHG-252 U2.5/O9.2, has separate
[candidate-bound Operations evidence](u2/chg-252-u2.5-o9.2/qualification.json)
with synthetic PostgreSQL 18 fixtures and desktop/phone screenshots. It keeps
the six O9 axes independent, reports disabled H6 controls without an audited
runtime capability, and leaves production RPO/RTO `NOT_ESTABLISHED`.
The R2 FIX continuation is preserved separately at
[review-fix1](u2/chg-252-u2.5-o9.2/review-fix1/qualification.json); it carries
the predecessor forward, verifies all canonical O2 worker statuses, and
qualifies authorized exact worker status/type filtering without changing
scope-wide worker health facts. Its test, package, W0 and browser results are
summarized in the [final verification record](u2/chg-252-u2.5-o9.2/review-fix1/validation/final-verification.json).
