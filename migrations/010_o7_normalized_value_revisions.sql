-- CHG-205: normalized immutable value revisions. O2 remains the command,
-- aggregate CAS, receipt, audit and outbox authority; amounts live only here.
BEGIN;

CREATE TABLE IF NOT EXISTS outcome_value_revision (
    scope_key TEXT NOT NULL,
    group_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    category TEXT NOT NULL,
    amount NUMERIC,
    currency CHAR(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    event_at TIMESTAMPTZ NOT NULL,
    known_at TIMESTAMPTZ NOT NULL,
    supersedes TEXT,
    revision_kind TEXT NOT NULL CHECK (revision_kind IN ('VALUE', 'VOID')),
    claim_revision_id TEXT NOT NULL,
    evidence_identity TEXT,
    cost_model_identity TEXT,
    rate_policy_identity TEXT,
    maturity TEXT NOT NULL,
    CONSTRAINT outcome_value_amount_shape CHECK (
        (revision_kind = 'VALUE' AND amount IS NOT NULL)
        OR (revision_kind = 'VOID' AND amount IS NULL)
    ),
    CONSTRAINT outcome_value_revision_pkey PRIMARY KEY (scope_key, group_id, entry_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outcome_value_one_successor
    ON outcome_value_revision(scope_key, group_id, supersedes)
    WHERE supersedes IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_outcome_value_group_known
    ON outcome_value_revision(scope_key, group_id, known_at, entry_id);

-- Preserve value history produced by the predecessor candidate, then remove
-- its duplicate monetary representation from aggregate JSONB.
INSERT INTO outcome_value_revision(
    scope_key, group_id, entry_id, category, amount, currency, event_at,
    known_at, supersedes, revision_kind, claim_revision_id, evidence_identity,
    cost_model_identity, rate_policy_identity, maturity
)
SELECT
    aggregate.scope_key,
    aggregate.aggregate_id,
    revision.value ->> 'entry_id',
    revision.value ->> 'category',
    (revision.value ->> 'amount')::NUMERIC,
    revision.value ->> 'currency',
    (revision.value ->> 'event_at')::TIMESTAMPTZ,
    (revision.value ->> 'known_at')::TIMESTAMPTZ,
    revision.value ->> 'supersedes',
    COALESCE(revision.value ->> 'revision_kind', 'VALUE'),
    revision.value ->> 'claim_revision_id',
    revision.value ->> 'evidence_identity',
    revision.value ->> 'cost_model_identity',
    revision.value ->> 'rate_policy_identity',
    COALESCE(revision.value ->> 'maturity', 'OBSERVED')
FROM aggregate_state AS aggregate
CROSS JOIN LATERAL jsonb_array_elements(
    COALESCE(aggregate.state_json -> 'value_entries', '[]'::JSONB)
) AS revision(value)
WHERE aggregate.aggregate_type = 'outcome_claim_group'
ON CONFLICT (scope_key, group_id, entry_id) DO NOTHING;

UPDATE aggregate_state
SET state_json = state_json - 'value_entries'
WHERE aggregate_type = 'outcome_claim_group'
  AND state_json ? 'value_entries';

CREATE OR REPLACE FUNCTION reject_outcome_value_revision_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'outcome value revisions are append-only';
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'outcome_value_revision_immutable'
          AND tgrelid = 'outcome_value_revision'::regclass
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER outcome_value_revision_immutable
        BEFORE UPDATE OR DELETE ON outcome_value_revision
        FOR EACH ROW EXECUTE FUNCTION reject_outcome_value_revision_mutation();
    END IF;
END;
$$;

COMMIT;
