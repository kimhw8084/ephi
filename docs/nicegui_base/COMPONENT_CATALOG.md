# Component & Capability Catalog

Authoritative construction index for Gemma/OpenCode. Exact signatures are in `PUBLIC_API_INDEX.md` and source. **Never invent parameters.**

## Core components

| Key | Public API / purpose | Use when |
|---|---|---|
| `button_group` | **ButtonGroup** — Group related actions without visual fragmentation | toolbar actions, mode actions |
| `split_button` | **SplitButton** — Primary action with closely related alternatives | export alternatives, run options |
| `divider` | **Divider** — Subtle structural separation | sections, menus |
| `collapsible_panel` | **CollapsiblePanel** — Progressive disclosure without leaving context | advanced settings, secondary detail |
| `accordion` | **Accordion** — Grouped progressive disclosure | help, configuration groups |
| `chip` | **Chip** — Interactive compact metadata or filter value | filter value, selection |
| `count_badge` | **CountBadge** — Compact numeric count | notifications, selected count |
| `severity_indicator` | **SeverityIndicator** — Semantic severity with label and non-color cue | operational state, risk |
| `freshness_indicator` | **FreshnessIndicator** — Data recency state | updated time, stale data |
| `data_quality_badge` | **DataQualityBadge** — Data reliability/completeness state | partial data, estimated data |
| `button` | **ButtonSpec** — Standard user action | normal action, secondary action, danger action |
| `action_button` | **ActionButtonSpec** — Async-safe or stateful action | save, run analysis, submit |
| `icon_button` | **IconButtonSpec** — Compact icon-only action with accessible label | toolbar action, row action |
| `surface` | **SurfaceSpec** — Consistent content containment and interaction surface | panel, card, well |
| `badge` | **BadgeSpec** — Compact semantic status or metadata label | status, severity, metadata |
| `text_input` | **TextInputSpec** — Single-line text entry | name, identifier, free text |
| `number_input` | **NumberInputSpec** — Numeric entry with bounds and unit | threshold, count, measurement |
| `textarea` | **TextAreaSpec** — Multi-line text entry | notes, description, comment |
| `search_input` | **SearchInputSpec** — Debounced search entry | global search, table search, entity search |
| `select` | **SelectSpec** — Single choice from known values | filter, form choice |
| `multi_select` | **MultiSelectSpec** — Multiple choices from known values | multi-filter, assignment |
| `autocomplete` | **AutocompleteSpec** — Searchable known values | large option set, entity lookup |
| `combobox` | **ComboboxSpec** — Search/select with optional custom value | tag-like entry, mixed known/custom choice |
| `checkbox` | **CheckboxSpec** — Independent boolean selection | multi-option form, enable choice |
| `checkbox_group` | **CheckboxGroupSpec** — Set of independent boolean choices | feature selection, permissions |
| `radio_group` | **RadioGroupSpec** — Mutually exclusive choice | mode selection, single preference |
| `switch` | **SwitchSpec** — Immediate on/off setting | preference toggle, feature enablement |
| `slider` | **SliderSpec** — Bounded continuous or stepped value | threshold, range tuning |
| `range_slider` | **RangeSliderSpec** — Bounded low/high selection | numeric filtering, window selection |
| `date_picker` | **DatePickerSpec** — Single date selection | effective date, event date |
| `date_range_picker` | **DateRangePickerSpec** — Date interval selection | analysis period, reporting window |
| `time_picker` | **TimePickerSpec** — Time-of-day selection | schedule time, cutoff |
| `datetime_picker` | **DateTimePickerSpec** — Date and time selection | timestamp, scheduled action |
| `file_upload` | **FileUploadSpec** — Validated file selection and upload | attachment, data import |

## Content, metrics, viewers & workflow

