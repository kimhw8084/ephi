-- CHG-205: one durable O2 aggregate per scoped economic event.
-- Claim, value and review revisions remain in the versioned aggregate state;
-- O2 continues to own CAS, receipts, audit and outbox publication.

CREATE UNIQUE INDEX IF NOT EXISTS idx_outcome_economic_event
    ON aggregate_state(scope_key, (state_json ->> 'economic_event_key'))
    WHERE aggregate_type = 'outcome_claim_group';
