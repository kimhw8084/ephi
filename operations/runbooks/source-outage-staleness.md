# Source outage, staleness, or `BLOCKED_REAL_SOURCE`

Degraded user behavior: preserve existing durable workflow/read state, mark
source capability `UNAVAILABLE` or `STALE`, and disable source-dependent
scientific decisions. Process and PostgreSQL axes may remain ready.

Diagnostic evidence: source-reality preflight, capability state/reason,
snapshot available cutoff, mapping identity, and last verified manifest hash.
Never fill missing source rows with zeros or healthy defaults.

Safe action: contact the approved source owner, check the bounded adapter and
freshness window, and publish only a validated immutable snapshot through the
existing O4 ingress when the source is genuinely available.

Verification: adapter binding, mapping hash, units, required identifiers,
artifact bytes, temporal facts, and PostgreSQL source manifest/capability state
all agree.

Rollback: leave the capability unavailable/stale and retain prior durable
history. Do not delete source history or claim authentic-family qualification.
