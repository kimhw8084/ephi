# J. NiceGUI Base integration — reuse the platform, preserve EPHI authority

## 1. Verified integration baseline

Repository: `kimhw8084/nicegui-base`, candidate SHA `000298562d6bcbf6df304edbd41b98b30fe4bfcf`. Read authorities: `AGENTS.md`; `source/nicegui_base/ai/construction_manifest.json`; `source/nicegui_base/__init__.py`; `source/nicegui_base/design/{__init__,tokens}.py`; `examples/nicegui_base/golden_analysis_workspace.py`. The framework identifies version 3.0.0a8 and exactly NiceGUI 3.15.0.

Use the installed package at a pinned artifact/commit. Do not clone source into EPHI and gradually fork it. Framework improvement requests are separate changes with separate tests. Do not launch the Base reference explorer as the EPHI application. Its existing `run_nicegui_base.py` remains the canonical explorer launcher; EPHI extends its own existing CLI through an application bootstrap generated from the supported Base pattern. No extra launcher is added inside the golden framework checkout.

The Root README's Project OS workspaces and examples are not blanket authorization to copy implementation modules. The public framework contract explicitly prohibits copied Workbench/demo implementations, raw NiceGUI in ordinary application code, arbitrary CSS, parallel catalogs/state and direct SQL in page callbacks.

## 2. The concrete integration seam

**UI composition:** `ephi_ui` depends on installed `nicegui_base` plus `ephi.application` DTOs/interfaces. `ephi_ui.bootstrap` receives the already composed application service container and resolved identity adapter. NiceGUI Base owns shell/session/workspace/render lifecycle; company composition owns credentials/sources.

**Workspace state:** one Base `ApplicationRuntime` per correct runtime/session authority, using the golden example's workspace/data-session ownership model. Each episode workspace has one canonical analysis context/selection bus beneath that owner, not a new application-local state machine. Map EPHI scope, episode, viewed revision, exploratory time window and cohort to explicitly named Base state keys. Immutable evidence identity remains in EPHI, not a mutable browser store.

**Data:** new `EphiReadDataSource` adapter implements the installed Base DataSource contract. It translates allowlisted filter/sort/projection requests into EPHI query DTOs, sends current principal/scope through the application service and converts bounded results into Base table/panel data. Stable row keys are canonical EPHI IDs. Unknown sorts/filters are rejected, not ignored. Nulls, units and IDs remain typed. Metadata capabilities must truthfully declare pushdown/paging/aggregation support and be checked with Base conformance tooling.

**Commands:** page callback → Base AsyncAction/lifecycle protection → EPHI command with receipt/version → render committed result. Long computation returns a job handle and uses a `DurableJobAdapter` bridge to EPHI's queue. Base in-process jobs are not sufficient for restart-durable science or qualification.

**Updates:** one workspace refresh controller watches revision changes, uses stale-response protection and cancels obsolete reads. Base browser persistence stores preferences/drafts only; never authorization, manufacturing health, workflow truth or monetary ledger state.

## 3. Reuse map

The symbols below were found in the public exports/manifest. Exact constructor signatures must be resolved from the pinned installed catalog during W0; this document does not invent parameters.

| EPHI surface | Verified Base authority / pattern | EPHI contribution |
|---|---|---|
| Product shell/navigation | `AppShell`, `AppHeader`, `AppSidebar`, `NavigationModel`, `NavSection`, `NavItem` | EPHI destinations and authorized labels |
| Attention | `MonitoringPage`, `MasterDetailPage`, `DataTable`/`ServerDataTable`, filter/preset/selection controls | Query DTO, columns, priority/coverage semantics |
| Episode | `AnalysisWorkspacePage`, `FullScreenWorkspace`, `PanelSpec`, shared workspace/context | Decision task sequence and pinned revisions |
| Timelines | `EngineeringTimeline`, `TimelineChart`, `ProcessTrendPanel`, chart annotation contracts | Temporal tracks, onset/arrival/action semantics |
| Evidence/quality | `RcaEvidencePanel`, `ConfidenceIndicator`, `EvidenceCard`, `DescriptionList`, `InspectorDrawer` | Independent-group data and honest qualifiers |
| Peer/material comparison | `PopulationComparisonPanel`, `ComparePanel`, `DifferenceTable`, `BeforeAfter` | Approved matching/reference input, no recalculation |
| RCA/wafer/genealogy | registered `rca_commonality_*`, `rca_evidence_matrix`, `rca_genealogy_graph`; semiconductor visuals | Precomputed results, bounds and hypotheses |
| Check/closure forms | `Form`, `FormDrawer`, `ValidationSummary`, `DirtyStateGuard`, `ConfirmDialog` | Typed result schemas and command invariants |
| Value | `MetricStrip`, `DataTable`, `ActivityFeed`, appropriate chart wrapper | Claim maturity, correction, attribution and reviewer workflow |
| Family Center | `WizardPage`, `Stepper`, `ProgressSteps`, `DataSourceTable`, status/error/empty panels | Existing control-plane commands and evidence |
| Operations | `MonitoringPage`, `BackgroundTaskIndicator`, `LogViewer`, runtime diagnostics | Approved system status and audit-scoped controls |
| Search/handoff | `CommandPalette`, `SearchResults`, `NotificationCenter`, `ActivityDrawer` | Scoped exact-ID/text queries and deterministic brief |
| Lifecycle | `AsyncLoader`, `AsyncAction`, `AutoRefreshController`, `StaleResponseGuard`, `Debouncer` | Application request/revision identities |

