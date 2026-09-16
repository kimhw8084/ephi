# K–M. Notifications, authorization, AI and operational controls

## K1. Attention and notification lifecycle

Canonical state is EPHI's episode/workflow, not a chat/email thread. A committed domain/application event enters the outbox. The delivery worker computes authorized recipients and channel preferences, applies deduplication and records delivery attempt/acknowledgment. A notification contains episode identity, what changed, why it matters, source/knowledge cutoff, key limitation, next authorized action and a deep link. Sensitive raw measurements/material lists remain in the authorized application unless policy explicitly allows export.

Notify on new actionable work; meaningful priority/risk escalation; unsupported ownership delay; newly available result requiring action; failed recovery; material source degradation that affects a live decision; assignment/handoff; closure. Repeated detector observations within the same unchanged episode do not create new messages.

Dedup key: `(episode/cycle, event_kind, material_change_signature, recipient, channel, policy_version)`. Coalesce equivalent events within a configurable short window; never coalesce away a higher-priority deadline/risk change. Email/enterprise messaging are company adapter capabilities, not assumed available in the codebase.

Proposed pilot response policy, to approve by family: P1 unowned triggers immediate in-app/team dispatch and escalation after 5 minutes without acknowledgment; P2 after 30 minutes; P3 batched into work list/digest; P4 no interrupt by default. These numbers are workflow targets, not guarantees of safety or existing fab policy. A supported exposure deadline earlier than escalation time overrides batching and calls for the approved immediate channel. No available accountable channel is a pilot blocker for time-critical claims.

Snooze is personal/team attention behavior with expiration, owner and reason. It never changes scientific severity, source freshness or data retention. Higher risk, failed recovery or expiry reactivates attention. Suppression rules require scope, justification, expiry, audit and qualification when they affect detector/episode behavior. Notification acknowledgment and workflow acknowledgment are related but not automatically equivalent.

Delivery failure remains visible in Operations; retry with capped backoff. An external timeout may have delivered; use supported external idempotency/reconciliation or label duplicate risk. Do not promise exactly-once messaging. A company channel cannot authorize a manufacturing change by interpreting a free-form reply.

## L1. Authorization model

Keep `ephi_company.auth.IdentityResolver` as the company trust seam. Resolve identity on request/session establishment and revalidate security revision on consequential actions. Use server-derived actor, not browser-submitted actor names. API middleware can enforce coarse access, but **every application use case** enforces its own scope and capability, including NiceGUI callbacks that never traverse a REST route.

| Capability | Typical allowed role | Additional conditions |
|---|---|---|
| `episode.read` / `evidence.read` | viewer, engineer, lead | authorized site/area/family; source sensitivity restrictions |
| `workflow.claim`, `check.record`, `action.record` | engineer | valid scoped entity, revision/command receipt |
| `workflow.assign`, `closure.exception` | lead | qualified assignee; reason and residual-risk owner |
| `containment.propose` | authorized manufacturing/engineering role | fresh required evidence or explicit recorded uncertainty; no autonomous execution |
| `case.curate` | reviewer | evidence, confidence and prior outcome history |
| `value.submit` | engineer/analyst | economic event identity, evidence, attribution |
| `value.validate` | authorized value reviewer | independent review as required; no self-validation by default |
| `family.map`, `qualification.run` | family owner | approved source access; type firewall; scope |
| `qualification.promote` | approver | complete current gates, independent approval, release identity |
| `operations.control`, `restore.execute` | platform operator | change record/approval; audited impact; no implied scientific/financial role |

Roles are defaults; companies map real groups to these capabilities. Read-only users cannot gain permissions through disabled UI manipulation, direct application call, export or notification deep link. Apply authorization before aggregation, search ranking, result counts, caching and artifact links. Never put entire unfiltered datasets into the browser and merely hide rows.

Cache keys include principal scope/grant version, query, scientific revision and policy. No cross-user cache is allowed unless the result is proven identical under the full access scope. Revoke access by invalidating session/grant version and protected subscriptions; client storage is not an access control.

