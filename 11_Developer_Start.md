# Developer execution brief

Implement this design in vertical slices. The authoritative product choices are in this pack; source-derived behavior and proposed changes are distinguished in `02_Source_Audit.md`. Do not blindly reuse earlier chat claims or a floating GitHub main.

## Repository prerequisite

The normal starting point is a fresh Git checkout. Run the package check, canonical tests, and Git-native W0 baseline before expanding the new implementation:

```bash
python3 tools/check_package.py
python3 -m unittest discover -s tests -v
python3 tools/w0_repo_baseline.py
python3 -m ephi --self-check
```

The application is a new canonical implementation under `src/ephi`, not a reconstruction of the historical audit source. Do not request or reconstruct historical application files from excerpts. Historical archive filename/SHA details remain `REFERENCE_ONLY` provenance only.

## Optional historical-source compatibility

`tools/source_preflight.py` and `tools/w0_baseline.py` remain available for explicitly scoped historical-source compatibility work. They are optional and are not prerequisites for application work, package validation, canonical tests, or any default W0 gate.

## First branch: W0 and the smallest W1 slice

1. Keep the repository-native package, import, entrypoint, and deterministic test baseline passing. CHG-109 canonical F02/F03/F04/F05 checks remain `NOT_IMPLEMENTED`/`NOT_RUN` until their APIs are added under `src/ephi`.
2. Pin NiceGUI Base `000298562d6bcbf6df304edbd41b98b30fe4bfcf` as the inspected candidate. Read its AGENTS/construction contract, use its catalog/golden patterns, and save actual selected APIs in a binding manifest. No invented constructor signatures and no copied demo renderers.
3. Implement application context/DTO/errors, the unit-of-work/receipt contract and bounded episode/attention read ports. Adapt existing services instead of cloning them. Implement one durable repository path and migration with real transactional integration tests.
4. Render a single honest fixture episode through installed Base Attention → Episode. Allow scoped claim/acknowledge, persist it, restart the application and verify the state. Demonstrate stale/unavailable/conflict/reconnect behavior. No production profile may fall back to fixtures or memory.

**First demonstrable result:** two permitted sessions see the same canonical episode; one claims it; the other receives a version conflict rather than silently overwriting; after process restart the claim, audit and evidence revision remain; technical recovery does not hide the open work; low-quality data does not prove recovery; historical ledger values remain correct after correction.

Do not start with a new detector, chatbot, custom CSS system, seven empty pages, microservices, or a giant untyped configuration DSL. Implement only modules required by the current vertical slice while preserving the target boundaries.

## Evidence required with every delivery

Report exact starting/ending source identity, files changed, requirement IDs, commands run and PASS/FAIL/NOT_RUN, saved logs/screenshots, pre-existing versus new warnings, source/target/qualification scope, migration/rollback and remaining blockers. Never claim target/browser/runtime PASS from code inspection. Never label pending company evidence as complete.

Stop the wave when its exit criteria pass; present the runnable artifact and next wave's concrete scope. Do not silently expand into another redesign. Do not alter the NiceGUI Base framework or push/overwrite unrelated repository work without the user's requested scope.
