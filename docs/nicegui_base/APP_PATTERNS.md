# Application Pattern Decision Guide

| Requirement signal | Pattern | Typical anatomy |
|---|---|---|
| KPIs + trend overview | `DashboardPage` | Header → optional filters → metrics → primary/secondary visuals → summary data |
| Filters + analytics + records | `DataExplorerPage` | Header → filters → metrics → charts → DataTable → DetailDrawer |
| Browse list and inspect entity | `MasterDetailPage` | Header → master data → selected detail → actions |
| Manage records | `CrudPage` | Header → filters/search → DataTable → create/edit drawer/page |
| Operational health/live status | `MonitoringPage` | Header/status → alerts → metrics → trends → affected records |
| Search heterogeneous entities | `SearchPage` | Header → facets/filtering → results → contextual preview |
| App/user configuration | `SettingsPage` | Header → local settings navigation → form content → actions |
| Guided multi-step task | `WizardPage` | Header → progress/navigation → constrained content → safe actions |
| Baseline/current comparison | `ComparisonPage` | Header → filters → comparative metrics/visuals → delta/evidence |
| Maximum-density analysis | `AnalysisWorkspacePage` | Compact header/filtering → resizable primary workspace → optional inspector |

If two patterns seem plausible, choose the pattern that best matches the **user's dominant task**, not the one that merely resembles the requested widgets.
