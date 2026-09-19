# Semiconductor Longitudinal Assurance Review & Evidence Exception Governance

Use Wave 76 when an approved external reviewer must assess an exact Wave 75 longitudinal assurance package and record bounded evidence exceptions without changing the underlying evidence truth.

## Golden path

1. Preserve the exact immutable Wave 75 longitudinal assurance ZIP.
2. Create a review manifest with reviewer, authority, traceable reference, bounded authority validity, and hash-backed authority artifacts.
3. Run `nicegui-base longitudinal-assurance-review <dossier.json> <wave75.zip> <review-manifest.json>`.
4. Review `REVIEWED`, `PENDING`, or `BLOCKED` diagnostics. Expired/revoked/missing authority is `PENDING`; tamper, unsafe archive, identity drift, or contradictory timestamps are `BLOCKED`.
5. For Wave 75 `PENDING` findings, add only explicitly bounded exception decisions allowed by the configured policy. An accepted exception closes the review obligation only; it does not turn Wave 75 evidence into `ASSURED`.
6. Run `nicegui-base evidence-exception-governance <review.json> --package <handoff.zip>` to create a deterministic self-contained handoff.

The default exception policy is a framework governance default, not a company SLA or waiver authority. Replace it only with an approved bounded policy supplied by the destination organization.
