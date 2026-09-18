# Source identities, verification scope and reproducibility

## Evidence hierarchy

**Delivery boundary:** this repository is the canonical Git executable source for the CHG-111 R3 baseline. The design-pack and earlier application-source records below are historical provenance only; they are not installation, package-validation or W0 prerequisites. All historical executions below belong to the earlier audit. See [13_Package_Review.md](13_Package_Review.md) for the current review and [evidence/README.md](evidence/README.md) for preserved provenance.

The historical EPHI source archive is the provenance authority for the historical findings only. The canonical implementation authority is this Git repository. The user's original mission was the requested product/design brief, not evidence that a feature or test already exists. Pinned NiceGUI Base source describes template capabilities and construction rules, not target certification. External official documentation supports limited technical platform choices. Proposed architecture, policies, load envelopes, interface names and UI behavior are design decisions, not claims about the current implementation.

## Local source and evidence

| Source | Identity / scope |
|---|---|
| Historical EPHI source archive | Filename/SHA-256 are preserved here as `REFERENCE_ONLY` provenance; the artifact is not a canonical checkout prerequisite |
| Source root | `ephi_v0.19.1_production_hardened_release/` |
| User mission | `붙여넣은 마크다운(1)(1).md`; available in the original audit conversation, not in this delivery |
| Python source inventory | `evidence/source_inventory.json`: relative paths, file hashes, line counts and parsed symbols; includes additional Python scripts beyond the three summarized directories |
| Audit environment | `evidence/audit_environment.json`: interpreter, installed/absent relevant packages and API route inventory |
| Portable test execution | `evidence/pytest.log` and `evidence/pytest_results.xml`; 275 passed, 1 optional PyArrow module skipped |
| Four synthetic behavior probes | `evidence/behavior_probes.py`, `.json`, `.log`; F02, F03, F04, F05 observations, not fixes |
| Selected source excerpts | `evidence/source_excerpts.md`; line-numbered extracts supporting key findings |
| Pack checks | `evidence/design_pack_checks.json`: structural/source-identity checks, not implementation or browser tests |

The preserved behavior-probe harness and outputs are historical observations only. They are not part of the canonical W0 command path. The canonical CHG-109 runner targets `src/ephi` and reports the four behavioral checks as `NOT_IMPLEMENTED`/`NOT_RUN` until future changes implement them. In the historical source, F03's absent work row, F04's zero historical cost and F05's resolved state are reproduced defects/product mismatches, not successful product acceptance. F02 deliberately omits the separate checkpoint restore and establishes that restoring source rows alone is insufficient; it does not establish failure of the full checkpoint restoration path.

In the historical audit, the application archive/source was not patched, committed or pushed. This repository publishes the separate design and evidence; it does not change that application's source or historical results.

## NiceGUI Base pinned references

Repository: `kimhw8084/nicegui-base`. Inspected commit: `000298562d6bcbf6df304edbd41b98b30fe4bfcf`. Read through the connected GitHub service. This is an inspected candidate, not an assertion of user approval or completed target/browser qualification.

- Commit: `https://github.com/kimhw8084/nicegui-base/commit/000298562d6bcbf6df304edbd41b98b30fe4bfcf`
- Framework agent contract: `https://github.com/kimhw8084/nicegui-base/blob/000298562d6bcbf6df304edbd41b98b30fe4bfcf/AGENTS.md`
- Construction manifest: `https://github.com/kimhw8084/nicegui-base/blob/000298562d6bcbf6df304edbd41b98b30fe4bfcf/source/nicegui_base/ai/construction_manifest.json`
- Public exports: `https://github.com/kimhw8084/nicegui-base/blob/000298562d6bcbf6df304edbd41b98b30fe4bfcf/source/nicegui_base/__init__.py`
- Golden workspace example: `https://github.com/kimhw8084/nicegui-base/blob/000298562d6bcbf6df304edbd41b98b30fe4bfcf/examples/nicegui_base/golden_analysis_workspace.py`
- Governed design tokens: `https://github.com/kimhw8084/nicegui-base/blob/000298562d6bcbf6df304edbd41b98b30fe4bfcf/source/nicegui_base/design/tokens.py`
- Repository README: `https://github.com/kimhw8084/nicegui-base/blob/000298562d6bcbf6df304edbd41b98b30fe4bfcf/README.md`

These sources establish the framework version/runtime pin, public-API rule, registered pattern/visualization approach, shared analysis/workspace ownership, target-evidence separation, documented discovery commands and actual token values cited in this design. They do not establish that every proposed EPHI composition or constructor has been implemented. Resolve exact selected signatures through the pinned installed catalog in W0 and store the binding manifest.

## External primary documentation

Consulted on 2026-09-15 America/Chicago:

| Reference | Limited use in the design |
|---|---|
| PostgreSQL SELECT — `https://www.postgresql.org/docs/current/sql-select.html` | Queue claims using row locking/SKIP LOCKED; not a consistent analytical snapshot mechanism |
| [PostgreSQL 18 transaction isolation](https://www.postgresql.org/docs/18/transaction-iso.html) | Repository-review clarification of coherent multi-statement reads; not a company server-version selection |
| NiceGUI — `https://nicegui.io/documentation` | Event-loop considerations and I/O/CPU offloading; product integrates through NiceGUI Base authorities |
| FastAPI lifespan — `https://fastapi.tiangolo.com/advanced/events/` | Explicit startup/shutdown ownership for runtime resources |
| W3C WCAG 2.2 — `https://www.w3.org/TR/WCAG22/` | Accessibility target and browser review criteria; no conformance certification asserted |

PostgreSQL is a proposed operational backend subject to company approval. No specific company installation, version, capacity, credentials, external action endpoint or data mapping was discovered. Proposed SLOs, pilot response times, retention and recovery objectives require target measurement/approval.

## Not executed or not available

NiceGUI Base's full source suite; installed NiceGUI/Base smoke; browser rendering or interactions; visual regression; accessibility conformance; real company database/source/identity integration; optional PyArrow/Polars paths; fault-injection tests for the proposed durable backend; load/performance benchmarks; family scientific qualification; production backup/restore; real-fab ROI measurement. They are explicitly assigned release gates rather than represented as completed.
