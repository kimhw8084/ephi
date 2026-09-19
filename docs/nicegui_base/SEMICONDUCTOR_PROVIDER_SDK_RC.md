# Semiconductor Provider SDK & Release-Candidate Workflow — Wave 65

Wave 65 operationalizes the existing Wave 59–64 authorities. It does **not** add a second data/query/runtime system.

## Provider SDK

Implement `SemiconductorProviderAdapterBase.build_source()` and return the existing Wave 59 `DataSource`. Keep database, credentials, proxy, authentication and company topology inside the adapter/application configuration. Do not put provider-specific SQL or API calls in page code.

```python
from nicegui_base import ProviderAdapterManifest, SemiconductorProviderAdapterBase

class CompanyAdapter(SemiconductorProviderAdapterBase):
    manifest = ProviderAdapterManifest('company-provider', 'Company Provider')

    async def build_source(self, recipe):
        return build_approved_source(recipe)
```

Create a neutral starter with:

```bash
nicegui-base provider-init ./provider --key company-provider --recipe spc-monitor
```

Run bounded conformance with:

```bash
nicegui-base provider-check package.adapter:CompanyAdapter --recipe spc-monitor --profile production --format json
```

`provider-check` trusts neither capability labels nor provider claims. Production policy verifies observed `QueryStats.pushdown` while every probe remains bounded.

## Conformance fixtures and diagnostics

`ProviderConformanceFixture` captures the recipe, profile and reviewed field overrides. Use `development` only for bounded local fixtures. Use `production` for a release candidate. `provider_report_guidance()` converts conformance findings into remediation guidance without suppressing errors.

A provider may not receive a production PASS merely because it advertises pushdown. Filter, pagination and projection pushdown must be observed where the policy requires them.

## Guided application setup

`SemiconductorRecipeRuntime.prepare_setup_workflow()` composes one setup model from:

1. provider/source health,
2. semantic field binding,
3. recipe-specific operational guardrails,
4. bounded runtime readiness,
5. provider conformance,
6. representative performance benchmark,
7. target environment certification.

`ready_to_run` and `ready_for_release` are intentionally different. Engineers may use a healthy, correctly mapped application while release-only browser/provider/human evidence remains pending. `SemiconductorSetupWizard` renders the same model across all eight recipe applications.

## Operational guardrails

`RecipeConfigurationReview` adds recipe-specific safety checks without changing the underlying recipe or `AnalysisContext`. Examples include affected/control population requirements for excursion/RCA workflows, wafer coordinate requirements for spatial workflows, sensor/time identity for FDC, and reviewed context recommendations for SPC/chamber matching.

Warnings do not silently become failures, and failures are not downgraded to keep a workflow moving.

## Governed performance profiles

`development-smoke` is intentionally small and cannot be presented as representative fab-scale evidence.

`provider-rc` repeats only bounded Wave 64 performance probes, requires observed backend pushdown, and requires at least 10,000 matching rows to demonstrate representative scale. The profile records per-operation latency samples, median, p95, maximum latency, pushdown rate and bounded row counts. It never loads the complete source.

## Target evidence bundle

`build_semiconductor_target_evidence_bundle()` packages provider conformance, performance, benchmark results, runtime fingerprint, optional hashed artifacts, and target gate statuses into portable JSON. Missing installed NiceGUI, real server/WebSocket, supported browser or human visual-baseline execution stays `PENDING`.

Generated semiconductor applications include `services/release_evidence.py` so target teams can write the evidence JSON without modifying framework internals.

## Release-candidate sequence

1. Implement approved provider behind `DataSource`.
2. Resolve ambiguous semantic mappings explicitly.
3. Review recipe operational guardrails.
4. Run bounded production provider conformance.
5. Run `provider-rc` benchmark on representative target data.
6. Execute installed NiceGUI/server/browser gates in the target environment.
7. Record explicit human visual-baseline approval.
8. Export the target evidence bundle.
9. Promote only when every required gate is `PASS`.

Historical browser artifacts are evidence for their historical source only and must not be reused as a current-source PASS.
