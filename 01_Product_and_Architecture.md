# A. Product architecture and requirement baseline

## 1. Product definition

EPHI is an engineering decision system for detecting equipment/process or measurement-system degradation, explaining the evidence, identifying time-sensitive material exposure, completing an investigation and verifying the outcome. Its value is not the number of charts or alerts. Its value is fewer avoidable exposures, faster correct decisions, less manual reconstruction and demonstrable benefit.

The permanent loop is:

**Detect → Explain → Prioritize → Contain → Investigate → Act → Verify recovery → Learn → Prove value.**

“Contain” means a human-authorized decision and an observed action in the relevant operational system. It does not mean EPHI automatically holds tools or reroutes material.

## 2. One-page design

```text
 COMPANY SYSTEMS (read-only integrations; no autonomous production commands)
 telemetry / metrology / executions / WIP / routes / quality / maintenance
                              |
                 approved adapters + AutoPort
                 explicit units, semantics, available_at
                              |
              bounded source snapshots + capability status
                              |
            DURABLE WORKERS — same EPHI deployment artifact
     detect → fuse → episode → exposure → RCA/history → publish
                              |
            +-----------------+--------------------------+
            | Shared transactional state                 |
            | analytical revisions + coherent read heads |
            | workflow / checks / actions / recovery     |
            | qualification / jobs / outbox / value      |
            +-----------------+--------------------------+
                              |             |
                    immutable artifacts     audit history
                              |
             EPHI APPLICATION QUERIES AND COMMANDS
             identity/scope · CAS · validation · receipts
                  /           |             \
       NiceGUI Base UI     FastAPI /v2      CLI / notifications
                  |
       +----------+-----------------------------------------+
       | ATTENTION → EPISODE DECISION WORKSPACE              |
       | What changed? | Why? | At risk? | Next check/action? |
       | Decision brief → evidence → action → recovery       |
       +----------------------------------------------------+

 CONTROL PLANE: mapping → replay → golden cases → shadow → qualify → promote
 Every product conclusion: known-at / revision / freshness / limits / provenance.
 Science, operational urgency and engineering workflow are separate authorities.
```

The shared state box is not permission for every module to write every table. Each aggregate has one writer and published contracts. The diagram shows runtime flow, not import dependencies.

## 3. Users and jobs

| User | Primary job | Default view | Authorized changes |
|---|---|---|---|
| Equipment/process engineer | Find and discriminate a real local issue | My attention / episode | Claim, checks, observations, action records, proposed closure |
| Manufacturing engineer | Decide what material needs attention before exposure | Attention sorted by preventable deadline / exposure | Containment proposal, external action reference, ownership |
| Metrology/quality engineer | Distinguish measurement-system change from process/material change | Evidence, matched comparisons, quality maturity | Verification results, reviewed interpretation |
| Engineering lead | Resolve unowned/aging work and coordinate shift handoff | Team attention / asset history | Assign, escalate, approve defined exceptions |
| Qualification owner | Establish what is safe and supported for each family | Family center | Mapping review, gates, replay/shadow review |
| Value reviewer | Validate attribution and non-overlapping benefit | Value/outcomes | Approve/reject a claim; no scientific edits |
| Platform operator | Restore trustworthy service | Operations | Worker/source controls, recovery operations; no implied scientific authority |

Job capabilities are assigned by company identity and scope; a role label alone does not authorize cross-site access.

## 4. Requirements in ROI order

Ranks below express design priorities, not measured ROI. Source readiness and prerequisites change implementation order.

| ID | Required outcome | Measurable acceptance / benefit metric | Delivery |
|---|---|---|---|
| R1 | Prevent avoidable exposure | Time to first feasible decision; distinct material at risk; observed protected material; deadlines with source age | V1 |
| R2 | High-signal attention | No silent loss of open work; actionable precision; unowned age; time to first correct action | V1 |
| R3 | Canonical investigation workspace | Engineer identifies change, onset, evidence, uncertainty and exposure without reconstructing separate dashboards | V1 |
| R4 | Context-aware next checks | Every suggestion names hypotheses discriminated, prerequisites, cost/time and reason; unavailable checks never appear executable | V1 deterministic; calibrated optimization later |
| R5 | Action and recovery closure | Actions persisted with evidence; recovery is affirmative valid evidence, not missing alerts; closure and reopen fully audited | V1 |
| R6 | Historical/RCA leverage | Comparable prior cases expose differences and curation; route associations never mislabeled as proven causes | Basic V1; deeper search later |
| R7 | Trustworthy outcomes/value | No estimated benefit labeled realized; no double-counted event/material; correct historical as-of summaries | V1, independent review can remain pending |
| R8 | Repeatable family onboarding | Second family via mappings/config/qualification unless a documented domain gap exists | V1 architecture and first workflow; second-family gate before “scalable” claim |
| T1 | Temporal and scientific integrity | Point-in-time replay, unit/context checks, no future evidence, preserved dependency groups | All waves |
| T2 | Durable secure collaboration | Authorized scoped commands; atomic audit/receipt; crash/retry correctness | Before company pilot |
| T3 | Fast usable truthful UI | Quantified browser/service targets; all degraded states; accessible dense layouts | Before pilot |
| T4 | Controlled qualification | Release identity + family/capability + current evidence; missing evidence blocks promotion | All deployment stages |

