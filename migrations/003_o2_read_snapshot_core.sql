-- CHG-129: generic PostgreSQL coherent-read and retained-query substrate.
-- This migration contains no Attention, Episode, product query, UI, identity,
-- notification, or company-binding tables.

CREATE TABLE IF NOT EXISTS read_revision (
    revision_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    revision_vector_json JSONB NOT NULL,
    payload_json JSONB NOT NULL,
    known_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    workflow_aggregate_type TEXT NOT NULL,
    workflow_aggregate_id TEXT NOT NULL,
    workflow_version BIGINT NOT NULL CONSTRAINT read_revision_workflow_version_nonnegative CHECK (workflow_version >= 0),
    workflow_state_json JSONB NOT NULL,
    CONSTRAINT read_revision_pkey PRIMARY KEY (revision_id),
    CONSTRAINT read_revision_entity_identity_unique UNIQUE (scope_key, entity_type, entity_id, revision_id),
    CONSTRAINT read_revision_vector_object CHECK (jsonb_typeof(revision_vector_json) = 'object'),
    CONSTRAINT read_revision_payload_object CHECK (jsonb_typeof(payload_json) = 'object'),
    CONSTRAINT read_revision_workflow_state_object CHECK (jsonb_typeof(workflow_state_json) = 'object')
);

CREATE TABLE IF NOT EXISTS read_head (
    scope_key TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    head_version BIGINT NOT NULL CONSTRAINT read_head_version_positive CHECK (head_version > 0),
    published_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT read_head_pkey PRIMARY KEY (scope_key, entity_type, entity_id),
    CONSTRAINT read_head_revision_fkey FOREIGN KEY (revision_id) REFERENCES read_revision(revision_id)
);

CREATE TABLE IF NOT EXISTS query_snapshot (
    snapshot_id TEXT NOT NULL,
    query_identity_hash TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    subject TEXT NOT NULL,
    security_revision_json JSONB NOT NULL,
    required_read_capability TEXT NOT NULL,
    token_binding TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    total_row_count BIGINT NOT NULL CONSTRAINT query_snapshot_count_nonnegative CHECK (total_row_count >= 0),
    CONSTRAINT query_snapshot_pkey PRIMARY KEY (snapshot_id),
    CONSTRAINT query_snapshot_hash_valid CHECK (query_identity_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT query_snapshot_binding_valid CHECK (token_binding ~ '^[0-9a-f]{64}$'),
    CONSTRAINT query_snapshot_count_bounded CHECK (total_row_count <= 1000),
    CONSTRAINT query_snapshot_expiry_after_creation CHECK (expires_at > created_at)
);

CREATE TABLE IF NOT EXISTS query_snapshot_row (
    snapshot_id TEXT NOT NULL,
    ordinal BIGINT NOT NULL CONSTRAINT query_snapshot_row_ordinal_positive CHECK (ordinal > 0),
    row_id TEXT NOT NULL,
    row_version_json JSONB NOT NULL,
    payload_json JSONB NOT NULL,
    CONSTRAINT query_snapshot_row_pkey PRIMARY KEY (snapshot_id, ordinal),
    CONSTRAINT query_snapshot_row_identity_unique UNIQUE (snapshot_id, row_id),
    CONSTRAINT query_snapshot_row_payload_object CHECK (jsonb_typeof(payload_json) = 'object'),
    CONSTRAINT query_snapshot_row_snapshot_fkey FOREIGN KEY (snapshot_id) REFERENCES query_snapshot(snapshot_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_read_revision_entity ON read_revision(scope_key, entity_type, entity_id, published_at);
CREATE INDEX IF NOT EXISTS idx_read_head_revision ON read_head(revision_id);
CREATE INDEX IF NOT EXISTS idx_query_snapshot_expiry ON query_snapshot(expires_at, snapshot_id);
CREATE INDEX IF NOT EXISTS idx_query_snapshot_row_id ON query_snapshot_row(snapshot_id, row_id);

CREATE OR REPLACE FUNCTION ephi_o2_read_revision_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'read_revision rows are immutable' USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS read_revision_immutable_trigger ON read_revision;
CREATE TRIGGER read_revision_immutable_trigger
BEFORE UPDATE OR DELETE ON read_revision
FOR EACH ROW EXECUTE FUNCTION ephi_o2_read_revision_immutable();

CREATE OR REPLACE FUNCTION ephi_o2_query_snapshot_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'query_snapshot rows are immutable' USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS query_snapshot_immutable_trigger ON query_snapshot;
CREATE TRIGGER query_snapshot_immutable_trigger
BEFORE UPDATE OR DELETE ON query_snapshot
FOR EACH ROW EXECUTE FUNCTION ephi_o2_query_snapshot_immutable();

CREATE OR REPLACE FUNCTION ephi_o2_query_snapshot_row_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'query_snapshot_row rows are immutable' USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS query_snapshot_row_immutable_trigger ON query_snapshot_row;
CREATE TRIGGER query_snapshot_row_immutable_trigger
BEFORE UPDATE OR DELETE ON query_snapshot_row
FOR EACH ROW EXECUTE FUNCTION ephi_o2_query_snapshot_row_immutable();
