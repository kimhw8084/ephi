# Isolated backup/restore rehearsal

Degraded user behavior: the active source remains unchanged; rehearsal output
is evidence only and does not switch traffic or establish a production SLO.

Diagnostic evidence: backup manifest, dump SHA-256/size, migration identity,
server/tool versions, server-time cutoff/high-water, artifact inventory, restore
report, and post-cutoff reconciliation. Redact connection authority.

Safe action: create a new temporary PostgreSQL database and distinct artifact
directory, restore the logical dump and only catalog-referenced immutable
bytes, then run independent schema/state/artifact/read/workflow/worker checks.
Create a deterministic post-cutoff reconciliation report; do not run a generic
command replay engine.

Verification: required tables and fingerprints match, acknowledged receipts /
audit / outbox / O3 workflow and retained reads survive an application restart,
stale fencing/effect identities remain non-executable twice, and O4 manifests
restore as generic mechanics only.

Rollback: discard only the named isolated target after preserving evidence.
Never restore over the active database or artifact store. Record
`production_disaster_rpo_rto_claim = NOT_ESTABLISHED`.
