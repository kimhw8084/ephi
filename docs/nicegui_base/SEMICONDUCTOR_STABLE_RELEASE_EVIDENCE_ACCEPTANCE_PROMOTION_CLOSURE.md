# Semiconductor stable release evidence acceptance and promotion closure

Wave 70 is a bounded documentary layer over the existing Wave 59–69 authorities. It does not create a second target-certification or promotion engine and it does not deploy or publish NiceGUI Base.

## 1. Verify the immutable Wave 69 audit package

`verify_release_audit_archive()` independently checks the self-contained release-audit ZIP: archive safety, duplicate entries, `MANIFEST.sha256`, the persisted `ReleaseAuditClosure`, the embedded stable-candidate ZIP, execution-adapter qualification artifacts, and operation evidence hashes.

A changed, corrupt, unsafe, incomplete, or wrong-subject archive is **BLOCKED**. The framework never repairs external evidence in place.

## 2. Import external release-evidence acceptance

Use an external JSON manifest only when the company release authority has produced authoritative evidence:

```json
{
  "schema_version": 1,
  "status": "accepted",
  "framework_version": "3.0.0a8",
  "nicegui_version": "3.15.0",
  "acceptance_authority": "approved-company-release-process",
  "approval_reference": "<external reference>",
  "artifacts": [
    {"key": "release-approval", "path": "approval.json", "sha256": "<optional expected sha256>"}
  ]
}
```

Run:

```bash
nicegui-base release-evidence-accept release-audit.json release-audit.zip acceptance-manifest.json \
  --output stable-release-acceptance.json --format json
```

Requested `ACCEPTED` stays **PENDING** when artifact bytes, observed framework/NiceGUI identity, authority, or approval reference are missing. Identity mismatch, changed bytes, subject mismatch, or expected-hash mismatch is **BLOCKED**. Authority/reference strings are retained as metadata; NiceGUI Base does not interpret them as approval by themselves.

## 3. Build final documentary closure

```bash
nicegui-base promotion-close release-audit.json release-audit.zip stable-release-acceptance.json \
  --output stable-promotion-closure.json --package stable-promotion-closure.zip --format json
```

Default stable closure requires all of the following:

- the exact Wave 69 audit ZIP independently verifies;
- the canonical Wave 69 release audit is `CLOSED`;
- the canonical Wave 67 candidate is `READY` for target `3.0.0`;
- the canonical Wave 66 promotion decision is `PROMOTABLE`;
- external release evidence is artifact-backed `ACCEPTED`.

Any canonical BLOCKED state stays **BLOCKED**. Missing acceptance or canonical readiness stays **PENDING**. Wave 70 never overrides an earlier authority.

## 4. Meaning of `CLOSED`

Wave 70 `CLOSED` means only that the immutable canonical evidence chain and external evidence-acceptance record are complete and verify together. It explicitly does **not** mean:

- NiceGUI Base deployed anything;
- NiceGUI Base published stable `3.0.0`;
- candidate or target-gate status was changed;
- an external authority/reference string was interpreted as approval;
- a missing company/runtime/browser/human artifact was synthesized.

The closure ZIP is deterministic and self-contained for retention/audit handoff. PENDING closures may be packaged to communicate gaps; BLOCKED closures are refused.
