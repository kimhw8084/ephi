# Contributing

Keep product decisions in the numbered design chapters and record material changes in [the package review](13_Package_Review.md). Keep application findings F01–F14 separate from package-review findings P01–P10. Requirement, invariant and gate IDs must remain stable.

Do not overwrite historical audit files. Add new executions under a separate evidence directory with the exact source identity, command, environment, result and limitations. A successful package check is not an application, browser, scientific or production test.

After intentional changes:

```bash
python3 tools/check_package.py --refresh-manifest
python3 -m unittest discover -s tests -v
git diff --check
git diff --stat
```

Review the manifest diff before committing. Refreshing hashes records changed bytes; it does not certify their correctness or provenance. The checker preserves the original evidence hashes recorded in [the imported manifest](evidence/import/original_manifest.json). New local reports belong in the ignored `artifacts/` directory unless deliberately prepared as versioned evidence.

Use relative links for repository documents and pinned references for framework claims. Proposed interface snippets are contracts, while `src/ephi` is the new canonical implementation boundary. Keep historical observations separate from newly executed canonical checks. Run application checks in addition to this repository's package checks.

Never commit credentials, company production data, local environments or unreviewed archives. Changes to manufacturing authority, scientific policy, retention or company scope require their stated design gates.
