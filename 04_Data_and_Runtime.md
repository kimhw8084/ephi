# E–F. Production data, temporal semantics and durable computation

## E1. Technology decision

Default deployment design: an **approved PostgreSQL instance** for operational transactions, indexed product reads, job/lease state, configuration and receipts; an **approved internal immutable artifact store** for bulk snapshots, detailed evidence, replay outputs and release dossiers; existing company warehouses/APIs remain read-only sources. The actual company-approved service/version must be bound during W0. PostgreSQL is a proposed implementation, not a discovered company dependency.

Use typed relational columns for identifiers, scope, filters, versions, timestamps and status; versioned JSON payloads only for heterogeneous evidence/DTO bodies. Raw fab telemetry does not get copied wholesale into the application database. No mandatory Redis, Kafka, Elasticsearch or vector database in V1. Introduce another system only after measured needs exceed these boundaries.

The existing `StateStore` CAS and `SnapshotStateStore` contracts remain for scientific checkpoint/control state. The existing Arrow `MaterializationStore` remains for batches; a separate `ProductReadRepository` provides indexed queries. One interface should not pretend to serve both entire analytical batches and interactive paginated reads.

## E2. Core identities and time

Use UUIDs for new canonical entities; preserve `EPHI-E...` and `VAL-...` legacy aliases in a namespaced mapping. Scope IDs are mandatory on all product entities. Analytical assessment identity is deterministically derived from input snapshot, source execution, context, characteristic, detector/policy versions and pipeline namespace. Episode identity is assigned atomically on episode birth; replay namespace never collides with live namespace. Persist occurrence sequencing and predecessor/recurrence links rather than deriving a new episode ID from display row order.

Store UTC instants; display chosen site timezone with explicit UTC offset on exact timestamps. Context keeps the source timezone and DST interpretation when source timestamps need conversion. Invalid ambiguous local timestamps enter quarantine until the adapter resolves them.

Distinguish `event_at`, `source_available_at`, `ingested_at`, `computed_at`, `published_at`, and `knowledge_cutoff`. Eligibility for an as-known computation requires both the relevant event window and availability no later than cutoff. Late evidence creates a new revision. It never edits a previously shown decision snapshot.

## E3. Logical schema and ownership

All entities include schema version, created_at and scope unless specified. Foreign keys enforce same-scope ownership. The following fields are normative logical design; physical migration files are implementation work.

