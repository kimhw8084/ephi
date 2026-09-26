# O9.1 operational tooling

CHG-147 / O9.1 adds a narrow, CLI-only operations boundary. It reads the
existing PostgreSQL command/receipt/audit/outbox, worker lease/effect, O3
read/workflow, artifact catalog, and O4 source manifest authorities. It does
not create a second operational state store, copy raw telemetry, switch
traffic, mutate Notion, or qualify an authentic source family.

## Commands

```bash
python3 tools/o9_operations.py status --json
python3 tools/o9_operations.py backup-create \
  --dsn "$EPHI_POSTGRES_DSN" --artifact-root /absolute/artifacts \
  --output-dir /absolute/backup
python3 tools/o9_operations.py backup-verify \
  --manifest /absolute/backup/backup_manifest.json \
  --pg-restore-command 'pg_restore'
python3 tools/o9_operations.py reconcile \
  --manifest /absolute/backup/backup_manifest.json --dsn "$EPHI_POSTGRES_DSN"
```

`pg_dump` and `pg_restore` are discovered from `PATH`. An explicit command
prefix can be supplied for a container or platform-managed PostgreSQL 18
toolchain. Commands and JSON output never print DSNs, credentials, tokens,
raw source rows, material identifiers, or private artifact contents.

The backup manifest records repository/source SHA and tree, migration identity,
safe PostgreSQL identity, a server-time transaction cutoff/high-water, dump
SHA-256/size, critical durable-state fingerprints, and the immutable artifact
inventory. A restore rehearsal always creates a new database and artifact
directory; it never changes traffic routing. Reconciliation reports writes
accepted after the cutoff and classifies local idempotent candidates separately
from consequential/external handling.

Release/install inventory uses the same shared migration file-set helper as
O9 backup verification. It preserves the existing ordered file records,
per-file SHA-256/byte-size facts and aggregate canonical SHA-256; there is no
second migration identity algorithm or migration history store.

All timing evidence is `LOCAL_RESTORE_REHEARSAL`. The production disaster
claim is always `NOT_ESTABLISHED`; this change does not establish RPO/RTO.

See [runbooks](runbooks/README.md) for operator actions and rollback limits.
