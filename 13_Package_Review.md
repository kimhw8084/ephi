# Package review and implementation readiness

Review date: 2026-09-15 America/Chicago. Design revision: 0.2.

## Assessment

The package is a substantial implementation design with useful separation of science, operational urgency, human workflow and economic evidence. Its strongest choices are immutable decisions, qualified affirmative recovery, durable commands and a single canonical episode workspace. Its historical audit includes concrete synthetic observations and appropriately limits the meaning of portable tests.

It is ready to serve as a versioned design repository and now contains the smallest installable canonical application boundary plus the scoped W0 F02/F03 advisory/workflow integrity slice. It is **not a qualified release**. The canonical package contains no broad UI, company adapters, production storage or scientific implementation. It is a new repository implementation; it is not a reconstruction of the historical audited application.

The repository root is `ephi`. The split chapters are the maintained design, and an absent companion master is not a build input. Historical archive/source facts remain preserved as `REFERENCE_ONLY` provenance; they are not required for checkout, installation or the canonical W0 path.

## Corrections in this review

P-identifiers below concern the package and contract review. Historical F-identifiers continue to describe the original application's findings.

| ID | Finding and correction | Requirements / gates |
|---|---|---|
| P01 | No repository entry point or repeatable package validation. Added README, contributor/agent instructions, a standard-library checker, regression tests and CI. | T4; package checks only |
| P02 | Original manifest mixed historical audit state with delivery claims and referenced an absent companion. Preserved it under `evidence/import/`, added current hashes, separated historical from current evidence, and added the canonical Git-native identity. | T1/T4; G00 canonical baseline |
| P03 | “One transaction” did not specify a coherent multi-statement read or prevent competing projection writers. Specified a single statement/repeatable-read snapshot, shared episode locking and owned-field updates. | R2/R3, I10/I12; G04/G05/G07 |
| P04 | Receipt lookup alone left concurrent first attempts and lost responses ambiguous. Added unique-receipt rollback/re-read behavior, payload identity, same-ID retry semantics, current authorization and archived deduplication identities. | T2, I10; G05/G07/G10 |
| P05 | Source availability could be confused with knowledge actually published in EPHI. Defined AS_KNOWN, SOURCE_REPLAY and RESTATED modes with a late-ingestion example and pinned historical workflow. | T1, I01/I02/I07; G02 |
| P06 | Optional expected/viewed revisions left decision-sensitive write preconditions unclear. Required versions for existing aggregates and revalidation of decision prerequisites, while preserving honest historical observations. | R5, T2; G03/G05/G07 |
| P07 | A stable cursor lacked a retained row-version mechanism. Required immutable query snapshots, current permission checks and explicit expiry without browser-long database transactions. | R2, T3; G07/G10 |
| P08 | Ledger correction rules needed period-movement, approval-time, chain and monetary precision semantics. Specified active-leaf selection before event-period filtering, constrained successors and exact decimals. | R7, I11; G02/G11 |
| P09 | Generic downstream “action” delivery could be interpreted as a manufacturing control path. Restricted V1 delivery to approved work requests/tickets/notifications and retained human external execution. | R1/R5, ADR 10; G07/G12 |
| P10 | Permission lifetime on receipts/artifact links/notifications needed explicit treatment. Required authorization at disclosure/dispatch, added concrete regression obligations, and kept framework inspection distinct from installed API qualification. | T2/T4; G07/G08/G09 |

The affected application behavior is still proposed. CHG-118 adds only the bounded canonical F05 recovery integrity slice; it does not qualify a production family or implement the broader recovery plan.

## Evidence and verification

| Check | Result and scope |
|---|---|
| ZIP integrity and original manifest | PASS: archive CRC check; all 22 listed files matched size and SHA-256 before edits |
| Original evidence preservation | PASS: all nine historical artifacts retain their imported hashes |
| Historical audit consistency | PASS: 409 inventory records; 237 core / 18 company / 147 test Python files with reported line counts; 53 explicit API routes; JUnit/log agree on 275 passed / one skipped |
| NiceGUI Base pin | PASS for source inspection: pinned public commit and metadata confirm version 3.0.0a8, NiceGUI 3.15.0 and Python >=3.11,<3.14; file hashes saved in the review evidence |
| Current package checks | PASS when run on a supported interpreter: `python3 tools/check_package.py`; integrity, syntax, local links/fences, traceability presence and historical consistency |
| Checker regression suite | PASS: 12 tests via `python3 -m unittest discover -s tests -v`; changed/missing/extra files, damaged evidence, invalid syntax, broken links and other rejection paths |
| Canonical application self-check and repository tests | PASS/IMPLEMENTED capability for F02/F03/F04/F05; fresh F02/F03/F04/F05 execution is separately reported by `tools/w0_integrity_regressions.py` |
| Installed Base, browser, company sources, persistence and scientific qualification | NOT_RUN: no implementation/target environment in this package |
| Original mission completeness | NOT_VERIFIABLE: original attachment absent; internal A–S and requirement traceability retained |

Exact import/review identities are recorded in [the review evidence](evidence/review/package_review.json). Current file identities are in [manifest.json](manifest.json). CI evaluates package checks for the exact commit; it does not run the historical application's test suite. Source inspection cannot establish installed constructor compatibility or browser quality.

## Implementation blockers and next slice

1. **Keep the canonical baseline reproducible.** Run the Git-native baseline, package checks and offline tests from a fresh checkout; do not add an artifact prerequisite.
2. **Resolve framework and dependency bindings.** Use the pinned catalog, exact dependency declarations and the existing CHG-105 runtime tool; installation/bootstrap is separately reported from offline repository checks.
3. **Continue beyond the bounded F05 slice.** Add one scoped durable claim/acknowledge path through the actual Base UI with conflict/restart evidence. Do not treat the W0 recovery fixture as family qualification or multiply screens before that works.
4. **Bind company-specific evidence before a pilot.** Identity, backend/artifact store, source mappings, family thresholds, action authority and operational policies remain the gates already defined in the design.

No browser, scientific or production-readiness claim follows from publishing this repository. The next authorized implementation can proceed from [11_Developer_Start.md](11_Developer_Start.md) against the canonical package.

## CHG-105 W0 runtime delta

The independent framework/dependency qualification slice is recorded separately in [the runtime evidence](evidence/review/nicegui_base_runtime_evidence.json) and linked from [the NiceGUI Base binding manifest](evidence/review/nicegui_base_binding_manifest.json). It verified an isolated Python 3.11.7 environment, the exact NiceGUI Base VCS commit/version, exact `nicegui==3.15.0`, and 21/21 CHG-104 public-root authority imports. All six requested installed discovery commands returned machine-readable output. `runtime-contract` and browserless framework `runtime-smoke --port 0` PASS; application/browser/production qualification remain NOT_RUN. The canonical package self-check and Git-native baseline are the current W0 execution targets.
