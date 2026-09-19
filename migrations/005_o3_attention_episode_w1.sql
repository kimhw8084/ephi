-- CHG-134: the narrow durable O3 Attention projection.
-- Workflow truth remains the existing aggregate_state authority. Immutable
-- Episode analytical/read truth remains read_revision/read_head. This table
-- is only the bounded indexed Attention projection.

CREATE TABLE IF NOT EXISTS o3_attention_projection (
    scope_key TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    row_version TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    CONSTRAINT o3_attention_projection_pkey PRIMARY KEY (scope_key, episode_id),
    CONSTRAINT o3_attention_projection_payload_object CHECK (jsonb_typeof(payload_json) = 'object'),
    CONSTRAINT o3_attention_projection_scope_nonempty CHECK (char_length(scope_key) > 0),
    CONSTRAINT o3_attention_projection_episode_nonempty CHECK (char_length(episode_id) > 0),
    CONSTRAINT o3_attention_projection_version_nonempty CHECK (char_length(row_version) > 0)
);

CREATE INDEX IF NOT EXISTS idx_o3_attention_scope_priority
    ON o3_attention_projection(scope_key, episode_id);
