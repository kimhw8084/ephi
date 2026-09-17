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

[review/w0_integrity_regression_contract.json](review/w0_integrity_regression_contract.json) is the CHG-109 W0 contract for source-bound F02/F03/F04/F05 regression evaluation. Its historical observations remain reference-only; the runner's fresh probe and checkpoint results belong under ignored `artifacts/` runtime output.

Use the commands in [the README](../README.md) for current package checks; consult GitHub Actions for checks on a particular published commit. Do not overwrite these historical results when running a new audit.
