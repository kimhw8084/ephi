# G–I. Information architecture, page specifications and investigation experience

## G1. Navigation: three daily destinations, two governed centers

Primary navigation: **Attention**, **Assets**, **Outcomes**. Secondary, permission-filtered: **Family Center**, **Operations**. An episode is a deep-linked workspace opened from attention, an asset, a material search or a notification—not a separate navigation silo. Family Center combines Data/AutoPort and Qualification under one family/capability/release identity. That avoids two competing definitions of “ready.”

Global header: product/environment identity, current authorized site/area, global search, notification inbox, user menu. The product scope is always visible. A family filter is not an authorization control. Switching scope closes or marks incompatible workspace contexts and clears incompatible selections; it does not carry selected lots across unrelated sites.

| Route | Purpose / persistent URL state |
|---|---|
| `/ephi/attention` | view ID, scope alias, owner/state/priority/family filters, sort; cursor can be temporary |
| `/ephi/episodes/{id}` | tab, immutable revision or `latest`, window, selected evidence/group; selection IDs validated |
| `/ephi/assets/{id}` | characteristic/context and window |
| `/ephi/materials/{id}` | approved material drilldown, route/exposure history and linked episodes |
| `/ephi/outcomes` | period, knowledge cutoff, currency, evidence maturity |
| `/ephi/families/{id}` | capability, release, step=data/replay/golden/shadow/qualification |
| `/ephi/operations` | source/worker/family view; safe diagnostic filter |

Links never contain credentials or serialized source datasets. Sharing a link grants no permission. Back navigation restores table filters, scroll, focus and selected row through Base state authorities. Global search exact IDs precede text suggestions; Enter opens the selected item, Escape restores prior focus. Keyboard command palette uses registered Base shortcuts, with conflict detection and disabled behavior in editors.

## G2. Visual grammar and exact token mapping

Use the pinned Base `design/tokens.py`, not a parallel theme. Verified baseline values: shell header 60px, expanded sidebar 256px / compact 64px, page gutter 20px (mobile 16), default content gap 24px, section gap 28px, surface padding 20px, compact table row 38px/header 40px; dense rows 34px; comfortable rows 44px. Default controls 38px, touch target 44px. Typography: page title 26/32, section heading 18/24, body 13/19, data 12/18. Author through semantic tokens/layouts; these numbers are the expected baseline, not inline CSS instructions.

Preserve semantic severity, urgency, freshness and workflow styling. Color never carries meaning alone. Technical severity and operational priority have separate labeled indicators; do not use one unlabeled traffic light to imply both. A confidence indicator means assessment confidence unless calibration explicitly permits probabilistic wording. Null is an em dash plus an accessible reason; 0 is only an observed/derived zero with adequate coverage.

Default desktop density: compact. Dense is user-selected and never shrinks critical controls below the framework target policy. On phone use comfortable touch behavior. Dates show relative time in lists and exact timezone-qualified time on focus/hover/detail. Do not bury essential warnings exclusively in hover text.

Viewport rules use Base breakpoints: phone 600, tablet 900, laptop 1200, desktop 1440, wide 1800. At wide/desktop, episode workspace uses approximately 8:4 main/decision columns in registered panels. Below laptop, collapse the decision column into a prominent next-action strip and move details below the brief. Below tablet, use one content lane and full-width side sheets. Phone keeps the decision brief, urgent exposure and actions first; deep comparison tables remain scrollable/inspectable, not crushed into unreadable charts. Full application canvas occupies the width beside navigation; only long prose uses an inner reading column.

## H1. Attention / Operations Cockpit

Purpose: find the next important piece of engineering work without opening charts. Use a registered monitoring/master-detail pattern, not metric-card wallpaper.

```text
┌ Header: EPHI | environment | scope | Search… | Inbox | User ──────────────┐
│ Nav │ Attention                       [My work] [Team] [All permitted]   │
│     │ P1 needing action · unowned · due soon · source coverage             │
│     │ [Search/filter] [Family] [Priority] [Owner] [State] [Saved view]       │
│     ├─────┬──────────────────┬──────────┬──────────┬────────┬──────────────┤
│     │ Pri │ What / where     │ At risk  │ Deadline │ Owner  │ Work / age   │
│     │     │ Why now + limits │          │          │        │              │
│     │ ... one row per canonical work item; grouped relationships ...      │
│     ├─────────────────────────────────────────────────────────────────────┤
│     │ Preview: decision brief · next check · exposure freshness [Open]     │
└─────┴─────────────────────────────────────────────────────────────────────┘
```

