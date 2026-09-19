-- CHG-144 / O4.1: bounded immutable metrology source ingress.
-- Raw telemetry remains in the approved read-only source or immutable artifact
-- boundary.  These tables store only manifest identity and capability state.

CREATE TABLE IF NOT EXISTS source_snapshot (
    snapshot_id TEXT NOT NULL,
    schema_version TEXT NOT NULL DEFAULT 'o4.1.v1',
    scope_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    schema_id TEXT NOT NULL,
    mapping_version TEXT NOT NULL,
    mapping_hash TEXT NOT NULL CONSTRAINT source_snapshot_mapping_hash_valid CHECK (mapping_hash ~ '^[0-9a-f]{64}$'),
    unit TEXT NOT NULL,
    reference_population_id TEXT,
    comparable_population_id TEXT,
    required_identifiers_json JSONB NOT NULL,
    source_partition TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    event_start TIMESTAMPTZ NOT NULL,
    event_end TIMESTAMPTZ NOT NULL,
    available_cutoff TIMESTAMPTZ NOT NULL,
    manifest_artifact_sha256 TEXT NOT NULL CONSTRAINT source_snapshot_artifact_hash_valid CHECK (manifest_artifact_sha256 ~ '^[0-9a-f]{64}$'),
    manifest_artifact_byte_size BIGINT NOT NULL CONSTRAINT source_snapshot_artifact_size_nonnegative CHECK (manifest_artifact_byte_size >= 0),
    manifest_artifact_object_key TEXT NOT NULL,
    row_count BIGINT NOT NULL CONSTRAINT source_snapshot_row_count_nonnegative CHECK (row_count >= 0),
    status TEXT NOT NULL CONSTRAINT source_snapshot_status_valid CHECK (status IN ('PUBLISHED', 'PARTIAL', 'INSUFFICIENT', 'QUARANTINED')),
    manifest_hash TEXT NOT NULL CONSTRAINT source_snapshot_manifest_hash_valid CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
    ingested_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT source_snapshot_pkey PRIMARY KEY (snapshot_id),
    CONSTRAINT source_snapshot_identity_scope_unique UNIQUE (snapshot_id, scope_key),
    CONSTRAINT source_snapshot_logical_unique UNIQUE (scope_key, source_id, family_id, capability_id, source_partition, source_revision),
    CONSTRAINT source_snapshot_scope_nonempty CHECK (char_length(scope_key) > 0),
    CONSTRAINT source_snapshot_identity_nonempty CHECK (
        char_length(source_id) > 0 AND char_length(provider_id) > 0 AND char_length(family_id) > 0
        AND char_length(capability_id) > 0 AND char_length(adapter_id) > 0 AND char_length(schema_id) > 0
        AND char_length(mapping_version) > 0 AND char_length(unit) > 0
        AND char_length(source_partition) > 0 AND char_length(source_revision) > 0
    ),
    CONSTRAINT source_snapshot_event_window_valid CHECK (event_end >= event_start),
    CONSTRAINT source_snapshot_artifact_key_valid CHECK (manifest_artifact_object_key = 'sha256/' || manifest_artifact_sha256),
    CONSTRAINT source_snapshot_publication_after_ingest CHECK (published_at >= ingested_at),
    CONSTRAINT source_snapshot_required_identifiers_object CHECK (jsonb_typeof(required_identifiers_json) = 'array')
);

CREATE INDEX IF NOT EXISTS idx_source_snapshot_scope_available
    ON source_snapshot(scope_key, source_id, family_id, capability_id, available_cutoff DESC);

CREATE INDEX IF NOT EXISTS idx_source_snapshot_scope_event
    ON source_snapshot(scope_key, source_id, family_id, capability_id, event_end DESC);

CREATE TABLE IF NOT EXISTS source_capability (
    scope_key TEXT NOT NULL,
    schema_version TEXT NOT NULL DEFAULT 'o4.1.v1',
    source_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    schema_id TEXT NOT NULL,
    mapping_version TEXT NOT NULL,
    mapping_hash TEXT NOT NULL CONSTRAINT source_capability_mapping_hash_valid CHECK (mapping_hash ~ '^[0-9a-f]{64}$'),
    unit TEXT NOT NULL,
    reference_population_id TEXT,
    comparable_population_id TEXT,
    required_identifiers_json JSONB NOT NULL,
    state TEXT NOT NULL CONSTRAINT source_capability_state_valid CHECK (state IN ('READY', 'PARTIAL', 'STALE', 'UNAVAILABLE', 'INSUFFICIENT', 'ERROR', 'NOT_QUALIFIED')),
    latest_snapshot_id TEXT,
    latest_event_at TIMESTAMPTZ,
    latest_available_at TIMESTAMPTZ,
    checked_at TIMESTAMPTZ NOT NULL,
    freshness_age_seconds BIGINT NOT NULL CONSTRAINT source_capability_age_positive CHECK (freshness_age_seconds > 0),
    reason TEXT NOT NULL,
    latest_source_partition TEXT,
    latest_source_revision TEXT,
    CONSTRAINT source_capability_pkey PRIMARY KEY (scope_key, source_id, family_id, capability_id),
    CONSTRAINT source_capability_scope_nonempty CHECK (char_length(scope_key) > 0),
    CONSTRAINT source_capability_identity_nonempty CHECK (
        char_length(source_id) > 0 AND char_length(provider_id) > 0 AND char_length(family_id) > 0
        AND char_length(capability_id) > 0 AND char_length(adapter_id) > 0 AND char_length(schema_id) > 0
        AND char_length(mapping_version) > 0 AND char_length(unit) > 0 AND char_length(reason) > 0
    ),
    CONSTRAINT source_capability_required_identifiers_object CHECK (jsonb_typeof(required_identifiers_json) = 'array'),
    CONSTRAINT source_capability_snapshot_scope_fkey FOREIGN KEY (latest_snapshot_id, scope_key)
        REFERENCES source_snapshot(snapshot_id, scope_key)
);

CREATE INDEX IF NOT EXISTS idx_source_capability_state
    ON source_capability(scope_key, state, checked_at DESC);

-- The canonical reference adapter has no migration ledger yet.  These
-- additions upgrade a database created by an earlier draft of this numbered
-- migration without rewriting accepted source history.
ALTER TABLE source_snapshot
    ADD COLUMN IF NOT EXISTS schema_version TEXT NOT NULL DEFAULT 'o4.1.v1';

ALTER TABLE source_capability
    ADD COLUMN IF NOT EXISTS schema_version TEXT NOT NULL DEFAULT 'o4.1.v1';

CREATE OR REPLACE FUNCTION ephi_o4_source_snapshot_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'source_snapshot rows are immutable' USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS source_snapshot_immutable_trigger ON source_snapshot;
CREATE TRIGGER source_snapshot_immutable_trigger
BEFORE UPDATE OR DELETE ON source_snapshot
FOR EACH ROW EXECUTE FUNCTION ephi_o4_source_snapshot_immutable();
