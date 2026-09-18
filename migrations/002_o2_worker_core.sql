-- CHG-126: narrow PostgreSQL durable worker/lease/effect substrate.
-- This migration is intentionally idempotent and contains no product/O3 tables.

CREATE TABLE IF NOT EXISTS job (
    job_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    job_type TEXT NOT NULL,
    semantic_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    status TEXT NOT NULL,
    priority BIGINT NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    attempts INTEGER NOT NULL DEFAULT 0 CONSTRAINT job_attempts_nonnegative CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL CONSTRAINT job_max_attempts_positive CHECK (max_attempts > 0),
    lease_owner TEXT,
    lease_epoch BIGINT NOT NULL DEFAULT 0 CONSTRAINT job_lease_epoch_nonnegative CHECK (lease_epoch >= 0),
    lease_expires_at TIMESTAMPTZ,
    last_failure_code TEXT,
    last_failure_message TEXT,
    last_failure_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT job_pkey PRIMARY KEY (job_id),
    CONSTRAINT job_scope_semantic_unique UNIQUE (scope_key, semantic_key),
    CONSTRAINT job_status_valid CHECK (status IN ('QUEUED', 'RUNNING', 'DEFERRED', 'SUCCEEDED', 'FAILED', 'DEAD_LETTER', 'CANCELED')),
    CONSTRAINT job_lease_owner_pair CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);

CREATE TABLE IF NOT EXISTS applied_effect (
    job_id TEXT NOT NULL,
    effect_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    committed_revision BIGINT NOT NULL CONSTRAINT applied_effect_revision_nonnegative CHECK (committed_revision >= 0),
    result_identity TEXT NOT NULL,
    result_json JSONB NOT NULL,
    committed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT applied_effect_pkey PRIMARY KEY (job_id, effect_key),
    CONSTRAINT applied_effect_job_fkey FOREIGN KEY (job_id) REFERENCES job(job_id)
);

CREATE INDEX IF NOT EXISTS idx_job_claim_order
    ON job(scope_key, priority DESC, available_at, created_at, job_id)
    WHERE status IN ('QUEUED', 'RUNNING', 'DEFERRED');
CREATE INDEX IF NOT EXISTS idx_job_status_updated ON job(scope_key, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_applied_effect_job ON applied_effect(job_id, committed_at);