Top metric strip has at most four actionable counters, each applying a visible filter. “Due soon” counts only supported ETA data; unknown deadlines are separate. Source coverage never appears as zero risk. Default sort: operational priority, qualified decision deadline ascending (null last), unresolved ownership/escalation, technical severity, age, canonical ID. Changing this sort changes display only.

Default table columns: priority; asset/context with short issue title; one-line why-now/relevant limitation; preventable exposure with unit; earliest supported decision/exposure deadline with source age; owner; work state; age. Technical severity appears beside the issue label or as a selectable column. Extended fields: family/operation/recipe, hypothesis, confidence band, current/peak severity, quality maturity, check status and data freshness. Base column chooser owns configuration and overflow.

One click selects preview; explicit row link/Enter opens the workspace. Selection remains stable during refresh. Multi-selection supports assignment, export and notification subscription only when permissions and semantics permit; bulk resolution, bulk qualification and bulk manufacturing actions are unavailable. An acknowledgment does not mute future material-risk escalation.

Grouping: related fleet/local episodes and exact duplicates form collapsible groups with deterministic relation labels. Grouping never merges scientific evidence or adds overlapping exposure totals. Unresolved human work remains visible after technical recovery. Archived/closed filters include all dispositions and reopened cycles.

Empty states distinguish no open work, no filter matches, family not onboarded, no source coverage, permission limitation and read failure. “No open work” includes coverage as-of; it is not a claim all equipment is healthy. A stale projection retains last rows with age and a reason. Retry preserves filters and selection.

## H2. Episode workspace — canonical investigation room

The initial screen answers seven questions: what changed, when, confidence/limits, evidence, affected material, urgency and next step. First paint includes a coherent decision brief; supporting panels progressively load without shifting primary actions.

```text
┌ Back to Attention | Asset / operation / recipe | Episode alias | Share ──┐
│ SCIENCE: [state/severity]   WORK: [state]   Owner   [Claim/Assign]         │
│ Decision brief: <change and qualified interpretation>                   │
│ Onset [low—best—high] · confidence/limits · known-at · revision [History] │
│ At risk <classes/units> · preventable deadline or UNKNOWN · source age  │
├──────────────────────────────────────────────┬─────────────────────────┤
│ Understand | Exposure | Investigate | Recovery│ NEXT BEST WORK          │
│                                              │ 1. <eligible check>     │
│ Aligned evidence/event timeline               │ Why this distinguishes  │
│ - signal & matched reference                  │ Prerequisites / effort  │
│ - metrology / independent evidence groups      │ [Start / Record result] │
│ - quality maturity & arrival                  │                         │
│ - maintenance / human actions                 │ 2. alternative check    │
│ - exposure windows & operational deadlines     │ Current blockers        │
│                                              │                         │
│ Support by independent group | contradictions │ Decision/activity trail │
├──────────────────────────────────────────────┴─────────────────────────┤
│ Persistent context: selected window/cohort/revision [Reset exploration] │
└────────────────────────────────────────────────────────────────────────┘
```

Actions: Claim/Assign, Start check, Record observation, Propose containment, Record external action, Start recovery, Submit closure. Show one primary action according to actual state and permission; alternatives are explicit secondary actions, not hidden magic. An urgent exposure banner can place “Review containment” ahead of investigation checks without claiming scientific certainty.

A technically recovered episode with open work reads “Technical signal recovered · Investigation still open.” A resolved workflow with a newly active signal shows “New evidence requires review,” opening a new cycle or a linked recurrence according to the qualified episode policy.

### Understand tab

Decision brief is deterministic from the published DTO. Main timeline uses aligned time axes and separate tracks: measurement/behavior with valid reference; evidence group changes; maintenance/calibration; quality observations and their arrival; material exposure; recorded actions. Onset is a low/best/high interval, not a falsely precise vertical line. Quality pending is visibly pending. Action effective time and the time EPHI learned of it are distinct.

Select a timeline interval to update *exploratory* comparisons and record table using the shared Base context. The authoritative brief and risk calculation remain pinned to their published revision. A persistent “Exploring <window/cohort>” chip and Reset prevents silently redefining an incident. Show decimation/aggregation method and offer bounded raw records.

Support table: hypothesis, independent supporting groups, contradictory groups, limiting data and reviewed status. Expand one group to see correlated detector variants without presenting them as separate confirmations. Selecting evidence opens an InspectorDrawer containing observation, unit, source, event/available time, effect, dependency group, reference population, policy/engine version and source link. Source links remain authorization checked.

### Exposure tab