## 5. ROI measurement, without fabricated savings

Measure three distinct outputs: **opportunity**, **observed operational outcome**, **validated economic benefit**. A saved click is not a saved engineer hour; a routed wafer is not automatically avoided scrap; a detected metrology shift does not prove defective material.

For a common review period and currency:

`net_validated_value = sum(non-overlapping active validated claim groups) - eligible observed operating costs`

`benefit_cost_ratio = validated_benefit / eligible_cost`, only when cost is positive. A zero denominator is “not defined,” not infinity.

`net_engineering_hours_saved = sum(matched_baseline_minutes - observed_active_minutes)/60` for reviewed, comparable completed investigations. Preserve signed differences so slower cases reduce the reported benefit. Gross positive time savings and extra effort may also be shown separately, but neither replaces the signed net result. Idle browser time is not active effort. Monetization requires an approved rate and cannot be added again when already contained in a validated claim.

A proposed opportunity calculation may use `material_count × calibrated risk × approved unit loss`; without a supported risk estimate, show count and scenario range, not a false expected-dollar amount. Keep risk assumptions visible and versioned.

Establish an observation baseline before enabling the pilot: incident volume, manual investigation effort, acknowledgment and containment times, false-alert effort, remeasurement/rework and current reporting burden. Compare matched families/contexts and report data coverage, sample size and censoring. An improvement after launch is association unless attribution has been reviewed.

An adoption decision should prefer positive conservative net value and acceptable safety/workload over a large speculative headline. A fixed savings target cannot be derived from the uploaded code.

## 6. First flagship and V1 cut

**Default: measurement-system health for one metrology family.** It is supported by `metrology/engine.py`, the shipped metrology advisory fixture, metrology replay, reference/matching/repeatability/spatial channels and existing campaign machinery. This is code maturity evidence, not proof of that family's business ROI.

Select the first family that has: trustworthy identifiers/timestamps/units, a reference or valid comparable population, an accountable engineer, a feasible verification action, and enough history for qualification. Prefer one with executions/WIP and outcome evidence so R1/R7 can be measured. No source can substitute for another merely to complete a screen.

Minimum flagship scenario: distinguish head/tool measurement drift from a real material/process shift; identify measurements/material requiring review; compare a reference or same material on a qualified peer; record the authorized intervention; verify matched-context recovery; capture reviewed effort and outcome. WIP protection remains unavailable until WIP/routing data is qualified.

V1 needs one polished Attention surface, one complete Episode workspace, an Asset history drilldown, a usable Family center, basic Value and Operations views. It does not need seven independent fully featured workspaces.

## 7. Permanent invariants

I01 Event time, observation availability and publication/knowledge time are distinct.
I02 An as-known decision only uses observations available at that knowledge cutoff.
I03 Technical state cannot be repriced into a different scientific conclusion.
I04 UI, notifications and operational prioritization cannot run a second detector.
I05 Correlated evidence remains grouped; confidence/support is not automatically probability.
I06 Missing, stale, ambiguous or unit-invalid data cannot create affirmative health/recovery.
I07 Human actions do not retroactively rewrite analytical evidence.
I08 Promotion is specific to release, family, capability and evidence identity.
I09 Company physical schemas remain in adapters/mappings, outside generic EPHI.
I10 Every consequential write is scoped, version checked, durable and audited.
I11 Opportunity, observation and validated value remain separate with no duplicate attribution.
I12 Technical recovery and engineering closure are independent; neither silently hides the other's work.

## 8. Non-goals

No new generic detector collection before the first closed loop; no autonomous holds/routing; no chatbot as primary UI; no new dashboard builder; no uncontrolled online learning; no microservice estate; no live warehouse scans on page open; no duplicate NiceGUI Base runtime/state/catalog; no implied production certification from unit tests.
