-- CHG-167 / O5.1: durable decision-loop aggregate access path.
--
-- The existing aggregate_state table remains the sole workflow/decision-loop
-- authority.  Checks, externally observed action records, locked recovery
-- plans/observations, cycle history and closure records are bounded JSON
-- state in this aggregate and are committed by VersionedAggregateCommand-
-- Executor together with its receipt, audit and outbox rows.  This additive
-- index makes the new aggregate identity explicit without introducing a
-- second command, state, recovery, or read authority.

CREATE INDEX IF NOT EXISTS idx_o5_decision_loop_scope_episode
    ON aggregate_state(scope_key, aggregate_id, version)
    WHERE aggregate_type = 'ephi_decision_loop';