Do not invoke Base reference FDC/SPC/RCA analytics to fill EPHI result fields. Render qualified EPHI results through Base surfaces. A visualization wrapper that insists on recalculating a score needs a provider adapter or controlled extension—not silent substitution.

## 4. Only six EPHI-specific compositions initially

| Composition | Contract | States / interactions |
|---|---|---|
| `EpisodeDecisionBrief` | immutable brief + RevisionVector + capability states | current/historical/stale; open provenance; choose next allowed action |
| `EvidenceDependencyView` | hypothesis rows, grouped evidence refs, contradictions | expand groups; inspect correlated variants; never sum display rows |
| `ExposureDecisionPanel` | exposure revision, non-additive class semantics, deadline/alternatives | class filter, material drilldown, proposal; unknown WIP clearly limited |
| `InvestigationPlanPanel` | stored plan, eligible/excluded checks, template schema | start/reject/result; replan badge; no arbitrary command execution |
| `RecoveryVerificationPanel` | plan + eligibility counts + evidence/result refs | waiting/monitoring/pass/fail/invalidated; matched before-after |
| `DecisionRevisionLens` | revision timeline and fixed decision snapshots | compare latest vs as-known; return to current; provenance |

These are domain compositions of existing Base panels/tables/timelines/forms, not six new raw render engines. Build one narrowly isolated custom overlay only where a registered composition demonstrably cannot express the requirement. Use Base tokens, accessibility, state and validator exception policy; contribute broadly reusable changes upstream rather than accumulate EPHI CSS patches.

## 5. Required construction workflow

In the pinned environment:

```bash
nicegui-base runtime-contract
nicegui-base agent-context "EPHI prioritized episode investigation with evidence, exposure and recovery" --format json
nicegui-base catalog-search "analysis workspace evidence timeline" --format json
nicegui-base recommend-pattern "EPHI engineering attention and investigation" --format json
nicegui-base recommend-visualization "onset interval and maintenance evidence timeline" --schema timestamp --schema measurement --format json
nicegui-base scaffold-plan "EPHI episode investigation" --format json
```

Save the returned authorities and selected signatures as `docs/base_binding_manifest.json` with the framework/package/commit hashes. Generate only the supported application skeleton, then connect EPHI services. Use the installed framework examples to validate actual state ownership; do not combine two generations of examples into duplicate runtimes.

Application completion commands:

```bash
nicegui-base agent-check <ephi-ui-root>
nicegui-base gate <ephi-ui-root>
nicegui-base runtime-contract
nicegui-base runtime-smoke --port 0
```

The CLI commands are verified as documented by the pinned contract; their execution is **NOT_RUN in this audit**. Framework/browser/company-provider/human visual evidence stays PENDING until performed. A changed Base commit is a dependency change requiring interface/screenshot/target requalification, not a floating-main upgrade.

## 6. Integration acceptance

No direct `nicegui.ui`, framework internal renderer imports, raw AG Grid/ECharts, local theme registry or direct SQL in application pages. State restoration, selected cohort, typed identifiers and source scope agree across table/chart/inspection. An old read cannot overwrite a new selection. A reconnect cannot submit a stale command twice. A page unmount disposes timers/listeners. DataSource provider capability claims pass real conformance and bounded-query tests.