| Key | Public API / purpose | Use when |
|---|---|---|
| `metric_card` | **MetricCard** — Canonical KPI/metric presentation | dashboard KPI, engineering summary, clickable metric |
| `metric_strip` | **MetricStrip** — Responsive metric grouping | KPI row, summary metrics |
| `comparison_metric` | **ComparisonMetric** — Current/baseline/delta comparison | baseline comparison, performance delta |
| `key_value_list` | **KeyValueList** — Readable entity properties | detail drawer, metadata |
| `property_grid` | **PropertyGrid** — Dense multi-property presentation | engineering properties, settings summary |
| `entity_header` | **EntityHeader** — Canonical identity/status header | detail drawer, entity page |
| `tree_view` | **TreeView** — Hierarchical navigation/data exploration | equipment hierarchy, process tree, folders |
| `markdown_viewer` | **MarkdownViewer** — Sanitized Markdown presentation | documentation, AI explanation |
| `code_viewer` | **CodeViewer** — Read-only syntax-highlighted code | SQL preview, generated code |
| `json_viewer` | **JsonViewer** — Structured JSON inspection | API payload, diagnostics |
| `log_viewer` | **LogViewer** — Bounded log inspection | diagnostics, run logs |
| `image_viewer` | **ImageViewer** — Image inspection with local-first security | wafer image, metrology image |
| `search_results` | **SearchResults** — Canonical search result list | entity search, global search |
| `stepper` | **Stepper** — Multi-step workflow navigation | wizard, setup process |
| `progress_steps` | **ProgressSteps** — Read-only workflow progress | job progression, approval stages |
| `compare_panel` | **ComparePanel** — Side-by-side comparison surface | baseline/current, two entities |
| `difference_table` | **DifferenceTable** — Field-level differences | configuration diff, record comparison |
| `command_palette` | **CommandPalette** — Keyboard-first search and command execution | global navigation, power-user commands |
| `background_task` | **BackgroundTaskIndicator** — Compact long-running task state | analysis running, export running |
| `notification_center` | **NotificationCenter** — Persistent bounded notification history | notification history, user alerts |
| `activity_feed` | **ActivityFeed** — Chronological system or entity activity | audit history, recent activity |

## Page patterns

| Key | Public API / purpose | Use when |
|---|---|---|
| `dashboard` | **dashboard** — High-level KPI and trend overview. |  |
| `data_explorer` | **data_explorer** — Interactive filtering, analysis, records and contextual drill-down. |  |
| `master_detail` | **master_detail** — Browse entities while preserving selected-entity context. |  |
| `crud` | **crud** — Search, create, inspect and edit managed records. |  |
| `monitoring` | **monitoring** — Operational health, alerts and periodically refreshed data. |  |
| `search` | **search** — Search and refine heterogeneous or entity-oriented results. |  |
| `settings` | **settings** — Structured application/user configuration. |  |
| `wizard` | **wizard** — Guided multi-step task with clear progress and bounded decisions. |  |
| `comparison` | **comparison** — Compare baseline/current populations, entities or scenarios. |  |
| `analysis_workspace` | **analysis_workspace** — Maximum-density resizable chart/table analysis environment. |  |

## Interaction patterns

| Key | Public API / purpose | Use when |
|---|---|---|
| `form` | **Form** — Own validation, dirty state and submission anatomy | structured data entry, edit/create workflow |
| `filter_bar` | **FilterBar** — Primary analytical filtering surface | analysis filtering, search + facets |
| `filter_drawer` | **AdvancedFilterDrawer** — Keep complex filters out of primary page flow | many filters, mobile filters |
| `detail_drawer` | **DetailDrawer** — Inspect an entity without losing page context | quick entity detail, contextual drilldown |
| `form_drawer` | **FormDrawer** — Create or edit while retaining current context | short/medium edit, contextual create |
| `dialog` | **Dialog** — Focused blocking decision or short task | confirmation, small focused task |
| `danger_dialog` | **DangerConfirmDialog** — Confirm irreversible or high-risk action | irreversible deletion, high-risk mutation |
| `popover` | **Popover** — Light contextual information or controls | small secondary details, compact actions |
| `menu` | **ActionMenu** — Compact set of contextual actions | overflow actions, row/context actions |
| `toast` | **Toast** — Transient operation result | save success, short nonblocking failure |
| `alert` | **Alert** — Persistent message scoped to a region | data quality, recoverable warning, inline failure |
| `banner` | **Banner** — Broad page/application condition | system degradation, page-wide warning |
| `state_view` | **StateView** — Durable empty/error/permission/offline condition | no data, no results, load failure, permission, offline |
| `async_content` | **AsyncContent** — Standard content lifecycle semantics | loading, refreshing, empty/error transitions |

## Data tables

| Key | Public API / purpose | Use when |
|---|---|---|
| `data_table` | **DataTable** — General interactive enterprise data grid | most tabular data, selection/filtering/export |
| `server_data_table` | **ServerDataTable** — Server-driven grid for large datasets | 100k+ source rows, database-backed pagination |
| `editable_table` | **EditableTable** — Opt-in validated table editing | small structured edits, admin maintenance |
| `master_detail_table` | **MasterDetailTable** — Expand a record into rich contextual detail | compact drilldown, nested process history |
| `table_toolbar` | **TableToolbar** — Canonical table search/columns/density/export/refresh controls | interactive tables |
| `selection_bar` | **TableSelectionBar** — Bulk actions for selected rows | multi-select actions |

## Visualization

