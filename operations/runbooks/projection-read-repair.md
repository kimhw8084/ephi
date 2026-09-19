# Projection/read inconsistency repair

Degraded user behavior: show the last coherent retained read or an explicit
unavailable-history state; pause affected write actions and do not splice live
workflow into a historical revision.

Diagnostic evidence: current head, immutable read revision, workflow version,
retained snapshot/cursor identity, source manifest, outbox event, and projection
row-version fingerprints.

Safe action: stop the competing projection writer, reconcile the existing
outbox/command receipt, and rebuild the bounded projection from its immutable
durable revision using the existing O2/O3 authorities. Expired retained reads
must return the existing expiry state and be intentionally refreshed.

Verification: current and historical bundles are coherent, row order/version
is stable, workflow revision vectors agree, and no accepted command or effect
was duplicated.

Rollback: keep the last trustworthy head/read revision visible read-only and
disable the repair worker. Never overwrite immutable read history or invent a
projection row.
