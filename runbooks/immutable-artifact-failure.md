# Missing or corrupt immutable artifact

Degraded user behavior: hide the affected evidence/download and mark the
artifact axis `ERROR` or `UNAVAILABLE`; retain the durable reference and do
not substitute an empty/private file.

Diagnostic evidence: catalog SHA-256/byte size, safe scope hash, verified
failure reason, and backup/restore manifest hash. Do not include artifact
contents or material labels.

Safe action: stop publication that references the failed identity, quarantine
the affected result, and recover exact bytes from an approved immutable backup
or object-store authority. A missing source artifact keeps O4 publication
blocked.

Verification: recompute exact SHA-256 and size, verify the content-addressed
path, then rerun the existing artifact publish precondition and any source/read
verification.

Rollback: roll back only the uncommitted publication or disable the affected
capability. Never rewrite an immutable catalog row or create fabricated bytes.
