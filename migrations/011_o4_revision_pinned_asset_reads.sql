-- CHG-234: retain the declared O4 freshness policy beside its immutable
-- snapshot so as-of source health never depends on the mutable capability row.
-- Existing snapshots remain unchanged and explicitly lack reconstructable
-- historical freshness policy until republished as a new source revision.
ALTER TABLE source_snapshot
    ADD COLUMN IF NOT EXISTS freshness_age_seconds BIGINT
    CHECK (freshness_age_seconds IS NULL OR freshness_age_seconds > 0);
