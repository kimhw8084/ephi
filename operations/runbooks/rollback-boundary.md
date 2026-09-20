# O9.1 rollback boundary

Degraded user behavior: operators may lose the new status/backup/rehearsal
evidence surface, but existing O2/O3/O4 durable authorities and accepted
history remain intact.

Diagnostic evidence: candidate SHA/tree, carrier manifest, tool exit status,
and the last verified backup/restore report. Do not remove original evidence.

Safe action: stop invoking the O9 CLI, withdraw only unverified carrier
evidence, and keep the active database/artifact store on the existing
authorities. Disable any scheduled backup/reconciliation job created for this
change.

Verification: existing O2/O3/O4 focused tests and source preflight still report
their prior truthful states; no migration or durable row was deleted or
rewritten.

Rollback: revert the O9 tooling commit or remove only the isolated rehearsal
namespace and its temporary artifact directory after preserving evidence.
There is no traffic cutover or schema rollback in O9.1; never roll back by
deleting command/effect receipts or immutable artifacts.
