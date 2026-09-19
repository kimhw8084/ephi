-- CHG-133: narrow generic scoped immutable-artifact catalog.
-- Blob bytes remain in a separate immutable blob adapter.  This table stores
-- no unrestricted filesystem path, public URL, signed URL or company binding.

CREATE TABLE IF NOT EXISTS artifact_catalog (
    scope_key TEXT NOT NULL,
    sha256 TEXT NOT NULL CONSTRAINT artifact_catalog_sha256_valid CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_size BIGINT NOT NULL CONSTRAINT artifact_catalog_byte_size_nonnegative CHECK (byte_size >= 0),
    media_type TEXT NOT NULL CONSTRAINT artifact_catalog_media_type_bounded CHECK (char_length(media_type) BETWEEN 1 AND 128),
    logical_purpose TEXT NOT NULL CONSTRAINT artifact_catalog_purpose_bounded CHECK (char_length(logical_purpose) BETWEEN 1 AND 128),
    object_key TEXT NOT NULL CONSTRAINT artifact_catalog_object_key_valid CHECK (object_key = 'sha256/' || sha256),
    producing_job_id TEXT,
    revision_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT artifact_catalog_pkey PRIMARY KEY (scope_key, sha256),
    CONSTRAINT artifact_catalog_scope_nonempty CHECK (char_length(scope_key) > 0),
    CONSTRAINT artifact_catalog_job_bounded CHECK (producing_job_id IS NULL OR char_length(producing_job_id) BETWEEN 1 AND 128),
    CONSTRAINT artifact_catalog_revision_bounded CHECK (revision_id IS NULL OR char_length(revision_id) BETWEEN 1 AND 128)
);

CREATE INDEX IF NOT EXISTS idx_artifact_catalog_scope ON artifact_catalog(scope_key);

CREATE OR REPLACE FUNCTION ephi_o2_artifact_catalog_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'artifact_catalog rows are immutable' USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS artifact_catalog_immutable_trigger ON artifact_catalog;
CREATE TRIGGER artifact_catalog_immutable_trigger
BEFORE UPDATE OR DELETE ON artifact_catalog
FOR EACH ROW EXECUTE FUNCTION ephi_o2_artifact_catalog_immutable();
