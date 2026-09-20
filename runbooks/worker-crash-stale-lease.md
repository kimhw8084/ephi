# Worker crash, lease loss, or stale worker

Degraded user behavior: queued work may be delayed; no stale worker may
publish a head or local effect. Report job state separately from process
liveness.

Diagnostic evidence: job status/attempt, owner, lease epoch/expiry, failure
code, applied-effect identity, and the relevant command/read revision hashes.
Do not infer execution from a log line alone.

Safe action: allow the existing O2 claim/takeover path to fence the old lease;
heartbeat only with the current lease. Reconcile an existing applied-effect
receipt before retrying a local effect. Treat external effects as requiring
explicit reconciliation.

Verification: the new epoch is authoritative, stale heartbeat/complete/effect
operations are rejected, and a repeated effect key returns the existing
receipt without a second mutation.

Rollback: defer or cancel the named job through the existing worker port. Do
not reset epochs, delete effect receipts, or make a stale worker executable.
