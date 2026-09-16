# R–S. Traceability, decisions and remaining evidence

## R1. Requirement-to-implementation traceability

| Requirement | Architecture / application | State | UI | Key test / gate | First complete wave |
|---|---|---|---|---|---|
| R1 Exposure prevention | existing exposure engine + `GetExposure`, `ProposeContainment`, `RecordExternalAction` | source/operational revisions, action, decision snapshot | Episode Exposure, Attention deadline | temporal/material dedup/unknown WIP; G02/G06/G07 | W3 subject to source readiness |
| R2 Attention | `ListAttention`, work projection, notification policy | attention rows, workflow/obligations, outbox | Attention and previews | F03 continuity, cursor, alert grouping; G04/G08/G10 | W3 |
| R3 Investigation room | brief/evidence/timeline/comparison queries | immutable evidence/read head + view context | canonical episode tabs | coherent revisions, contradictions, exploration pin; G07/G09 | W3 |
| R4 Planner | `GetInvestigationPlan`, CreateCheck/SubmitResult | templates, plan revisions, checks/results | next work + matrix | no fake probabilities, eligibility/redundancy; G06/G09 | W4 |
| R5 Action/recovery | action and lifecycle commands; scientific eligibility | workflow/events, action/recovery/closure cycle | action side sheets + Recovery | F05 and scoped closure/reopen; G03/G05/G09 | W3 |
| R6 History/RCA | adapt HistoryRCAApplicationService, worker compute | curated case/fingerprint/result | analogue compare and candidates | temporal lookup, same-episode exclusion, controls; G02/G06 | W4 |
| R7 Value | value services + temporal selection and reviewer commands | claim group/event, ledger revisions, rates | Outcomes and episode closure | F04, duplicates/currencies/attribution; G11 | W5 |
| R8 Onboarding | existing AutoPort/port/launch/control plane | mapping/data reality/replay/gates/promotion | Family Center | ambiguity, qualification and release identity; G06/G12 | W5; portability W7 |
| T1 Integrity | boundary tests + temporal/recovery policies | immutable sources/revisions | provenance + as-known lens | G01/G02/G03/G06 | W0 onward |
| T2 Durable security | app authz/UoW/receipts/queue fencing | scope, grants, jobs/effects/audit | conflict/permission/reconnect states | G05/G07 | W1 onward |
| T3 UX/performance | Base runtime/data providers, bounded queries | query snapshots/preferences | all surfaces/states | G08/G09/G10 | W6 release |
| T4 Qualification | existing control plane + target evidence | release/family/capability manifest | Family/Operations gates | G00/G06/G12 | every promotion |

## R2. Requested A–S artifact coverage

| Mission output | Primary file |
|---|---|
| A Executive product architecture | 01 |
| B Current source assessment | 02 plus evidence/ |
| C Exact target modules | 03 |
| D Domain/application contracts | 03 |
| E Persistence/schema/temporal state | 04 |
| F Workers/runtime/failure behavior | 04 |
| G Navigation/information architecture | 05 |
| H Page/interaction/state specifications | 05 |
| I Investigation deep design | 05 and 06 |
| J NiceGUI Base integration | 07 |
| K Notifications | 08 |
| L Security/authorization | 08 |
| M AI augmentation | 08 |
| N Performance architecture | 09 |
| O Test/qualification/observability | 09 and 08 |
| P Migration | 09 |
| Q Implementation roadmap | 09 |
| R Traceability | 10 |
| S Decisions and risks | 10 |

## S1. Decisions to commit

| ADR | Decision | Rejected alternative / consequence |
|---|---|---|
| 01 | Preserve tested science, repair proven integrity defects | Freeze every line would retain false recovery and time-travel ledger defect; rewrite loses tested semantics |
| 02 | Modular monolith with durable worker roles | Microservices increase coordination before measured need; extraction remains possible through ports |
| 03 | Explicit product application boundary adapting existing services | Pages/API directly calling repositories bypass concurrency/auth and duplicate behavior |
| 04 | Proposed PostgreSQL + immutable artifacts, approved company source adapters | Pod-local state does not support multi-process collaboration; extra broker/search infrastructure deferred |
| 05 | Separate science, urgency, work, recovery and value maturity | One status makes recovered-but-unfinished work disappear and overstates outcomes |
| 06 | Immutable analytical/decision revisions and as-known queries | Overwritten latest-only state cannot reproduce decisions or historical value |
| 07 | NiceGUI Base installed/pinned; public catalog + domain compositions | Copying demos creates a fork; Base sample algorithms are not EPHI authority |
| 08 | Three daily navigation entries plus two centers | Seven equal dashboards obscure the primary engineering loop |
| 09 | Deterministic context-aware planner first | Uncalibrated Bayesian/LLM planner creates false certainty; calibrated optimization is conditional later |
| 10 | Human-controlled manufacturing actions with external reconciliation | Autonomous holds/routing expand safety/scope beyond current evidence |
| 11 | Claim-group dedup and independent value review | Summing episode estimates inflates ROI; software can be valid even before benefit matures |
| 12 | Metrology default first, selected by data/action readiness | Code maturity alone does not prove the highest-return family; unavailable data stays explicit |
| 13 | Dual-stage release: source-correct and target-qualified | Unit tests cannot grant company-source, browser or human acceptance |
| 14 | Single web role initially; scale workers first | Multiple UI workers without verified connection/session handling is not an assumed deployment feature |

## S2. Missing company facts and decided fallbacks

| Missing fact | Design treatment | Gate requiring real evidence |
|---|---|---|
| Approved transactional backend and object store | Implement ports against approved services; Postgres is default proposed backend | W1 durable integration / W2 company deployment |
| Real identity groups and scopes | Capability model + company IdentityResolver; production startup fails if unresolved | G07 before non-demo use |
| Actual tables/units/available-at fields | AutoPort review/type firewall, source snapshot contract; ambiguity blocks | G06 |
| Qualified family thresholds/recovery criteria | Versioned family policy; no generic invented thresholds | G03/G06 |
| WIP/routing/alternative availability | Capability unavailable; no preventable-count or deadline claims | R1 qualification |
| Approved action authority and communication channels | Human external action contract; no autonomous commands | W3 controlled pilot |
| Real incident volume, loss rates, effort baseline | Instrument and review pilot; no numeric ROI claim from code | R7 validation |
| Template target-browser approval | Pin inspected candidate; run installed-browser/human gates | G08/G09/G12 |
| Retention and disaster policy | Explicit proposed defaults; company approval and restore test | G10/G12 |

These are implementation bindings and evidence obligations, not unanswered product architecture questions. W0/W1 can begin once the absent original application source is restored and its identity verified; package review alone cannot satisfy that prerequisite. Repository corrections and their gate mappings are recorded in [13_Package_Review.md](13_Package_Review.md).

## S3. Remaining risk register

Highest risks: confounded measurement/process inference; false recovery under sparse data; delayed WIP creating obsolete urgency; workflow/analysis version mismatch; repeated external actions after a timeout; poor prior-case curation; source scope leakage through cached reads; framework API/version drift; a polished demo being mistaken for qualified production; overreported value. Each is tied to a specific gate above.

Capacity/performance targets and time savings remain proposed. Family validation may reject a detector/recovery policy; preserve the ability to disable one capability while retaining honest read-only evidence and human work continuity. Never compensate for failed scientific qualification with a smoother UI.