| Table/entity | Primary key / important fields | Mutation and owner | Important indexes/constraints |
|---|---|---|---|
| `source_snapshot` | snapshot_id; source_id; scope; event_start/end; available_cutoff; manifest_uri/hash; mapping_hash; row_count; status | Immutable ingestion manifest | unique source/partition/revision; cutoff and scope indexes |
| `source_capability` | scope/source/capability; state; latest_event_at; latest_available_at; checked_at; age_limit; reason | Latest projection, source-monitor writer | unique composite key; no implicit READY default |
| `analysis_revision` | revision_id; episode_id; input_manifest_id; event/knowledge windows; qualified_engine_hash; technical payload; evidence_manifest | Append-only scientific publisher | unique semantic computation key; episode/known_at; lineage must exist |
| `episode_identity` | episode_id; scope; asset/context/family; birth_input; legacy_alias; predecessor_id | Identity allocator; stable after creation | unique scope/legacy alias; semantic occurrence uniqueness |
| `evidence_item` | evidence_id/revision; channel; dependency_group; polarity; units; event/available times; refs | Immutable evidence publisher | revision/group/channel; foreign source artifact hash |
| `operational_revision` | op_rev; analysis_rev; exposure_rev; priority_policy; WIP snapshot; technical_hash; payload | Append-only exposure/priority publisher | referenced analysis must match; science hash cannot change |
| `episode_read_head` | episode_id; analysis_rev; op_rev; projection_version; published_at | CAS publisher, current pointer | one head/episode; monotonic projection sequence |
| `attention_projection` | episode_id; scope; work_state; priority/severity; owner; deadline; preventable_count; freshness; reason; row_rev | Query projection; worker + workflow txn | indexes `(scope,open_work,priority,deadline,episode_id)` and owner/state |
| `workflow` | episode_id; cycle_no; version; state; owner/team; ack_at; last_action_at | Application command writer | CAS version; exactly one current cycle; transitions checked |
| `workflow_event` | event_id; episode/cycle; seq; command_id; actor; type; payload; event_at/recorded_at; decision_snapshot_id | Append-only command writer | unique episode/cycle/seq; command relation; event-time not used as write sequence |
| `investigation_check` | check_id; episode/cycle; template_version; target/cohort; prerequisites; state; due_at; version | Check commands | dedup active episode/template/target/cohort/revision; no duplicate execution |
| `check_result` | result_id; check_id; outcome; measurements; unit; artifact hash; supersedes; event/available times | Append-only result writer | valid prior result; sequence and knowledge indexes |
| `plan_revision` | plan_id; episode; evidence/context versions; planner_policy; ranked/excluded checks and reasons | Planner worker | idempotent evidence+policy+completed-check hash |
| `external_action` | action_id; episode; intended/actual time; external system/ref; status; scope; evidence refs | Commands + reconciler | external system/ref unique where available; payload hash for retry |
| `recovery_plan` / `recovery_observation` | plan_id/version; eligible contexts; criteria; action references; sample/time minima; status; evidence/reasons | Immutable plan revision + evaluation worker | one current plan/cycle; unique observation/source identity |
| `decision_snapshot` | snapshot_id; revision vector; capability states; viewed_at; command_id; artifact hash | Append-only at significant command | fixed evidence identity; never “latest” link |
| `historical_case` / `analogue_result` | case_id/version; curation; root-cause confidence; as_known; fingerprint; result input IDs | Curator + history worker | family/context/as_known; exclude same episode/live future outcomes |
| `claim_group` / `claim_attribution` | group_id; economic_event_key; episodes/material scope; contributor roles | Outcome commands | one economic event key; attribution cannot inflate total |
| `value_entry` | entry_id; group; category; evidence_state; amount/currency or units; event/known times; supersedes | Append-only value writer | unique superseding successor where applicable; group/category/known_at; NUMERIC monetary amount |
| `qualification_workspace` / `gate_record` / `promotion_record` | existing domain IDs; family; capability; release; evidence hashes; command receipts; versions | Existing control-plane service backed durably | preserve existing receipt and CAS behavior; current evidence identity |
| `job` / `job_dependency` | job_id; type; semantic_key; scope; payload hash; status; available_at; attempts; lease_owner/epoch/expiry | Queue adapter | unique semantic key; ready-job partial index; dependency DAG validation |
| `applied_effect` | job_id; effect_key; input_hash; committed_rev | Same transaction as each local effect | unique job/effect; replay no duplicate scientific/workflow side effect |
| `outbox` / `delivery_attempt` | event_id; type; revision; destination; status; next_attempt; external key | Command/publisher transaction, transport worker | unique logical event/channel/recipient/version; not exactly-once external delivery |
| `command_receipt` | scope/subject/command_id; payload_hash; status; result_ref | Command transaction | unique; result redaction respects current permissions |
| `user_view` | user/scope/view_id; filter/order/layout schema; preference_version | Preference command | scope bound; no scientific/authorization meaning |

## E4. Atomic publication and read consistency

A compute job reads a fixed input manifest and prior checkpoint, computes outside database locks, writes any immutable artifacts, then performs a short transaction: revalidate lease epoch and expected previous checkpoint/head; insert revision and effect receipt; update checkpoint; publish the coherent head; update affected attention rows; append outbox event; commit. Referenced artifacts must exist with verified hashes before head publication. Orphan artifacts from failed publication are safe and later collected by reference-aware retention.