Three explicit sections: prior exposure, committed/current material, future exposure opportunities. Show definite vs possible past exposure; committed vs likely/possible/preventable future; earliest ETA; qualified alternative status. Material counts and exposure-event counts use different labels. Table columns: material/lot, class, operation/route position, suspect executions, earliest/latest event, confidence limitation, ETA, preventability, alternative qualification, source age.

Select a class to filter records; the count has a visible denominator and union policy. Do not stack overlapping classes into a misleading total. Alternative rows show qualification, compatibility, health and availability separately. “Availability unknown” is not a recommendation that capacity exists.

Containment proposal side sheet prepopulates selected scope and evidence revision, requires reason and intended authorized action, exposes source age, and creates a proposal. An external reference records actual action. No “Hold now” button exists without a separately approved product scope. Export generates a reviewed, scoped artifact with data cutoff and query identity; clipboard exports never include hidden columns without explicit choice.

### Investigate tab

Use hypothesis/check matrix and ranked checks rather than another overview dashboard. Rows are leading alternatives; columns show discriminating evidence/checks; cells indicate supports, contradicts, unresolved, unavailable or not discriminating. All markings have text alternatives. Check selection reveals reason, competing hypotheses, source prerequisites, estimated effort/turnaround, disruption and expected interpretation branches.

History panel initially shows three comparable cases with similarity *and* differences, curation state and outcome knowledge cutoff. Open compare to align context, trajectory, evidence, actions and outcome. Similarity is not causality. RCA panel shows ranked factors, affected/control sizes, matching quality, temporal contradiction and recommended verification. Unsupported controls show a blocked/limited state rather than a colorful high-score plot.

### Recovery tab

Present the locked plan, intervention line, eligible observation counter, required duration, coverage, context match and contradictory evidence. Progress only advances with eligible observations. Insufficient sampling shows “Waiting for qualified observations,” not “recovering 100%.” A repeated signal resets or fails according to the plan. Recovery evidence and closure disposition are independently visible.

Before/after charts use matched units/context and identical axes where comparable. Show sample counts, missingness, concurrent changes and selection criteria. A visual step after an action does not prove that action caused recovery.

Closure side sheet: disposition; confirmed/not-confirmed/unknown behavior; process vs measurement vs data issue; evidence/check refs; action(s); recovery status; residual risks; root-cause confidence; successor obligation; notes. Root cause can remain unknown while recovery is verified. An unavailable outcome creates a pending-value task; it does not force invented value. Reopen preserves the original closure and starts a new cycle.

## I. Complete cognitive sequence

1. Open the brief; identify issue scope, state, known-at and the largest confidence limitation before reading charts.
2. Determine whether a time-sensitive containment decision is warranted. Use real exposure/ETA/capability evidence; otherwise show the missing prerequisite.
3. Inspect the leading explanation plus strongest contradiction. Expand to raw provenance only when it changes the decision.
4. Review the top eligible discriminating check, select or reject with reason, and request/perform it through approved mechanisms.
5. Submit result as structured observation with source/measurement identity. The planner updates; scientific conclusions only update through the qualified pipeline.
6. Compare the most similar curated cases and affected/control evidence; mark a working hypothesis as a human interpretation, not a proven root cause.
7. Record an authorized intervention with actual effective time and external reference. Freeze the decision evidence snapshot.
8. Monitor matched-context affirmative recovery; stale/unreliable data pauses confidence, not alarms away the problem.
9. Close with correct disposition and residual obligations; value remains estimated/observed/pending until validated.
10. At shift handoff, generate a factual brief: what is known, what changed, next owner/check, time-sensitive risk, blockers and last revision. Recipient opens the same canonical work item.

## H3. Asset 360

Use a registered master-detail/analysis pattern. Header: asset identity and qualified context, technical state with age, open engineering work count. Default content: longitudinal episode/action timeline, selected characteristic trend/reference, current capability coverage. Tabs: Episodes, Changes, Measurement/quality, Material context. No composite “health score” invented across incomparable characteristics.

Asset list is server-paginated and filterable by family/site/operation. Episode selection opens the same canonical workspace, preserving origin. Multi-asset compare requires compatible context/units and explicit peer qualification. Missing qualification blocks the comparison, not a silently broadened population. History gap renders as a gap, not an interpolated normal line.

## H4. Outcomes

Default is three adjacent *labeled* summaries: estimated opportunity, observed operational outcome, validated benefit/net cost. Separate counts and currency. Period and knowledge-cutoff selectors are always visible. A restatement banner appears when using later-known revisions for an earlier event period.