| Key | Public API / purpose | Use when |
|---|---|---|
| `ChartPanel` | **ChartPanel** — Standard themed analytical visualization surface |  |
| `LineChart` | **LineChart** — Trend over ordered/time axis |  |
| `AreaChart` | **AreaChart** — Trend with magnitude emphasis |  |
| `BarChart` | **BarChart** — Categorical comparison |  |
| `StackedBarChart` | **StackedBarChart** — Composition across categories |  |
| `ScatterChart` | **ScatterChart** — Relationship/correlation |  |
| `Histogram` | **Histogram** — Distribution frequency |  |
| `BoxPlot` | **BoxPlot** — Distribution summary |  |
| `Heatmap` | **Heatmap** — Matrix/intensity distribution |  |
| `ParetoChart` | **ParetoChart** — Ranked contributors with cumulative percentage |  |
| `ControlChart` | **ControlChart** — Process trend with control/spec context |  |
| `TimelineChart` | **TimelineChart** — Events or values over time |  |
| `DonutChart` | **DonutChart** — Small-part composition only |  |
| `Gauge` | **Gauge** — Single bounded measure |  |
| `WaferMap` | **WaferMap** — Wafer spatial data |  |
| `SpatialMap` | **SpatialMap** — 2D spatial engineering data |  |
| `DistributionPanel` | **DistributionPanel** — Distribution chart plus statistical context |  |
| `ProcessTrendPanel` | **ProcessTrendPanel** — Process trend plus limit/annotation context |  |
| `ChartCrossFilter` | **ChartCrossFilter** — Semantic chart-to-filter linking |  |
| `PlotlyPanel` | **PlotlyPanel** — Specialist Plotly figure within NiceGUI Base panel anatomy |  |

## Engineering/RCA

| Key | Public API / purpose | Use when |
|---|---|---|
| `EngineeringEntityCard` | **EngineeringEntityCard** — Canonical presentation for tool/lot/wafer/recipe/process entities | When a named engineering entity needs status + compact properties |
| `EngineeringStatusBadge` | **EngineeringStatusBadge** — Canonical engineering operational status | For normal/watch/warning/critical/offline/maintenance/hold state |
| `SpecLimitIndicator` | **SpecLimitIndicator** — Value relative to warning/spec limits | Whenever a numeric measurement must communicate in-spec/watch/OOS state |
| `OutOfSpecIndicator` | **OutOfSpecIndicator** — Prominent OOS treatment | When a spec violation needs explicit user attention |
| `BaselineComparison` | **BaselineComparison** — Current vs reference/baseline semantic delta | For KPI or engineering parameter comparison |
| `ProcessTrendSpec` | **ProcessTrendSpec** — Process trend with spec/control limit context | For time/order trends of process measurements |
| `DistributionComparisonSpec` | **DistributionComparisonSpec** — Affected vs control population distribution contract | For population-shift analysis without implying causality |
| `PopulationComparisonPanel` | **PopulationComparisonPanel** — Summaries and effect-size context for affected/control populations | For exploratory comparison of two populations |
| `CommonalityTable` | **CommonalityTable** — Affected/control exposure commonality table | For ranking overlaps/enrichment while keeping routing vs causal interpretation explicit |
| `EvidenceCard` | **EvidenceCard** — One typed evidence item with direction/strength/source | For root-cause evidence presentation |
| `ConfidenceIndicator` | **ConfidenceIndicator** — Confidence label that does not masquerade as probability | For qualitative/model confidence; show percentage only when explicitly calibrated |
| `RcaEvidencePanel` | **RcaEvidencePanel** — Evidence + contradiction view for one hypothesis | For transparent hypothesis review |
| `RcaWorkspaceSpec` | **RcaWorkspaceSpec** — Reusable hypothesis-oriented RCA workspace contract | For comparing several candidate root-cause hypotheses |
| `EngineeringTimeline` | **EngineeringTimeline** — Chronological engineering events | For PM, alarm, recipe, process, and investigation history |

## Convenience/state

| Key | Public API / purpose | Use when |
|---|---|---|
| `state_store` | **state_store** — Observable framework-agnostic state with atomic updates. | Local/page/session state must notify dependents without UI coupling. |
| `user_preferences` | **user_preferences** — Typed persistent UI preferences. | Theme, density, sidebar, table layouts, filters, favorites or recent entities must persist. |
| `url_state` | **url_state** — Typed deterministic URL query serialization. | Analytical state should be shareable/bookmarkable. |
| `async_action` | **async_action** — Timeout-aware duplicate-safe async action. | Buttons or commands invoke service work. |
| `cancelable_task` | **cancelable_task** — Latest-request-wins cancellable task. | Search/filter/data requests can supersede older requests. |
| `auto_refresh` | **auto_refresh** — Managed periodic refresh with stale/error status. | Monitoring pages need periodic data updates. |
| `debouncer` | **debouncer** — Delay bursty operations until input settles. | Search/filter inputs would otherwise create excessive service calls. |
| `stale_response_guard` | **stale_response_guard** — Prevent older responses overwriting newer state. | Concurrent requests can resolve out of order. |
| `notification_service` | **notification_service** — Central transient feedback service. | Business actions need consistent success/warning/error messages. |
| `preference_service` | **preference_service** — Load/update typed user preferences. | Application code needs persistence without direct storage manipulation. |
| `keyboard_shortcuts` | **keyboard_shortcuts** — Canonical shortcut registry. | Pages need discoverable keyboard-first actions. |
| `workspace_preferences` | **workspace_preferences** — Persist complete analytical workspace state. | Users should resume tabs, split positions and filter context. |
| `command_registry` | **command_registry** — Searchable keyboard-first application commands. | Apps have several discoverable actions/navigation targets. |
| `application_services` | **application_services** — Canonical bundle of standard application services. | Generated apps need the normal framework services without repetitive setup. |
| `error_service` | **error_service** — Safe user-facing error IDs with structured logging hook. | Technical errors must not become raw user UI. |