Publish operational results only against their stated analytical revision. A late WIP result may update operational urgency while retaining exactly the same technical hash. A stale worker cannot overwrite a newer head just because its computation finished later. Reject mismatched/reordered publication and schedule a recomputation if needed.

Episode brief reads the current head and workflow within one database read transaction and returns a revision vector. Expensive evidence and chart artifacts are then fetched by immutable references. A client may display “new revision available”; it must not silently splice a new exposure population into an old causal comparison.

Workflow commands should make their small attention-state updates in the same transaction, ensuring read-your-write. Analytical projections can lag; their visible `published_at` and capability freshness explain that lag.

## E5. Temporal value correction — mandatory F04 fix

For cutoff `t`, an entry is active when it was known at or before `t` and **no superseding entry known at or before `t` replaces it**. Do not call a present-day `active_entries()` and then filter out future rows. Apply this rule to episode reports, program summaries, exports and claim detail alike.

Example fixture: original cost 10 known January 1; correction 20 known January 3. January 2 report remains 10; January 4 report is 20. A decision-time report links the cutoff explicitly. A restated report uses latest-known values and is labeled restated. Supersession chains cannot branch or cross claim/category. Different currencies cannot be summed without an approved, dated FX policy.

## E6. Exposure identity and input requirements

Historical exposure deduplicates repeated suspect executions by material, preserving execution IDs. A material can also have a future rework exposure; therefore historical and future class totals are not automatically additive. Provide `distinct_material_union_count` and separate event/opportunity counts. A visual stacked bar is permitted only for a genuinely disjoint classification of the selected population.

Require explicit asset/context/operation/route identity, as-of-filtered eligible events, input snapshot IDs and UTC times at the exposure service boundary. If a caller supplies future historical events or mismatched context, reject rather than assume upstream correctness. Alternative qualification, health, compatibility and capacity/availability are separate facts; an unknown availability is not confirmed usable capacity.

## E7. Retention and recovery

Proposed operational defaults: retain workflow/decision/qualification/value evidence and its required source references for 24 months; keep hot diagnostic logs 30 days and terminal job operational rows 90 days after durable receipt archival. These are design defaults requiring company retention approval, not compliance assertions. Legal/quality holds override deletion. Open episodes and referenced artifacts are never collected by simple age alone.

Snapshot/backup must produce a consistent set of state versions and referenced immutable artifacts. Ordinary CAS on individual keys is insufficient for a consistent disaster-recovery snapshot, as the shipped `SnapshotStateStore` already states. Restore to an isolated clean namespace, verify hashes/versions/referential completeness, replay accepted post-snapshot inputs, then switch traffic after reconciliation. Proposed target: zero lost acknowledged commands under a single web/worker process crash; disaster RPO<=15 minutes and RTO<=60 minutes only after a production-like restore rehearsal. Cross-region disaster continuity is outside V1 unless required.

## F1. Job taxonomy and triggers

| Job | Trigger | Inputs / side effects | Priority and isolation |
|---|---|---|---|
| `ingest_partition` | approved source arrival or scheduled watermark poll | snapshot manifest, canonical batch, quarantine diagnostics | separate source I/O budget |
| `evaluate_health` | accepted partition | qualified detector/metrology state → immutable revisions/checkpoint | partition by family/context/asset; preserve event/knowledge ordering |
| `refresh_exposure` | new onset/episode, WIP snapshot, alternative change | same analysis rev + fresh operational inputs → exposure/priority revision | prioritize impending decision deadlines |
| `publish_product_view` | coherent scientific/op results | indexed projections + outbox | small bounded work; do not run full history |
| `evaluate_recovery` | new eligible observation or source status change | locked recovery policy + matched context → progress/result | same ordered subject partition |
| `refresh_plan` | new evidence, check result, action, capability change | eligible ranked checks with reason trace | coalesce superseded requests |
| `refresh_history_rca` | meaningful evidence/exposure revision or explicit request | bounded scoped cases/cohorts → stored results | lower priority; cancel obsolete queued request |
| `reconcile_external_action` | action request or timeout | approved external read → observed/unknown status | never assume request success |
| `deliver_notification` | committed outbox event | transport receipt / retry / reconciliation | own queue so delivery cannot block science |
| `run_qualification_stage` | authorized campaign command | existing launch/control-plane stages + artifacts | isolated replay namespace/resources |
| `refresh_value` | action/outcome/review | candidate claim/read projection, not automatic validation | idempotent economic event key |
| `source_health`, `projection_repair`, `backup_verify` | schedule | diagnostics, reconciled heads, backup evidence | separate operation budget |

