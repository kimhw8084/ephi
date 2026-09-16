# C–D. Target modules, authority and application contracts

## C1. Deployment and code organization

Use one versioned EPHI distribution with a web role, analytical worker role, operational worker role and qualification/replay worker role. They share domain contracts and release identity, not mutable Python globals. Start with one web process and independently managed workers. Multi-replica UI requires separately verified session/WebSocket routing and reconnect behavior; it is not the initial scaling mechanism.

```text
src/
  ephi/
    domain/                     existing domain contracts; conservative additions
    canonical/                  existing canonical execution handling
    detectors/, populations/    existing scientific calculations
    evidence/, health/, metrology/, quality/, episodes/
    exposure/, history/, rca/, value/
    autopilot/, port/, onboarding/, launch/, qualification/
    operations/, runtime/, reliability/, repositories/, adapters/
    advisory/                   preserve pure mappings; legacy facade compatibility
    application/                NEW product use cases, no UI/web dependencies
      context.py                Principal, AccessScope, CommandContext, RevisionVector
      contracts.py              DTOs, cursor, capability envelope, typed errors
      ports.py                  product read repository, UoW, authz, job submission
      attention.py              bounded attention and saved-view queries
      episodes.py               decision brief, evidence, comparison, revisions
      investigations.py         checks/results, reviewed interpretation, plan
      actions.py                ownership, external action records, recovery, closure
      assets.py                 longitudinal drilldowns
      outcomes.py               claim submission/review and temporal value reads
      families.py               adapters to existing onboarding/qualification services
      operations.py             health, freshness, retry/cancel requests
      notifications.py          event-to-message policy; no transport calls in txn
    product/                    NEW renderer-independent product policies
      investigations/           curated check templates and eligibility/ranking
      recovery.py               operational verification plan + eligibility
      attention.py              priority reasons, grouping, escalation policies
      temporal.py               as-known supersession and lineage selection
    infrastructure/             NEW generic production adapters
      postgres/                 typed repositories, unit of work, queue, read models
      artifacts/                immutable artifact implementation boundary
      notifications/            durable delivery orchestration; company transport injected
      migrations/               numbered reversible/forward-corrective migrations
    api/                        v1 compatibility and new v2 routes
    cli/                        extend existing CLI; do not introduce competing launcher
  ephi_ui/                      NEW NiceGUI Base driving adapter/package
    bootstrap.py                framework app construction and service injection
    navigation.py               role-aware routes, no authorization authority
    context_bridge.py           EPHI scope/revisions ↔ Base workspace context
    providers.py                Base DataSource adapters to EPHI query services
    pages/                      attention, episode, assets, outcomes, families, operations
    components/                 domain compositions only; use Base public primitives
    presenters/                 format view DTOs; no scientific score recomputation
company_port/src/ephi_company/   preserve existing composition/identity/source seams
```

Do not move every existing subsystem into a new `domain/` directory for visual neatness. Keep stable import paths. Adapt `HistoryRCAApplicationService`, `QualificationControlPlaneService`, `CampaignExecutionController`, `ValueLedgerService` and existing worker contracts; do not create competing services with the same state.

## C2. Dependency rules and owners

| Layer | May call/import | Must not depend on |
|---|---|---|
| Scientific engine | Domain, canonical input contracts, numerical libraries | NiceGUI, API, application, company schema, value pricing |
| Domain/product policies | Domain types and explicit policy inputs | UI, HTTP request, database connection, hidden company globals |
| Application | Domain/services, typed ports and product policies | NiceGUI, raw SQL in use cases, transport-specific request objects |
| Infrastructure | Application/domain ports, approved drivers | Page state or presentation widgets |
| UI/API/CLI | Application contracts and identity adapter | Direct tables, detector calls, warehouse scans |
| Company composition | All implementations needed to wire ports | Company-specific imports flowing back into generic EPHI |

Static architecture tests inspect transitive imports as well as immediate imports. Worker orchestration may invoke science; API/page paths may not invoke heavy compute. Audit and qualification are downstream consumers of evidence; changing a UI label cannot change qualified scientific artifacts.

Scientific identity/health belongs to EPHI, including when NiceGUI Base offers a similarly named FDC/SPC calculation. Base owns visualization and interactive workspace state. No template sample score becomes an authoritative EPHI score.

## C3. Request identity and revision model

Proposed contracts:

```python
@dataclass(frozen=True)
class RevisionVector:
    analysis_revision: str
    exposure_revision: str | None
    priority_revision: str | None
    workflow_version: int
    plan_version: int | None
    qualification_manifest_id: str

@dataclass(frozen=True)
class CommandContext:
    command_id: str                 # client-generated UUID, persisted across retries
    principal: Principal            # resolved server-side; never trusted from payload
    scope: AccessScope              # intersection of identity and requested product scope
    expected_workflow_version: int | None
    viewed_revisions: RevisionVector | None
    reason: str | None
```

These are new EPHI contracts, not existing NiceGUI Base APIs. The principal contains subject, granted capabilities, scope grants and auth-session revision. Scope is site/area/family and explicitly allowed projects if needed. Asset, episode and lot access must be checked against that scope on every query and command.

