# Semiconductor post-release stability evidence and rollback readiness

Wave 72 extends the single fail-closed release-evidence chain after Wave 71. It does **not** introduce a monitoring, incident-management, deployment, publication, or rollback authority.

## 1. Verify the exact Wave 71 package

`verify_stable_post_promotion_archive()` independently checks archive safety, unique entries, exact `MANIFEST.sha256` coverage, all content hashes, the persisted `StablePostPromotionVerification` identity, the separately embedded publication-evidence identity, and the embedded Wave 70 closure-package binding.

Changed, corrupt, incomplete, unsafe, or wrong-subject packages are **BLOCKED**.

## 2. Assimilate production stability evidence

`PostReleaseStabilityEvidence` binds external production evidence to the exact Wave 71 verification/publication/archive identities. The default stable policy requires:

- `production-health-window`
- `incident-summary`

A requested `STABLE` state also requires observed `nicegui-base==3.0.0a8`, exact `nicegui==3.15.0`, and traceable external stability authority/reference metadata. Missing evidence remains **PENDING**. Hash, identity, or runtime-contract mismatches are **BLOCKED**.

The framework does not inspect incident semantics to invent a health conclusion. The authoritative external process supplies the requested state and artifacts; NiceGUI Base verifies traceability and integrity.

CLI:

```text
nicegui-base post-release-stability-intake post-promotion-verification.json post-promotion-verification.zip stability-manifest.json \
  --output post-release-stability.json --format json
```

## 3. Verify rollback readiness

`StableRollbackReadinessVerification` binds rollback-readiness evidence to the same Wave 71 package plus the exact Wave 72 stability identity. The stable rollback policy requires:

- `rollback-plan-validation`
- `rollback-artifact-integrity`
- `rollback-rehearsal`

Optional `rollback_execution_artifacts` and `rollback_execution_reference` may record an externally executed rollback. Their presence is evidence only: NiceGUI Base never executes, authorizes, or infers a rollback.

CLI:

```text
nicegui-base rollback-readiness-verify post-promotion-verification.json post-promotion-verification.zip \
  post-release-stability.json rollback-manifest.json \
  --output rollback-readiness.json --package rollback-readiness.zip --format json
```

A **PENDING** readiness dossier may be deterministically packaged for evidence-gap handoff. A **BLOCKED** dossier is refused.

## 4. Safety boundary

`STABLE` and `READY` are bounded evidence-verification states. They do not change Wave 66 promotion truth, Wave 67 candidate truth, Wave 70 documentary closure, Wave 71 `PUBLISHED`/`VERIFIED` status, or any target/runtime/browser/company/human gate. Continuous monitoring, incident response, deployment, publication and rollback remain external company operations.
