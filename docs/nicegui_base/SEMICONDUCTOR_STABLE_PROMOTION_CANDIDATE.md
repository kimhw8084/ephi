# Semiconductor stable promotion candidate — agent guide

When asked to assimilate enterprise target evidence or prepare a stable-channel candidate:

1. Reuse Wave 65 target-evidence bundles and Wave 66 qualification/promotion authorities. Do not create alternate gate semantics.
2. Build `EnterpriseTargetEvidenceSet` with `assimilate_enterprise_target_evidence(...)` from actual supplied evidence and operational-readiness reports.
3. Keep missing, stale, untraced, legacy-identity, or unavailable evidence PENDING; failed/corrupt/mismatched evidence remains BLOCKED.
4. Record promotion, rollback, incident, and evidence-capture rehearsals with `build_promotion_rehearsal(...)` against the canonical Wave 66 runbook. Rehearsals never change target-gate status.
5. Build `StablePromotionCandidate` and use its `gaps` / `next_actions` for remediation.
6. Package with `package_stable_promotion_candidate(...)`; only currently hash-verified artifact bytes may be copied into the ZIP.
7. A packaged candidate is not a promoted release. `release_channel_ready` requires the underlying Wave 66 decision to be PROMOTABLE plus all required rehearsals PASS.
8. Keep provider/database/auth/proxy/deployment specifics behind adapters/configuration unless authoritative company requirements are supplied.

CLI: `nicegui-base promotion-candidate <evidence...> [--operational-readiness <report>] [--rehearsal <report>] --package <candidate.zip>`.