## Performance

| Key | Public API / purpose | Use when |
|---|---|---|
| `ttl_cache` | **ttl_cache** — Bounded local TTL/LRU cache. | Deterministic repeated local result is expensive. |
| `single_flight_cache` | **single_flight_cache** — Coalesce identical concurrent async loads. | Multiple components can request the same backend data. |
| `analytical_data_controller` | **analytical_data_controller** — Debounce/cancel/cache analytical loads. | Filters/search can rapidly supersede earlier requests. |
| `lazy_resource` | **lazy_resource** — Load expensive content only on first use. | Tabs/drawers/panels may never be opened. |
| `concurrency_gate` | **concurrency_gate** — Bound async fan-out. | Many independent I/O operations could overload backend. |
| `retry_policy` | **retry_policy** — Controlled bounded retry. | Idempotent reads can fail transiently. |
| `run_blocking` | **run_blocking** — Move blocking work off async loop. | Existing synchronous library must run from async UI path. |
| `performance_monitor` | **performance_monitor** — Bounded latency telemetry and budgets. | Hot path should be measured. |
| `table_query_engine` | **table_query_engine** — Indexed/cached repeated local table queries. | Same in-memory dataset is searched/filter/paged repeatedly. |
| `cached_framework_css` | **cached_framework_css** — Build deterministic framework CSS once per process. | Theme adapter initializes repeatedly across pages/tests. |

## Durable jobs

| Key | Public API / purpose | Use when |
|---|---|---|
| `durable_job_adapter` | **DurableJobAdapter** — Stable long-running-job boundary | multi-minute analysis, restart-survivable work, external scheduler integration |
| `in_process_job_adapter` | **InProcessJobAdapter** — Reference task-backed adapter | development, short jobs, single-process deployments |

## Security

| Key | Public API / purpose | Use when |
|---|---|---|
| `principal` | **principal** — Never infer permissions from display names or UI visibility. | Represent the authenticated user supplied by the company identity layer. |
| `header_auth` | **header_auth** — Trust identity headers only from configured proxy networks. | Identity is asserted by a company reverse proxy/gateway. |
| `access_policy` | **access_policy** — Authorization must execute server-side; hidden buttons are not authorization. | Protect a page, route, action, or data capability. |
| `security_headers` | **security_headers** — Do not invent an untested CSP for NiceGUI; CSP is opt-in until runtime-certified. | Add safe baseline HTTP response headers. |
| `upload_policy` | **upload_policy** — Validate size, extension and media type; active content is rejected by default. | Accept user supplied files. |
| `redaction` | **redaction** — Secrets, authorization headers, cookies, credentials and tokens must be redacted. | Log or diagnose request/configuration context. |
| `correlation_id` | **correlation_id** — Generate server-side by default; trust incoming IDs only at a controlled boundary. | Trace one request/action across logs. |

## Runtime

| Key | Public API / purpose | Use when |
|---|---|---|
| `runtime_config` | **runtime_config** — Use typed RuntimeConfig; do not scatter environment-variable reads throughout apps. | Configure host/port/environment/session/proxy settings. |
| `root_path` | **root_path** — Set one normalized root_path and certify HTTP + websocket + static asset routing together. | Deploy under a reverse-proxy subpath. |
| `proxy_headers` | **proxy_headers** — Enable only with an explicit trusted-proxy allowlist. | Honor forwarded client/protocol information. |
| `health` | **health** — Health is minimal; detailed diagnostics require authorization. | Expose liveness/readiness for operations. |
| `runtime_doctor` | **runtime_doctor** — Treat failed error-severity checks as a release blocker. | Validate a workstation/server before deployment. |
| `compatibility_manifest` | **compatibility_manifest** — Applications do not select their own NiceGUI version. | Record certified versions and deployment assumptions. |

## Visual resources

- `143` packaged semantic icons.
- Use `Icons.*`; do not use emoji or remote icon resources.
- `12` packaged state illustrations.
