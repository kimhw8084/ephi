# O9.1 operational tooling

CHG-147 / O9.1 adds a narrow, CLI-only operations boundary. It reads the
existing PostgreSQL command/receipt/audit/outbox, worker lease/effect, O3
read/workflow, artifact catalog, and O4 source manifest authorities. It does
not create a second operational state store, copy raw telemetry, switch
traffic, mutate Notion, or qualify an authentic source family.

## Installed commands

```bash
export EPHI_POSTGRES_DSN='<injected PostgreSQL connection settings>'
.venv/bin/ephi-operations status --json
.venv/bin/ephi-operations backup-create \
  --artifact-root /absolute/artifacts --output-dir /absolute/backup
.venv/bin/ephi-operations backup-verify \
  --manifest /absolute/backup/backup_manifest.json
EPHI_POSTGRES_ADMIN_DSN="$EPHI_POSTGRES_DSN" \
  .venv/bin/ephi-operations restore-rehearsal \
    --manifest /absolute/backup/backup_manifest.json \
    --target-database ephi_restore_20260927 \
    --target-artifact-root /absolute/restore-artifacts
.venv/bin/ephi-operations reconcile \
  --manifest /absolute/backup/backup_manifest.json
```

`pg_dump` and `pg_restore` are discovered from `PATH`. An explicit command
name can be supplied for a platform-managed PostgreSQL 18 toolchain; it must
be a name found in `PATH`. PostgreSQL connection settings are read from the
process environment and are never accepted as command arguments. Native tool
arguments contain no DSN, credential, host, user, database name, or physical
dump path. Reports and manifests contain no raw connection identifiers,
private source/material identifiers, raw rows, or artifact contents.

`backup-create`, `backup-verify`, `restore-rehearsal`, and `reconcile` are
implemented by the installed `ephi.o9_operations` authority. The source
checkout command `python3 tools/o9_operations.py` is a compatibility wrapper
that delegates to the same package functions. Installed execution does not
inspect Git or repository-local paths.

The installed EPHI release also provides `ephi-db-migrate` for its numbered
PostgreSQL schema. Run `identity` to inspect the packaged plan without a
database connection, `verify` to check all declared current-schema facts
without applying SQL, and `apply` to apply the idempotent set and verify those
same requirements. The installed wheel also provides the same read-only O9
six-axis status through `ephi-operations`; it requires no repository checkout:

```bash
.venv/bin/ephi-db-migrate apply
.venv/bin/ephi-operations status --json
```

Status does not apply migrations or compose downstream providers. A reachable
database is READY only when the shared current-schema validator passes,
including `source_snapshot.freshness_age_seconds`. Missing DSN, artifact-root,
source binding, and qualification authority remain explicit unavailable or
unqualified axes. The checkout wrapper `python3 tools/o9_operations.py status
--json` delegates to this package implementation. Backup, restore, and
reconciliation use the installed package authority as well.

Current schema requirements include the required tables and
`source_snapshot.freshness_age_seconds`:

```bash
.venv/bin/ephi-db-migrate identity
.venv/bin/ephi-db-migrate apply
.venv/bin/ephi-db-migrate verify
```

`verify` and `apply` read `EPHI_POSTGRES_DSN` or accept `--dsn`. Run `apply`
after release/configuration preflight and before provider composition or
normal application startup. Results use fixed status and reason codes and do
not disclose the DSN. The tool does not maintain migration history.

The `o9.1.backup.v2` manifest binds the packaged release identity and shared
installed migration identity, a hashed PostgreSQL database/schema identity, a
server-time transaction cutoff/high-water, dump SHA-256/size, critical
durable-state fingerprints, and the immutable artifact inventory. It does
not invent Git SHA/tree facts when running from a wheel. Verification and
restore reject `o9.1.backup.v1` manifests because they do not bind the
packaged release identity; reconciliation accepts those manifests for cutoff
comparison only and labels the identity `LEGACY_RELEASE_IDENTITY_UNBOUND`.
A restore rehearsal creates a new database and a new artifact directory; it
never changes traffic routing. Reconciliation reports writes accepted after
the cutoff and classifies local idempotent candidates separately from
consequential/external handling. Consequential actions are never replayed by
the recovery command.

Release/install inventory uses the same shared migration file-set helper as
O9 backup verification. It preserves the existing ordered file records,
per-file SHA-256/byte-size facts and aggregate canonical SHA-256; there is no
second migration identity algorithm or migration history store.

All timing evidence is `LOCAL_RESTORE_REHEARSAL`. The production disaster
claim is always `NOT_ESTABLISHED`; this change does not establish RPO/RTO.

See [runbooks](runbooks/README.md) for operator actions and rollback limits.
