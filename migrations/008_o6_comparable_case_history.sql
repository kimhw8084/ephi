-- CHG-174 / O6.2: bound the same-scope immutable Episode revision window.
-- The source of truth remains read_revision/read_head; this adds no case store.

CREATE INDEX IF NOT EXISTS idx_read_revision_comparable_history
    ON read_revision(scope_key, entity_type, known_at DESC, published_at DESC, entity_id ASC, revision_id ASC);
