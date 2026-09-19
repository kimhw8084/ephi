# Semiconductor target certification — agent guide

When asked to certify, qualify, operationalize, or promote a semiconductor recipe application:

1. Keep Wave 59 `DataSource`, Wave 60 `AnalysisContext`/`SelectionBus`, Wave 62 recipe, and Wave 63 runtime as the authorities.
2. Run Wave 64/65 provider conformance and representative bounded benchmark on the approved target provider.
3. Build a Wave 65 target-evidence bundle from real target executions. Never mark unavailable installed NiceGUI/server/browser/human evidence as PASS.
4. Aggregate evidence with `build_provider_qualification_pack(...)`.
5. Assess operational readiness with `assess_semiconductor_operational_readiness(...)` and use the recipe runbook.
6. Evaluate stable promotion with `build_semiconductor_promotion_decision(...)`.
7. Require the evidence bundle's framework/runtime identity to match the current release authority. Legacy evidence without identity is PENDING; framework/runtime mismatch or an installed-NiceGUI PASS against the wrong version is BLOCKED.
8. Treat stale, missing, or untraced evidence as PENDING; hash mismatch, failed gates, environment mismatch, or provider/source mismatch are BLOCKED.

Do not bypass target gates with source tests, historical browser artifacts, waivers, or development fixtures.

CLI handoff: `nicegui-base target-qualify <evidence.json> --provider <provider> --operational-readiness <readiness.json>`. A PENDING decision returns a distinct non-zero status and is not reported as PASS.