Every response returns `request_id`, `generated_at`, `known_at`, `revision_vector`, `capabilities`, `warnings` and typed data. List endpoints include a stable cursor and `query_snapshot_id`. Time-series results include unit, reference identity, aggregation/downsampling method and raw-row availability.

`known_at` identifies the server-recorded publication represented by the response; it is not interchangeable with a source's claimed availability time. Historical responses also identify `temporal_mode` and `knowledge_cutoff` under E2 in [04_Data_and_Runtime.md](04_Data_and_Runtime.md). The revision vector is an opaque identity comparison contract, not a lexicographically sortable timestamp.

`expected_workflow_version` is mandatory for mutations of an existing workflow. Commands on checks, claims, plans or qualification aggregates also carry their own expected versions; null is valid only for a documented create/no-existing-aggregate operation. Decision-dependent commands require `viewed_revisions`. Compare the relevant components at execution: closure/recovery/check execution must reject stale prerequisites, while a historical note or observed external action may retain an old decision reference without asserting current scientific validity. Return refreshed permitted context for reconciliation, never silently rebase the user's decision.

`CapabilityStatus` has `state=READY|PARTIAL|STALE|UNAVAILABLE|INSUFFICIENT|ERROR|NOT_QUALIFIED`, source IDs, observed/available/watermark times, age limit, reason codes and affected outputs. `NOT_AUTHORIZED` is handled separately from absence and need not reveal an inaccessible source's existence. Unknown numeric values are null with reason, never default zero.

## D1. Query contracts

| Query | Required inputs | Output / limits | Authority and failure behavior |
|---|---|---|---|
| `ListAttention` | scope, filters, order, cursor, page_size<=100 | rows, counts for allowed scope, next cursor, snapshot ID; default 50 | Indexed work projection; no rendering all episodes. Unknown filter/sort -> validation error |
| `GetEpisodeBrief` | episode ID, latest or immutable snapshot ID | header, independent health/work states, summary, next work, revision vector | Coherent single-statement or repeatable-read snapshot as specified in E4; historical workflow pinned to decision snapshot; inaccessible/absent indistinguishable |
| `GetEpisodeEvidence` | episode, revision, hypothesis/polarity/channel/group, cursor | grouped evidence summary or paged members, provenance IDs | Preserve dependency groups; no refusion |
| `GetTimeline` | episode, revision, event window, mode | bounded aligned tracks, onset interval, arrivals/actions | Source materialization; unsupported raw resolution -> asynchronous job or explicit limit |
| `GetExposure` | episode, exposure rev, class/route/lot filters, cursor | mutually explained classes, material records, execution IDs, total union count | Invalid/missing WIP yields unavailable future fields, not 0 |
| `GetComparison` | episode, revision, approved cohort IDs, characteristic | observed/reference/peer distributions and matching diagnostics | Changing cohort generates an exploratory comparison, not new authoritative episode truth |
| `GetInvestigationPlan` | episode, plan version | top checks, alternatives, exclusions, rationale | Stored plan result with prerequisites; stale plans labeled |
| `GetAnaloguesAndRCA` | episode, analytic revision, limit<=20 | stored ranked candidates, similarities, contradictions and curation | If not ready, state plus job status; no heavy computation in query |
| `GetActivity` | episode/cycle, cursor | ordered human + analytical events with timestamps/actors | Immutable event stream; default newest 50 |
| `GetAssetHistory` | asset, time window, scoped context | timeline, episodes, interventions, active obligations | Summary projection; same source identifiers across navigation |
| `GetValueReport` | scope, period, knowledge cutoff, currency | opportunity/observed/validated separately, costs, coverage, claim IDs | Temporal supersession and dedup at claim-event level; currency mixing rejected |
| `GetFamilyWorkspace` | family, capability, release | mapping/data reality/replay/gates/shadow/promotion | Adapts existing control plane, no parallel gate engine |
| `Search` | scope, term, type, limit<=20 | exact-ID first; aliases then approved text fields | Scope filtering before ranking/snippets; no exposure via autocomplete counts |

No UI-only sort may imply a new operational priority. The API supplies sort keys and explanations. User sorts can rearrange visible presentation without modifying P1–P4.

## D2. Command contracts

Every write checks capability and scope, validates expected versions, persists a command receipt and emits an outbox event in the same transaction. Input text is content, never executable SQL/instructions.