Primary table: claim/event, linked episodes, affected material scope, evidence state, attribution role, value/currency, cost model, validator, validation time, latest correction. Drilldown explains exclusions and deduplication. Reviewer action opens evidence and sign-off; cannot edit scientific conclusions. Empty validated totals read “No validated claims in this covered period,” not “EPHI created no value.” Pending outcomes have age and owner. Show negative net value honestly and denominator coverage for time-saving comparisons.

## H5. Family Center (AutoPort + Qualification)

A single persistent header identifies family, capability and target release. A stepper shows Discover/Map → Validate/Data Reality → Replay → Golden → Shadow → Qualify → Promote. Existing stage semantics are adapted, not replaced with this cosmetic sequence.

Mapping table: canonical role, source/table/column, unit, timestamp semantics, confidence/explanation, example sanitized values, status. Ambiguity requires explicit choice; click never auto-promotes a guessed field. Type/units/ID mistakes are blocking. Data Reality shows coverage, duplicates, latency and available capabilities with source evidence.

Replay/golden/shadow stages use a bounded job-status view and immutable artifacts. Golden review compares expected and actual results without future outcomes leaking into simulated decision time. Shadow queue supports claim, review and independent judgments. Gate table shows PASS/FAIL/PENDING/NOT_APPLICABLE with policy basis, evidence hash and expiration. Promote is disabled with precise missing conditions; changing a release or mapping invalidates affected evidence.

## H6. Operations

Use a monitoring pattern: user-visible capability health first, then source latency, failed materializations, queue age, worker failures, release/gate identity and backup status. Manufacturing health never appears in the same status indicator as platform health. A live web process with stale scientific inputs is not “EPHI healthy.”

Source details show last successful coverage and retry state. Worker details show job ID, stage, input snapshot, attempts, lease and redacted reason. Controls: retry eligible job, pause source/family processing, request projection repair, start approved restore rehearsal. Each is audited, permission-scoped and explains impact. Large replay and retention actions require review; no button blindly clears a queue or deletes evidence.

## H7. Universal interaction/state contract

| State | Rendering and allowed behavior |
|---|---|
| Initial loading | Stable-size skeleton for relevant panel; meaningful header appears first; no fake values |
| Refreshing | Last coherent data stays with refreshing indicator; no selection loss or layout jump |
| Empty data | Explain no records versus no matching records; retain filters and offer safe reset |
| Partial | Render available independent panels; label missing capability and dependent disabled decisions |
| Stale | Age and last good cutoff persistent; block only decisions requiring fresh data; allow notes/action recording |
| Insufficient evidence | Show sample/reference limitation and next prerequisite; not healthy, zero or 0% |
| Error | Local panel error where possible, request ID and safe retry; no raw exception |
| Permission denied | No hidden data in client payload, tooltips, export or cached snippets |
| Conflict | Retain draft, show changed fields/revision, require explicit reconcile; never overwrite silently |
| Offline/reconnecting | Read-only last context marked stale, draft preservation, permission/revision recheck before writes |
| Historical snapshot | Prominent temporal-mode/cutoff label; historical workflow and capabilities stay pinned; current activity is separately labeled; no new decision-dependent action against old data without revalidation |

Hover and focus reveal the same supplementary information; essential content is directly visible. Escape closes one overlay and restores triggering focus. Drawers maintain unsaved-change protection. Keyboard navigation uses Base registries and accessible controls; chart selection always has an equivalent table/filter route. Reduced-motion preference is honored. Accessibility target is WCAG 2.2 AA; focus visibility, non-drag alternatives and target sizing are explicitly tested [U1]. This is a target, not a claim of achieved conformance.

## H8. Browser acceptance scenarios

At 1920×1080 and 1440×900: issue, scope, limitation, urgency and primary action visible without horizontal page overflow. At 1280×800: condensed navigation and single/dual panel layout remain actionable. At 390×844 and 200% zoom: primary workflow usable, no clipped overlays/focus; dense grids expose an accessible horizontal/record-detail alternative. Test light/dark themes and long identifiers, nulls, large magnitudes, unknown deadlines, 50/100-row pages, 30 families, contradictory evidence, concurrent reassignment, disconnection and zero authorized results.

Measure tasks, not visual enthusiasm: finding the highest-priority owned work, identifying leading contradiction, finding exposed material, completing a check, recording an action, understanding blocked recovery, reopening a closure and validating a claim. Record completion, errors, navigation count and active effort relative to the agreed baseline. Human engineers must review screenshots and actual interactions; generated mockups are not acceptance evidence.

[U1] W3C WCAG 2.2 Recommendation, `https://www.w3.org/TR/WCAG22/`, accessed 2026-09-15.
