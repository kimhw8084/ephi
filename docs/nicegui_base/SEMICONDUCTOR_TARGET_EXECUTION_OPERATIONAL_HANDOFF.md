# Semiconductor target execution intake and operational handoff

Use this workflow when real target-environment artifacts become available after a recipe already has canonical NiceGUI Base target evidence.

## 1. Capture target execution artifacts

Run the approved target-environment checks outside the generic framework boundary and retain their actual outputs. Create a provider-neutral intake manifest that names only the external target gates you actually executed. Do not mark a missing run PASS.

## 2. Assimilate into canonical evidence

```bash
nicegui-base target-intake evidence.json target-execution.json \
  --intake-output normalized-intake.json \
  --output merged-evidence.json
```

`target-intake` can only change `installed_nicegui`, `server_websocket`, `supported_browser`, and `human_visual_baseline`. A PASS without traceable current bytes and matching framework/runtime identity remains PENDING. Provider conformance/performance/benchmark status is not rewritten by this command.

## 3. Rebuild the Wave 67 candidate

Use the merged evidence with the existing qualification, operational-readiness, and rehearsal contracts. Stable-channel readiness still requires the canonical Wave 66 promotion decision plus all required Wave 67 rehearsals.

## 4. Prepare an operational handoff

```bash
nicegui-base promotion-handoff candidate.json candidate.zip \
  --change-reference CHG-12345 \
  --output handoff.json \
  --package handoff.zip
```

The command reopens and verifies the candidate ZIP from bytes. `READY` means the evidence package is safe to hand to an approved company deployment/change process; it does not mean deployment happened or was approved.

## 5. Capture operational execution evidence

After an approved company process executes a promotion/rollback/incident/evidence-capture action, bind the resulting artifacts to canonical runbook evidence keys:

```bash
nicegui-base promotion-operation handoff.json spc-monitor promotion \
  --completed startup \
  --completed release-evidence \
  --completed rollback \
  --evidence startup=setup-workflow=setup.json \
  --evidence startup=runtime-probe=runtime.json \
  --evidence release-evidence=target-evidence-bundle=evidence.json \
  --evidence release-evidence=artifact-hashes=hashes.txt \
  --evidence rollback=runtime-preset=preset.json \
  --evidence rollback=configuration-review=config-review.json \
  --output promotion-operation.json
```

A record is PASS only when all required Wave 67 steps and all runbook evidence items are present. Verification hashes every captured file again. Operation records do not change target gates or candidate status.

## Adapter boundary

The built-in adapters are intentionally file/provider neutral:

- target execution: `json-manifest`
- operational handoff: `file-package`

Keep database, auth, proxy, deployment, ticketing, and release-orchestrator vendor integrations outside the core. Add company-specific adapters only from authoritative company contracts.
