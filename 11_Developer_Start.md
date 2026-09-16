# Developer execution brief

Implement this design in vertical slices. The authoritative product choices are in this pack; source-derived behavior and proposed changes are distinguished in `02_Source_Audit.md`. Do not blindly reuse earlier chat claims or a floating GitHub main.

## Repository prerequisite

This repository currently contains the design and audit evidence only. The original application archive named in [README.md](README.md) must be obtained and hash-verified before application work. The companion master is unnecessary for reading this maintained split design; the original mission attachment is unavailable for an independent completeness check. Run the package checker first, then keep newly executed source/framework results separate from the imported evidence. Do not reconstruct 409 inventoried Python files from excerpts or substitute a new demo for the audited application.

## Bounded W0 restoration preflight

Use the repository-native preflight before opening application work:

```bash
python3 tools/source_preflight.py
```

The command expects the owner-supplied `ephi_v0.19.1_production_hardened(1).zip` at ignored `artifacts/source/`, verifies SHA-256 `5e9ad8f63b3158adc530af69fc650aec606cbfa64896ba73840cdcf994a2b6e3`, rejects unsafe ZIP members and protected evidence paths, and stages only verified members in a fresh ignored directory. It writes a machine-readable result to `artifacts/source-preflight.json`. `SOURCE_REQUIRED` is the expected current result while the archive is absent; it is not a successful baseline. A verified result discovers the source's Python/dependency/test inventory and runtime, but leaves source test execution explicitly `NOT_RUN` until an engineer runs and records that baseline.

The JSON's historical 275 passed / 1 skipped value is an immutable `REFERENCE_ONLY` audit fact. Never report it as a test result from this repository or as a newly executed source baseline. This preflight is source restoration and inspection only; it does not reconstruct application code and does not fix or claim F03/F04/F05.

## First branch: W0 and the smallest W1 slice

1. Run the bounded W0 source preflight and inspect its recorded source/dependency/test reality. With `SOURCE_STAGED`, restore the same source in isolation, run the full suite and optional integrations required by the chosen target, and read the four audit probes. Add regression tests for F03 open-work continuity, F04 as-of supersession and F05 recovery validity; define the revised qualified recovery policy.
2. Pin NiceGUI Base `000298562d6bcbf6df304edbd41b98b30fe4bfcf` as the inspected candidate, or record an explicitly approved replacement and its new evidence. Read its AGENTS/construction contract, use its catalog/golden patterns, and save actual selected APIs in a binding manifest. No invented constructor signatures and no copied demo renderers.
3. Implement application context/DTO/errors, the unit-of-work/receipt contract and bounded episode/attention read ports. Adapt existing services instead of cloning them. Implement one durable repository path and migration with real transactional integration tests.
4. Render a single honest fixture episode through installed Base Attention → Episode. Allow scoped claim/acknowledge, persist it, restart the application and verify the state. Demonstrate stale/unavailable/conflict/reconnect behavior. No production profile may fall back to fixtures or memory.

**First demonstrable result:** two permitted sessions see the same canonical episode; one claims it; the other receives a version conflict rather than silently overwriting; after process restart the claim, audit and evidence revision remain; technical recovery does not hide the open work; low-quality data does not prove recovery; historical ledger values remain correct after correction.

Do not start with a new detector, chatbot, custom CSS system, seven empty pages, microservices, or a giant untyped configuration DSL. Implement only modules required by the current vertical slice while preserving the target boundaries.

## Evidence required with every delivery

Report exact starting/ending source identity, files changed, requirement IDs, commands run and PASS/FAIL/NOT_RUN, saved logs/screenshots, pre-existing versus new warnings, source/target/qualification scope, migration/rollback and remaining blockers. Never claim target/browser/runtime PASS from code inspection. Never label pending company evidence as complete.

Stop the wave when its exit criteria pass; present the runnable artifact and next wave's concrete scope. Do not silently expand into another redesign. Do not alter the NiceGUI Base framework or push/overwrite unrelated repository work without the user's requested scope.
