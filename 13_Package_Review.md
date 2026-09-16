# Package review and implementation readiness

Review date: 2026-09-15 America/Chicago. Design revision: 0.2.

## Assessment

The package is a substantial implementation design with useful separation of science, operational urgency, human workflow and economic evidence. Its strongest choices are immutable decisions, qualified affirmative recovery, durable commands and a single canonical episode workspace. Its historical audit includes concrete synthetic observations and appropriately limits the meaning of portable tests.

It is ready to serve as a versioned design repository. It is **not a runnable application**, a completed W0 baseline, or a qualified release. The supplied archive contains 13 design chapters, nine audit artifacts and a manifest; it contains no original application source, dependency lockfile, migrations or browser implementation. Creating replacements from the excerpts would discard the scientific investment the design explicitly intends to retain.

The repository root is `ephi`; the archive's enclosing `EPHI_1.0_Design_Pack/` directory was removed during extraction. The original ZIP is retained locally and ignored by Git. The split chapters are the maintained design; an absent companion master is not a required build input.

## Corrections in this review

P-identifiers below concern the package and contract review. Historical F-identifiers continue to describe the original application's findings.

| ID | Finding and correction | Requirements / gates |
|---|---|---|
| P01 | No repository entry point or repeatable package validation. Added README, contributor/agent instructions, a standard-library checker, regression tests and CI. | T4; package checks only |
| P02 | Original manifest mixed historical audit state with delivery claims and referenced an absent companion. Preserved it under `evidence/import/`, added current hashes, named all missing inputs, and separated historical from current evidence. | T1/T4; G00 remains pending |
| P03 | “One transaction” did not specify a coherent multi-statement read or prevent competing projection writers. Specified a single statement/repeatable-read snapshot, shared episode locking and owned-field updates. | R2/R3, I10/I12; G04/G05/G07 |
| P04 | Receipt lookup alone left concurrent first attempts and lost responses ambiguous. Added unique-receipt rollback/re-read behavior, payload identity, same-ID retry semantics, current authorization and archived deduplication identities. | T2, I10; G05/G07/G10 |
| P05 | Source availability could be confused with knowledge actually published in EPHI. Defined AS_KNOWN, SOURCE_REPLAY and RESTATED modes with a late-ingestion example and pinned historical workflow. | T1, I01/I02/I07; G02 |
| P06 | Optional expected/viewed revisions left decision-sensitive write preconditions unclear. Required versions for existing aggregates and revalidation of decision prerequisites, while preserving honest historical observations. | R5, T2; G03/G05/G07 |
| P07 | A stable cursor lacked a retained row-version mechanism. Required immutable query snapshots, current permission checks and explicit expiry without browser-long database transactions. | R2, T3; G07/G10 |
| P08 | Ledger correction rules needed period-movement, approval-time, chain and monetary precision semantics. Specified active-leaf selection before event-period filtering, constrained successors and exact decimals. | R7, I11; G02/G11 |
| P09 | Generic downstream “action” delivery could be interpreted as a manufacturing control path. Restricted V1 delivery to approved work requests/tickets/notifications and retained human external execution. | R1/R5, ADR 10; G07/G12 |
| P10 | Permission lifetime on receipts/artifact links/notifications needed explicit treatment. Required authorization at disclosure/dispatch, added concrete regression obligations, and kept framework inspection distinct from installed API qualification. | T2/T4; G07/G08/G09 |

The affected application behavior is still proposed. None of these document corrections demonstrates an implemented F03/F04/F05 fix.

## Evidence and verification

| Check | Result and scope |
|---|---|
| ZIP integrity and original manifest | PASS: archive CRC check; all 22 listed files matched size and SHA-256 before edits |
| Original evidence preservation | PASS: all nine historical artifacts retain their imported hashes |
| Historical audit consistency | PASS: 409 inventory records; 237 core / 18 company / 147 test Python files with reported line counts; 53 explicit API routes; JUnit/log agree on 275 passed / one skipped |
| NiceGUI Base pin | PASS for source inspection: pinned public commit and metadata confirm version 3.0.0a8, NiceGUI 3.15.0 and Python >=3.11,<3.14; file hashes saved in the review evidence |
| Current package checks | PASS locally on Python 3.14.5: `python3 tools/check_package.py`; integrity, syntax, local links/fences, traceability presence and historical consistency |
| Checker regression suite | PASS: 12 tests via `python3 -m unittest discover -s tests -v`; changed/missing/extra files, damaged evidence, invalid syntax, broken links and other rejection paths |
| Application tests and defect corrections | NOT_RUN: original application source absent |
| Installed Base, browser, company sources, persistence and scientific qualification | NOT_RUN: no implementation/target environment in this package |
| Original mission completeness | NOT_VERIFIABLE: original attachment absent; internal A–S and requirement traceability retained |

Exact import/review identities are recorded in [the review evidence](evidence/review/package_review.json). Current file identities are in [manifest.json](manifest.json). CI evaluates package checks for the exact commit; it does not run the historical application's test suite. Source inspection cannot establish installed constructor compatibility or browser quality.

## Implementation blockers and next slice

1. **Restore the exact original application source.** Verify its recorded archive hash and reproduce the baseline before modifying algorithms. The repository is not a substitute for that input.
2. **Resolve framework and dependency bindings.** Inspect the installed pinned catalog, produce the binding manifest and lock a compatible environment; the original audit lacked NiceGUI, Polars and PyArrow.
3. **Complete W0 and the smallest W1 slice.** Add F03/F04/F05 regressions and fixes, then one scoped durable claim/acknowledge path through the actual Base UI with conflict/restart evidence. Do not multiply screens before that works.
4. **Bind company-specific evidence before a pilot.** Identity, backend/artifact store, source mappings, family thresholds, action authority and operational policies remain the gates already defined in the design.

No application deployment, scientific qualification or production-readiness claim follows from publishing this repository. The next authorized implementation can proceed from [11_Developer_Start.md](11_Developer_Start.md) once the source prerequisite is supplied.
