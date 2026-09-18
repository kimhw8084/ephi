-- CHG-123: narrow PostgreSQL reference schema for the O2 command core.
-- This migration is intentionally idempotent and contains no product/O3 tables.

CREATE TABLE IF NOT EXISTS aggregate_state (
    scope_key TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    version BIGINT NOT NULL CONSTRAINT aggregate_state_version_nonnegative CHECK (version >= 0),
    state_json JSONB NOT NULL,
    CONSTRAINT aggregate_state_pkey PRIMARY KEY (scope_key, aggregate_type, aggregate_id)
);

CREATE TABLE IF NOT EXISTS command_receipt (
    scope_key TEXT NOT NULL,
    subject TEXT NOT NULL,
    command_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    result_identity TEXT NOT NULL,
    result_json JSONB NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version BIGINT NOT NULL CONSTRAINT command_receipt_version_nonnegative CHECK (aggregate_version >= 0),
    auth_session_revision_json JSONB NOT NULL,
    security_revision_json JSONB NOT NULL,
    committed_at TEXT NOT NULL,
    CONSTRAINT command_receipt_pkey PRIMARY KEY (scope_key, subject, command_id)
);

CREATE TABLE IF NOT EXISTS audit_event (
    event_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    subject TEXT NOT NULL,
    command_id TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version BIGINT NOT NULL CONSTRAINT audit_event_version_nonnegative CHECK (aggregate_version >= 0),
    event_type TEXT NOT NULL,
    event_json JSONB NOT NULL,
    auth_session_revision_json JSONB NOT NULL,
    security_revision_json JSONB NOT NULL,
    recorded_at TEXT NOT NULL,
    CONSTRAINT audit_event_pkey PRIMARY KEY (event_id),
    CONSTRAINT audit_event_command_unique UNIQUE (scope_key, subject, command_id)
);

CREATE TABLE IF NOT EXISTS outbox_event (
    event_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    subject TEXT NOT NULL,
    command_id TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version BIGINT NOT NULL CONSTRAINT outbox_event_version_nonnegative CHECK (aggregate_version >= 0),
    event_type TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT outbox_event_pkey PRIMARY KEY (event_id),
    CONSTRAINT outbox_event_command_unique UNIQUE (scope_key, subject, command_id)
);

CREATE INDEX IF NOT EXISTS idx_receipt_scope_subject ON command_receipt(scope_key, subject);
CREATE INDEX IF NOT EXISTS idx_audit_scope_aggregate ON audit_event(scope_key, aggregate_type, aggregate_id, aggregate_version);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_event(status, created_at);
