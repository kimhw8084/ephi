# Semiconductor sustained-operations renewal and continuity

Wave 74 adds a bounded evidence-renewal layer above Wave 73. It does not replace Waves 66–73 promotion, publication, stability, rollback-readiness, sustained-operations acceptance, or incident/rollback audit truth.

## Evidence renewal

`SustainedOperationsEvidenceRenewal` binds a new external renewal evidence set to the exact Wave 73 `IncidentRollbackAuditClosure`, sustained-operations acceptance identity, immutable Wave 73 audit ZIP hash, current NiceGUI Base framework identity, and exact required NiceGUI version. Historical Wave 73 `ACCEPTED` and `CLOSED` records are never rewritten.

The default `stable` policy is a configurable framework policy, not a company SLA. It currently uses a 168-hour maximum age, a 24-hour expiring warning window, and five minutes of tolerated future-clock skew. External company policy can supply another `SustainedOperationsRenewalPolicy` without creating a second evidence authority.

Freshness diagnostics distinguish `CURRENT`, `EXPIRING`, `EXPIRED`, `MISSING`, and `CONTRADICTORY`. Missing or expired real evidence remains PENDING. Invalid/future-clock contradictory evidence, changed artifact bytes, archive corruption, or framework/NiceGUI identity mismatch is BLOCKED.

## Operational assurance continuity

`OperationalAssuranceContinuityDossier` combines independent Wave 73 archive verification with the current renewal verification. Its evidence state is `ASSURED`, `EXPIRING`, `PENDING`, or `BLOCKED`. `EXPIRING` is intentionally distinct from failure so operators can renew before evidence expires.

`package_operational_assurance_continuity_dossier()` creates a deterministic self-contained ZIP containing the exact Wave 73 audit package, Wave 74 renewal record, still-hash-valid renewal artifacts, and `MANIFEST.sha256`. PENDING/EXPIRING dossiers can be packaged for gap/renewal handoff; BLOCKED dossiers are refused.

## Safety boundary

NiceGUI Base does not continuously monitor production, execute incident response, execute rollback, deploy, publish, or approve stable `3.0.0`. Wave 74 only verifies evidence supplied by external company processes. All unavailable real company/runtime/browser/human/operations evidence remains PENDING.