## L2. Data handling and audit

Database credentials and identity validation configuration belong in approved secret delivery, not source/YAML/UI. TLS, cookie/session policy, ingress trust and network access follow company controls and are tested at deployment. Authentication cookies used for state-changing requests require appropriate CSRF/session protection; WebSocket origin/session authorization must be explicitly tested. Do not assume the existing HTTP middleware covers every UI transport.

Uploads: allowlisted types, bounded size, explicit source purpose, quarantined parsing, no executable content; immutable artifact hash and scope. CSV/table exports preserve types and protect spreadsheet consumers from formula injection. Notes/LLM/Markdown display is sanitized and cannot render arbitrary active HTML. Queries use typed parameterization and approved data-source mappings; no UI raw SQL console in V1.

Audit includes actor, granted scope, action, reason, before/after versions, server recorded time, domain effective time, command ID, decision evidence revision, relevant external references and result. Audit append failure rolls back a consequential command. Redact secrets and inappropriate raw data from logs. Revisions are retained rather than overwritten; corrections themselves are audited.

## M1. AI augmentation decisions

**No AI capability is required for the first production loop.** The deterministic decision brief, structured planner, query search and existing historical fingerprints already deliver value.

| Candidate | Useful input/output | Authority and safeguards | Phase |
|---|---|---|---|
| Shift handoff summary | scoped episode DTOs/actions → factual draft | evidence IDs per factual sentence; uncertainty preserved; human review | after complete V1 workflow |
| Investigation/closure draft | scoped timeline/check results → editable draft | cannot close, validate value or assign cause; reviewer accepts text | after V1 |
| Natural-language search assistance | user question → allowlisted structured filter proposal | preview scope/filter; no raw SQL or implicit broadening | later |
| Semantic retrieval of curated cases | approved narrative → candidate case IDs | access filter before retrieval and after; exact structured reranking | only if measured benefit over V1 retrieval |
| Explanation paraphrase | structured reason trace → readable wording | preserves labels/provenance; no invented numbers/action | optional |

LLM prompt input is bounded, scoped and versioned. Retrieved notes/documents are untrusted data, not instructions. Do not let them change tools, permissions or output schema. Model output must be schema validated and grounded against allowed facts; unsupported facts rejected or marked as suggestions. User can inspect the original deterministic facts. AI outage falls back to those facts, not a blank application.

Use only approved internal endpoints/provider retention terms and data classification. The design does not assume a specific current Gemma/OpenAI model, embedding service or endpoint. Log model/prompt version and source snapshot IDs with appropriate minimization. Human feedback becomes reviewed offline training/evaluation material only via qualification; no automatic online scientific self-training.

## O1. Operational observability

Expose four separate status axes: transport/web health, data freshness/coverage, analytical publication health and qualified capability health. Readiness requires configured approved adapters, compatible schema/release, healthy required store and acceptable critical capability state. Liveness merely shows the process is alive.

Metrics: source event/availability/publication lag; missing partitions; quarantine counts; dataset duplicate rate; job backlog/oldest age by priority/family; attempts and lease expiries; failed effect commits; stale-head rejection; materialization latency; request latency; command conflict rate; open/unowned/aging work; notification delivery failure; evidence/qualification expiration; restore evidence age. Avoid material IDs in high-cardinality labels; use trace context for detailed correlation.

Every analytical output and command has a trace chain from source snapshot through job, result revision, product response, decision and outcome. Product telemetry records task interactions and effort only with approved privacy expectations. It must not become an unreviewed employee productivity ranking.

Runbooks cover source outage; bad mapping/unit change; late event correction; database outage; worker crash/lease loss; incorrect projection; notification failure; qualification expiry; release rollback; restore rehearsal; incident response for unintended disclosure. Each specifies degraded user behavior, owner, diagnostic evidence, safe action, verification and rollback. Never “fix” a data outage by filling healthy defaults.
