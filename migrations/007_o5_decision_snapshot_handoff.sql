-- CHG-169 / O5.2: immutable decision snapshots and durable handoff delivery.
-- The tables are additive.  Episode workflow, O2 outbox, O2 jobs and O2
-- applied effects remain the authorities for their respective concerns.

CREATE TABLE IF NOT EXISTS decision_snapshot (
    snapshot_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    content_hash TEXT NOT NULL CONSTRAINT decision_snapshot_content_hash_valid CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    episode_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    workflow_version BIGINT NOT NULL CONSTRAINT decision_snapshot_workflow_version_nonnegative CHECK (workflow_version >= 0),
    viewed_revisions_json JSONB NOT NULL,
    content_json JSONB NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT decision_snapshot_pkey PRIMARY KEY (snapshot_id),
    CONSTRAINT decision_snapshot_scope_identity_unique UNIQUE (scope_key, snapshot_id),
    CONSTRAINT decision_snapshot_content_object CHECK (jsonb_typeof(content_json) = 'object'),
    CONSTRAINT decision_snapshot_revisions_object CHECK (jsonb_typeof(viewed_revisions_json) = 'object')
);

CREATE TABLE IF NOT EXISTS handoff_intent (
    intent_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    triggering_event_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    decision_snapshot_id TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    material_change_signature TEXT NOT NULL,
    recipient_selector TEXT NOT NULL,
    resolved_recipient TEXT NOT NULL,
    channel TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    deep_link_json JSONB NOT NULL,
    safe_payload_json JSONB NOT NULL,
    job_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT handoff_intent_pkey PRIMARY KEY (intent_id),
    CONSTRAINT handoff_intent_dedup_unique UNIQUE (scope_key, dedup_key),
    CONSTRAINT handoff_intent_deep_link_object CHECK (jsonb_typeof(deep_link_json) = 'object'),
    CONSTRAINT handoff_intent_safe_payload_object CHECK (jsonb_typeof(safe_payload_json) = 'object')
);

CREATE TABLE IF NOT EXISTS handoff_delivery_status (
    intent_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    job_id TEXT,
    delivery_state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CONSTRAINT handoff_delivery_attempt_nonnegative CHECK (attempt_count >= 0),
    last_failure_code TEXT,
    last_failure_message TEXT,
    last_failure_at TIMESTAMPTZ,
    ambiguity_warning TEXT,
    external_reference TEXT,
    idempotency_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT handoff_delivery_status_pkey PRIMARY KEY (intent_id),
    CONSTRAINT handoff_delivery_status_state_valid CHECK (delivery_state IN ('PENDING', 'DISPATCHING', 'DELIVERED', 'FAILED', 'UNKNOWN', 'CANCELED', 'SUPERSEDED')),
    CONSTRAINT handoff_delivery_status_idempotency_unique UNIQUE (scope_key, idempotency_key)
);

CREATE TABLE IF NOT EXISTS handoff_delivery_attempt (
    intent_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    attempt_no INTEGER NOT NULL CONSTRAINT handoff_attempt_positive CHECK (attempt_no > 0),
    delivery_state TEXT NOT NULL,
    error_code TEXT,
    external_reference TEXT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT handoff_delivery_attempt_pkey PRIMARY KEY (intent_id, attempt_no),
    CONSTRAINT handoff_delivery_attempt_intent_fkey FOREIGN KEY (intent_id) REFERENCES handoff_delivery_status(intent_id),
    CONSTRAINT handoff_delivery_attempt_state_valid CHECK (delivery_state IN ('PENDING', 'DISPATCHING', 'DELIVERED', 'FAILED', 'UNKNOWN', 'CANCELED', 'SUPERSEDED'))
);

CREATE INDEX IF NOT EXISTS idx_decision_snapshot_episode ON decision_snapshot(scope_key, episode_id, workflow_version, created_at);
CREATE INDEX IF NOT EXISTS idx_handoff_intent_event ON handoff_intent(scope_key, triggering_event_id);
CREATE INDEX IF NOT EXISTS idx_handoff_delivery_state ON handoff_delivery_status(scope_key, delivery_state, updated_at);

ALTER TABLE handoff_intent DROP CONSTRAINT IF EXISTS handoff_intent_event_snapshot_unique;

CREATE OR REPLACE FUNCTION ephi_o5_decision_snapshot_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'decision_snapshot rows are immutable' USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS decision_snapshot_immutable_trigger ON decision_snapshot;
CREATE TRIGGER decision_snapshot_immutable_trigger
BEFORE UPDATE OR DELETE ON decision_snapshot
FOR EACH ROW EXECUTE FUNCTION ephi_o5_decision_snapshot_immutable();
