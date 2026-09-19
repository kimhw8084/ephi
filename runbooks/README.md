# O9.1 runbook inventory

Every runbook below identifies degraded behavior, diagnostic evidence, a safe
action, verification, and rollback. The operator must preserve authoritative
state and must not fabricate healthy/zero values when PostgreSQL, source, or
immutable storage is unavailable.

| Runbook | Scope |
|---|---|
| [PostgreSQL outage](postgresql-outage.md) | database unavailable or schema not ready |
| [Immutable artifact failure](immutable-artifact-failure.md) | missing/corrupt content-addressed bytes |
| [Source outage or staleness](source-outage-staleness.md) | O4 `BLOCKED_REAL_SOURCE` / stale capability |
| [Worker crash or stale lease](worker-crash-stale-lease.md) | O2 lease/fencing/effect safety |
| [Projection/read inconsistency](projection-read-repair.md) | O2/O3 retained reads and projection repair |
| [Isolated restore rehearsal](isolated-restore-rehearsal.md) | backup identity, restore, and reconciliation |
| [O9.1 rollback boundary](rollback-boundary.md) | disabling tooling/evidence publication safely |

Qualification summary: generic health, backup identity, artifact verification,
isolated restore, and reconciliation are the O9.1 scope. Authentic-family
qualification remains `BLOCKED_REAL_SOURCE`; production disaster RPO/RTO is
`NOT_ESTABLISHED`.
