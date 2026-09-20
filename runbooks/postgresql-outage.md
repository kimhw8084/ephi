# PostgreSQL outage/unavailable database

Degraded user behavior: show process/transport liveness only; disable durable
writes and decision-sensitive reads. Do not fall back to memory, SQLite, or
healthy/zero source data.

Diagnostic evidence: `status --json`, PostgreSQL connectivity/readiness logs,
safe connection facts, and the last verified backup manifest. Do not collect a
DSN or raw row dump in the incident record.

Safe action: page the database owner, preserve the source database, stop
retry storms, and restore only to a newly created isolated target if recovery
is authorized. Reconcile receipts and post-cutoff identities before any traffic
change.

Verification: the required schema is present, the restored critical table
fingerprints and artifact inventory verify, and a fresh application/read
restart can read durable state.

Rollback: keep traffic on the active source unless an approved cutover exists;
delete only an explicitly named temporary rehearsal target after evidence is
preserved. Never overwrite the active database.
