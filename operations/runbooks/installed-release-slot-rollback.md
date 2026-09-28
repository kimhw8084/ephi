# Same-release installed release-slot rollback (CHG-295 / U3.7)

## Scope

This runbook returns the local EPHI release-selection record to its previously
selected, separately installed slot after rechecking that slot with
`ephi-release-preflight`. It is valid only when the previous and current slots
have the same exact `release_identity_sha256`. The command changes local
selection metadata only. It does not configure or switch real application
traffic.

## Degraded behavior

If the selected installed environment fails its release preflight or its
read-only O9 status is unavailable, the operator can use the recorded previous
slot only when it passes installed release preflight and has the same exact
release identity. A missing/tampered slot, stale generation, or different
release identity fails closed and leaves the prior valid selection file
unchanged.

## Diagnostic evidence

Record the fixed `ephi-release-slot` reason code, generation, current/previous
slot IDs, transition identity, and release identity digest. Keep the installed
release-preflight JSON and read-only O9 status result with the job evidence.
These records contain no slot path, DSN, credential, provider setting, raw
inventory, or private source fact.

## Safe action

Read the current selection and use its exact generation:

```bash
ephi-release-slot read --state-file /var/lib/ephi/release-selection.json
ephi-release-slot rollback \
  --state-file /var/lib/ephi/release-selection.json \
  --expected-generation 2 \
  --slot-root /opt/ephi/slot-a \
  --inputs-dir /approved/ephi-install-inputs
```

The `--slot-root` and `--inputs-dir` values must resolve to the recorded
previous slot and approved immutable input bundle. The operator must not reuse
a stale generation. If rollback succeeds, the current and previous slot IDs
swap and the generation increments once. If it fails, inspect the fixed
reason code and the read command; do not edit the state file manually.

Afterward, run the selected slot's installed read-only release preflight and
`ephi-operations status --json`. Do not run `ephi-db-migrate` as part of slot
selection or rollback. The installed O9 status check is read-only.

## Durable-state boundary

PostgreSQL and immutable artifact storage are shared external authorities.
Slot selection and rollback must not reverse or reapply migrations, restore
an old database, delete or rewrite accepted commands, receipts, audit or
outbox events, evidence, artifacts, source snapshots, value revisions,
workflow history, or provider/company state. Preserve the existing durable
history.

## Distinct recovery and compatibility boundaries

- O9 backup verification and isolated restore rehearsal are separate recovery
  operations. They do not select an application slot or switch traffic.
- Schema downgrade is unsupported. Use the documented forward-correction and
  migration policy; this runbook has no database rollback command.
- Scientific/model rollback is outside U3.7 and must publish a new versioned
  result under its own approved authority; it must not rewrite history.
- Cross-release switching, N-1-to-N upgrade/rollback, and cross-release
  provider compatibility remain `NOT_QUALIFIED` until U4 proves and binds
  them. A different release identity returns
  `CROSS_RELEASE_COMPATIBILITY_NOT_QUALIFIED`.
- Release promotion, G12, Port Gate, target cutover, and Production are not
  supported by this runbook.

## Rollback of this tooling

Stop using `ephi-release-slot` and retain its last valid local selection record
for diagnostics. Removing or reverting the package tooling does not authorize
deleting PostgreSQL rows, immutable artifacts, or any accepted Product
history. There is no traffic or schema rollback in this change.