| Command | Key payload | Durable effect | Preconditions / failure |
|---|---|---|---|
| `ClaimEpisode` / `AssignOwner` | episode, requested owner/team, reason | ownership/workflow version + audit | qualified owner in scope; conflict shows current owner, no silent overwrite |
| `Acknowledge` / `StartInvestigation` | episode | lifecycle event; acknowledgment time | valid transition; repeated same command returns prior receipt |
| `CreateCheck` | template version, episode, target/cohort, desired window | check record + eligible execution/request job | current prerequisites; manual check allowed with explicit provenance |
| `SubmitCheckResult` | check, result enum, measurements/artifact refs, event time | immutable result; check completion; replan event | units/schema/identity validated; result supersession appends |
| `RecordObservation` | episode, typed note, refs | human evidence event | cannot edit scientific assessment; note identified as human interpretation |
| `ProposeContainment` | material/asset scope, reason, intended system/action | proposal and requested approval, no machine hold | qualified decision capability; source freshness checked; stale proposal warns/blocks according to policy |
| `RecordExternalAction` | action type, external reference, actual effective_at, actor, scope | observed action + decision evidence link | record may be allowed during source outage; it does not certify success or restore health |
| `StartRecovery` | plan template version, action(s), characteristic/context scope | locked recovery plan + monitoring obligation | validated plan eligibility, independent from human lifecycle |
| `SubmitClosure` | disposition, rationale, result/action/recovery refs, residual risk | closure event + optional outstanding-value task | true-issue closure requires qualified recovery or reviewed exception; benign/data issue not forced through false recovery |
| `ReopenInvestigation` | closure ID, new reason/evidence | new cycle number; prior closure preserved | authorized; cannot rewrite historical resolved cycle |
| `LinkDuplicate` | source/target episode | relationship + closed duplicate work when approved | no destructive merge; union exposure and claim attribution handled explicitly |
| `SubmitValueClaim` / `ReviewValueClaim` | event key, attribution, evidence, amount/rate refs | claim/approval/rejection or superseding revision | reviewer independent as policy requires; no unsupported validated amount |
| `RequestRecompute` | episode/family, target snapshot, reason | job only | no UI thread computation; archived view remains readable |
| `RunQualification` / `ApprovePromotion` | family, capability, release and gate identities | existing control-plane commands + durable receipt | current complete evidence; approvals not accepted merely by button click |

## D3. Transaction algorithm

Within one bounded transaction: resolve authoritative scope; load command receipt by `(scope, subject, command_id)`; if same command hash return prior result; differing hash -> `IDEMPOTENCY_CONFLICT`. Lock/read aggregate; validate expected version and domain preconditions; append event and update aggregate/read projection; insert outbox item and receipt; commit; then return receipt with new versions. No external connector/network calls inside this transaction.

The payload hash covers the command type, canonical target/scope, expected versions, viewed revisions and normalized domain payload. It excludes transport/request IDs and credentials. Check current authorization before returning any prior receipt and redact its result under current permissions; revocation does not authorize another effect or disclose the original response.

Concurrent first attempts can both miss a receipt. Enforce its composite uniqueness in the database. If a duplicate-key race occurs, roll back the entire losing transaction, then read the committed receipt in a fresh transaction and apply the same hash/authorization checks. No partial effect from the losing attempt survives. If an expected-version conflict occurs during an identical in-flight retry, resolve the committed receipt before reporting a conflict. For an ambiguous commit outcome, the client retries the same command ID and payload. A user-edited payload is a new command ID. Serializable/deadlock retries restart the whole bounded transaction with the same ID and recheck preconditions.

An external action cannot be atomically committed with an EPHI transaction. For supported downstream requests use a durable outbox, external idempotency key and reconciliation. The action state is `REQUESTED`, `ACKNOWLEDGED`, `OBSERVED_EFFECTIVE`, `FAILED` or `UNKNOWN`. A timeout means unknown until reconciled, not successful or safe to repeat blindly.

In V1, downstream requests are approved work requests, tickets and notifications only. Equipment holds, routing, recipes and disposition remain actions performed by authorized humans in external systems. EPHI records proposals and observations; its outbox is not an actuator. Cancellation or expiration of an unexecuted proposal is recorded explicitly and cannot be inferred from a delivery timeout.

## D4. Error and degraded contract

`NOT_FOUND` for absent or inaccessible entity; `FORBIDDEN_ACTION` for a visible entity's unauthorized command; `VERSION_CONFLICT` includes refreshed allowed context; `IDEMPOTENCY_CONFLICT`; `INVALID_TRANSITION`; `SOURCE_NOT_READY`; `NOT_QUALIFIED`; `QUERY_TOO_BROAD`; `QUERY_SNAPSHOT_EXPIRED` with a restart-query instruction; `JOB_PENDING`; `VALIDATION_FAILED`; `DEPENDENCY_UNAVAILABLE`; `RETRYABLE_STORAGE_FAILURE` with no false success.

Use HTTP 404/403/409/422/503 and 202 for accepted jobs as appropriate; UI handles the same domain errors through its service adapter. A missing exposure source is a capability state on a known episode, not the same 404 as a missing episode. Do not leak raw SQL, tokens or upstream error text.

## D5. Compatibility

Keep `/v1/` endpoints during migration, adapting their existing DTOs to new reads and commands. Introduce `/v2/` envelopes/cursors/version preconditions and an explicit source-availability distinction. New human writes require a command ID and version; old write callers require a documented deprecation transition rather than silently bypassing concurrency. Freeze API examples in contract tests. Read-only archived snapshots remain stable across UI releases.
