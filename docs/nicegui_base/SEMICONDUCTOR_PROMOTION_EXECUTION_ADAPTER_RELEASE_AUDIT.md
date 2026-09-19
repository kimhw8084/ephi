# Semiconductor promotion execution-adapter qualification and release audit

Use this workflow after Wave 68 has produced a verified operational handoff and an approved company release process can emit real execution-adapter qualification artifacts.

## 1. Qualify the external execution adapter

Create a provider-neutral manifest referencing the actual qualification artifacts:

```json
{
  "schema_version": 1,
  "adapter_key": "company-release-adapter",
  "adapter_version": "1.0.0",
  "status": "qualified",
  "supported_operations": ["promotion", "rollback", "incident", "evidence-capture"],
  "framework_version": "3.0.0a8",
  "nicegui_version": "3.15.0",
  "qualification_authority": "approved-company-process",
  "approval_reference": "optional-reference-only",
  "artifacts": [
    {"key": "qualification-run", "path": "qualification-result.json"}
  ]
}
```

Then normalize and verify it:

```bash
nicegui-base execution-adapter-qualify adapter-qualification.json \
  --artifact-base-dir ./qualification-artifacts \
  --output normalized-adapter-qualification.json
```

A requested QUALIFIED result without actual current bytes or exact release identity remains PENDING. Wrong identity or changed bytes are BLOCKED. An approval reference is metadata only.

## 2. Capture promotion operation evidence

Use the existing Wave 68 workflow after the approved external company process executes:

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

## 3. Close the release audit

```bash
nicegui-base release-audit handoff.json normalized-adapter-qualification.json promotion-operation.json \
  --output release-audit.json \
  --package release-audit.zip
```

The stable audit closes only when the handoff still verifies, the external execution adapter is artifact-backed QUALIFIED, and every candidate recipe has a complete PASS promotion operation record. Supplied optional rollback/incident/evidence-capture records are also verified.

`CLOSED` is an audit-completeness state. It is not a new promotion decision, does not mutate target gates or the stable candidate, and does not imply that NiceGUI Base itself deployed anything.

## Adapter boundary

The core provides only a provider-neutral JSON qualification-manifest adapter. Keep database, auth, ticketing, proxy, deployment, release-orchestrator, and secrets/vendor details behind approved company adapters/configuration. Do not add such details to framework core without an authoritative company contract.