## F2. Queue, leases and retries

Preserve the reference queue for deterministic/local fixtures. Implement a production queue port supporting claim, heartbeat, complete, fail, defer, cancel and inspect; adapt `WorkerHost` instead of scattering worker loops.

Use row-level claims in a short transaction. PostgreSQL `FOR UPDATE SKIP LOCKED` is appropriate for distributing queue claims, not for consistent analytical reads [E1]. Claim increments a fencing epoch. Every local effect commit checks `(job_id, epoch, owner)` and unexpired lease against authoritative database time. Proposed lease=120s, heartbeat=30s; jobs exceeding the lease must heartbeat independently and checkpoint bounded work. Long work does not hold the claim transaction open.

Delivery semantics are **at least once**. Exactly-once local logical effects come from unique semantic keys plus effect/receipt transactions, not from the queue promise. A stale worker may have computed an artifact but cannot publish it. External effects use idempotency/reconciliation, not an exactly-once claim.

Retry transient connectivity, serialization conflicts and rate limits with capped exponential backoff and jitter; honor provider retry guidance. Proposed 5 attempts, 5s initial, 5min cap. Schema ambiguity, qualification failure, invalid units and forbidden actions are not automatically retried. Dead-letter entries retain reason and failed stage. Retrying a failed stage creates a new audited attempt with the same logical effect key; it cannot bypass a gate.

Fairness: reserve capacity for ingestion/health and operational deadlines; replay/backfill cannot starve live work. Bound concurrency per source and family. Avoid a single failed family stopping all others. Job dependency cycles are rejected at enqueue; missing/failed prerequisites become visible blocked states.

## F3. UI propagation

Use one Base-owned refresh coordinator per workspace. Subscribe/poll a lightweight authorized revision feed, default every 5 seconds while foreground, back off while hidden/disconnected. At scale, an optional invalidation transport replaces polling behind the same contract. Invalidation is a hint; the database remains authoritative.

Refresh only changed panels. Pause row reordering during selection, editing or keyboard traversal and display an “updates available” action. On reconnect, reload revisions and revalidate permissions before enabling writes; retain unsaved safe drafts with a conflict explanation. No duplicate timer for every chart. No long-lived durable job in a page coroutine or a framework in-process task. The official NiceGUI runtime uses an event loop; synchronous I/O/CPU work must be isolated appropriately [E2], while restart-durable work uses the external EPHI job role.

## External technical references

[E1] PostgreSQL current SELECT documentation, queue-oriented use and limitations of SKIP LOCKED: `https://www.postgresql.org/docs/current/sql-select.html` (accessed 2026-09-15).
[E2] NiceGUI official documentation, event loop and CPU/I/O helpers: `https://nicegui.io/documentation` (accessed 2026-09-15). Integrate through NiceGUI Base public lifecycle/async authorities, not direct application imports of NiceGUI.
[E3] FastAPI lifespan documentation: `https://fastapi.tiangolo.com/advanced/events/` (accessed 2026-09-15). Composition startup/shutdown owns pools and adapters; mounted-app lifecycle needs an explicit tested owner.
