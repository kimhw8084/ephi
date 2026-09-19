# Semiconductor release publication evidence intake and post-promotion verification

Wave 71 is a bounded verification layer over the existing Wave 59–70 authorities. It does not deploy, publish, rollback, approve, or continuously monitor NiceGUI Base.

## 1. Verify the exact Wave 70 closure archive

`verify_stable_promotion_closure_archive()` independently checks the self-contained Wave 70 closure ZIP: safe unique paths, `MANIFEST.sha256`, persisted closure identity, embedded acceptance identity, embedded Wave 69 audit archive SHA-256, and independent re-verification of that nested audit archive.

Changed, corrupt, incomplete, unsafe, duplicate-entry, or wrong-subject closure archives are **BLOCKED**.

## 2. Import authoritative stable publication evidence

Use an external manifest only after the approved company publication process produces real evidence:

```json
{
  "schema_version": 1,
  "status": "published",
  "framework_version": "3.0.0a8",
  "nicegui_version": "3.15.0",
  "publication_authority": "approved-company-release-service",
  "publication_reference": "<immutable external reference>",
  "deployment_reference": "<optional external deployment reference>",
  "artifacts": [
    {"key": "stable-publication-record", "path": "publication-receipt.json"}
  ]
}
```

Run:

```bash
nicegui-base release-publication-intake stable-promotion-closure.json stable-promotion-closure.zip publication-manifest.json \
  --output stable-release-publication.json --format json
```

Requested `PUBLISHED` remains **PENDING** unless current immutable artifact bytes, exact observed framework/NiceGUI identity, publication authority, and publication reference are present. Changed artifacts, closure/archive drift, wrong subject, or identity mismatch are **BLOCKED**. External authority/reference strings remain metadata only.

## 3. Verify post-promotion evidence

The default stable policy requires two generic evidence classes from the actual published target:

- `publication-integrity`
- `post-release-runtime-smoke`

A company may capture richer vendor-specific evidence behind its own process without changing the framework authority boundary.

```json
{
  "schema_version": 1,
  "status": "verified",
  "framework_version": "3.0.0a8",
  "nicegui_version": "3.15.0",
  "verification_authority": "approved-post-release-process",
  "verification_reference": "<immutable verification reference>",
  "artifacts": [
    {"key": "publication-integrity", "path": "publication-integrity.json"},
    {"key": "post-release-runtime-smoke", "path": "runtime-smoke.json"}
  ]
}
```

Run:

```bash
nicegui-base post-promotion-verify stable-promotion-closure.json stable-promotion-closure.zip stable-release-publication.json post-release-manifest.json \
  --output post-promotion-verification.json --package post-promotion-verification.zip --format json
```

Requested `VERIFIED` stays **PENDING** when required post-release artifacts, exact observed identity, or external verification authority/reference are missing. Corrupt/tampered evidence is **BLOCKED**.

## 4. Meaning of `PUBLISHED` and `VERIFIED`

`PUBLISHED` means authoritative external publication evidence verifies against the exact Wave 70 closure. `VERIFIED` means the bounded post-release evidence dossier verifies against that publication identity. Neither state means NiceGUI Base performed deployment/publication, changed Wave 66 promotion truth, changed target gates, approved a company release, or performs ongoing production monitoring.

PENDING post-promotion dossiers may be packaged for gap handoff. BLOCKED dossiers are refused.
