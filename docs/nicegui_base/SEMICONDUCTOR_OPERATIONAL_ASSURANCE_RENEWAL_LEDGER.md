# Semiconductor Operational Assurance Renewal Ledger

Use Wave 75 when multiple independently produced Wave 74 continuity packages must be reviewed as one longitudinal evidence history.

## Golden path

1. Preserve every exact Wave 74 continuity ZIP as immutable evidence.
2. Run `nicegui-base operational-assurance-ledger <wave74.zip> [<wave74.zip> ...]`.
3. Review gap, overlap, stale-window, duplicate, identity-drift, and tamper diagnostics.
4. Write the ledger JSON only after the exact package set is known.
5. Run `nicegui-base longitudinal-assurance <ledger.json> --package <handoff.zip>` to create a deterministic self-contained review package.

A missing or stale external period remains `PENDING`. Corrupt, unsafe, changed, identity-mismatched, or contradictory evidence is `BLOCKED`. Normal overlapping renewal windows are diagnostic and are not failures unless the configured policy sets an overlap limit.

Wave 75 does not monitor production or perform any incident, rollback, deployment, publication, or promotion action.
