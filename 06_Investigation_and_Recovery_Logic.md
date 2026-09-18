# I. Investigation planner, recovery and high-leverage product mechanisms

## 1. Innovation with clear authority

The differentiator is not a new chart library or a free-form AI agent. It is a **decision workspace that remembers what was known, shows what remains uncertain, and selects the least-wasteful next check**. Four product mechanisms deliver this:

- A decision brief pinned to a reproducible revision, with a separate latest-known view.
- An evidence/alternative matrix that makes contradictions and missing discriminating checks visible.
- A next-check planner that considers available capability, prior work, deadlines and disruption.
- Independent technical recovery, human work closure and value maturity so one status cannot hide the others.

These are proposed EPHI product features, not claims that all currently exist.

## 2. Curated check contract

`CheckTemplate` fields: template_id/version, title, family applicability, target kind, required capabilities and freshness limits, supported contexts/units, candidate hypothesis pairs, discrimination rubric, prerequisites, mutually redundant checks, estimated active effort/turnaround source, disruption class, approval capability, execution mode, result schema, interpretation branches, evidence-quality requirements, qualification artifact ID.

Execution modes: `READ_EXISTING`, `ASYNC_COMPUTE`, `REQUEST_HUMAN_MEASUREMENT`, `REQUEST_APPROVED_EXTERNAL_WORK`. A button cannot transform one mode into another. The first two can be low-friction when authorized; measurement/intervention requires the appropriate operator procedure. Result outcomes: supports A, supports B, inconsistent, inconclusive, failed/unavailable—with supporting measurements/references rather than the label alone.

Initial templates preserve and enrich existing `_recommendations()` in `advisory/read_models.py`: reference-standard check; same material/characteristic on a qualified peer; matched self/peer physical feature review; maintenance/calibration/change review; shared recipe/regime comparison; source schema/unit/completeness verification; route-matched upstream control comparison.

Each template has a small explicit allowed action vocabulary. No generated instruction can set a machine, alter a process recipe or prescribe manufacturing operating parameters. Company procedures supply safe execution details.

## 3. Deterministic planner algorithm for V1

Inputs are an immutable analysis revision, hypothesis support/contradiction and dependence groups, availability/qualification, evidence age, completed checks/results, action history, exposure/decision deadline and curated templates. Outputs are ranked eligible checks plus excluded-check reasons and a policy/evidence hash.

**Step A — guard and form alternatives.** Validate scope, reference and time. Collect the leading hypothesis and plausible alternatives with supporting evidence or unresolved contradictions. Preserve “insufficient information.” Do not treat the existing net scores as posterior probabilities. Include an unresolved high-consequence alternative through a documented rule rather than requiring it to be the highest score.

**Step B — enumerate eligible checks.** Check family/context/units, availability, freshness, operator authority, sample availability, cost/risk constraints and qualification. A completed check is reusable only for the same target/context and still-valid evidence window. Failed/unknown checks are not counted as completed evidence. Explain every exclusion (“peer not qualified,” “no reference sample,” “already answered at this revision,” “result would arrive after decision window”).

**Step C — score discrimination without fake probability.** For each unresolved hypothesis pair `(i,j)`, a curated template supplies `d(c,i,j)` in {0, 0.5, 1}, denoting no/partial/strong expected discrimination. Pair importance `w(i,j)` comes from reviewed ordinal policy and consequence constraints, normalized only as ranking weights. It is not a causal probability.

`D(c) = sum(w(i,j) * d(c,i,j)) / sum(w(i,j))`, if any unresolved pair exists. Otherwise choose a prerequisite or validation check, not a fabricated discrimination score.

Calculate independent coverage `N(c)` for currently unresolved evidence groups; completion feasibility `F(c)` from capability/readiness and known turnaround; redundancy `R(c)` with already selected/recent checks; effort/disruption `E(c)` from approved ordinal bands. Unknown effort/time is visibly unknown and receives a conservative band, not zero cost.

Proposed versioned ranking utility:

`U(c) = 0.45*D(c) + 0.20*N(c) + 0.20*F(c) - 0.10*E(c) - 0.15*R(c)`.

All terms are bounded [0,1]. Weights are an initial design default to qualify, not scientifically validated coefficients. Preventability/urgency does not change the hypothesis support score. A separately displayed containment prerequisite may outrank investigative convenience through a hard priority rule.

**Step D — select a diverse short plan.** Greedily select at most three checks, updating redundancy and covered hypothesis pairs after each choice. Prefer Pareto-nondominated checks when equally discriminating (less effort, delay and disruption). Tie break by lower disruption, faster supported turnaround, template ID. Retain alternative candidates and explanations. Never recommend three versions of the same correlated metric as independent confirmation.

**Step E — present reason trace.** “This check distinguishes measurement bias from shared process change, uses a qualified peer, adds an independent measurement, and has not yet been completed.” Also show what it will not resolve. Store reason codes and the inputs behind the explanation; optionally an LLM paraphrases only this record.

**Step F — replan safely.** New evidence/result/capability changes create a new plan revision. Do not cancel a started human measurement silently. Mark obsolete suggestions and ask to retain/cancel through a command. Replanning is idempotent for the same input hashes and never changes the outcome of a completed check.

## 4. Calibrated information gain — later, conditional

Only when there are independently reviewed outcome labels, representative validation data, and supported probabilities for check outcomes under competing hypotheses should a planner claim expected information gain:

`EIG(c) = H(Hypotheses | current evidence) - sum_y P(y | c,evidence) H(Hypotheses | evidence,c,y)`.

Then test calibration, temporal leakage, selection bias and performance by family/context. Information gain remains separate from action value and operational disruption. No softmax over existing support scores earns the right to call them posterior probabilities. V1 does not depend on this research step and is fully useful without it.

## 5. Technical recovery correction and operational plan

Fix F05 inside the qualified episode/recovery contract. Every candidate recovery observation must pass: correct context/characteristic/units; observation available at cutoff; eligible healthy/normal measurement evidence; required confidence/reference quality; no disqualifying pipeline/schema/measurement-integrity issue; independent sampling/spacing requirements. `severity <= OBSERVE` alone is insufficient.

Introduce `RecoveryEligibility` with `eligible`, reason codes, reference/capability IDs and validated criterion results. For behavioral and metrology pipelines, the scientific recovery evaluator determines eligibility from qualified inputs. An application UI must not decide that low severity is healthy. Default legacy behavior stays available only for replay comparison; release uses the corrected policy/version with regression evidence.

CHG-118 adds only the bounded `ephi.recovery` W0 API and in-memory service. Its deterministic regression policy uses a confidence floor, three independent sampling identities, exact context/characteristic/unit, available-at cutoff, freshness, valid reference/capability and an affirmative normal outcome. Five zero-confidence `OBSERVE` pipeline-suspect assessments therefore remain `ACTIVE`; a qualified contradictory observation deterministically clears progress and resets the episode to `ACTIVE`. These values are regression fixtures, not family production thresholds.

Operational `RecoveryPlan` defines scope, prior intervention, matched reference/cohort, acceptable deviation rule, confidence floor, minimum eligible sample count, minimum elapsed operating duration, maximum gap, required independent channels and failure/reset rules. Family-specific numerical thresholds must come from approved scientific configuration and data—not this generic design. Proposed scaffolding defaults can be marked unqualified, but cannot authorize real closure.

States: `NOT_STARTED`, `WAITING_FOR_DATA`, `MONITORING`, `PASS`, `FAIL`, `INVALIDATED`, `EXCEPTION_REVIEW`. New valid observation increments eligible evidence; a contradictory qualified observation resets/fails according to policy; stale/missing/pipeline-suspect input pauses or invalidates confidence. Repeated observations of the same wafer/reference run cannot inflate independent sample count.

Human closure for a confirmed true issue requires a PASS plan or an explicitly reviewed exception with residual-risk owner. It can still state “root cause unknown.” Benign, duplicate and data-issue dispositions follow their own evidence requirements. UNRESOLVED requires an outstanding obligation/owner or explicit accepted residual risk; it must not become a hidden wastebasket.

## 6. Attention logic beyond a priority score

A work item is open when an engineering lifecycle is open, a required check/action/recovery obligation is open, or a new material-risk change requires acknowledgment. Technical active state alone is neither necessary nor sufficient. Detected recovery does not auto-close the human task (F03).

Prioritize using the existing P1–P4 operational policy and an explained secondary order; do not change science. Deadline calculations must distinguish expected tool arrival from latest feasible decision time. If action turnaround is known, `decision_deadline = earliest_eligible_exposure_eta - supported_action_turnaround`; otherwise display ETA without inventing a decision deadline. Countdown stops being authoritative when source age exceeds policy.

Group notifications and related incidents by canonical episode/fleet relation, not by fuzzy title. A quieted work item stays discoverable. Escalation occurs on higher priority, a new supported exposure deadline, overdue ownership, failed recovery or material source degradation. A single lead seeing many alerts is not a substitute for measuring alert load per engineer-shift.

## 7. Historical intelligence and RCA

V1 uses existing structured fingerprints and exact similarity over a bounded qualified candidate population. Filter by family/context and available-at before ranking; precompute result sets. Always show known differences, data completeness and curation. Prior root cause/action success requires its own evidence/curation level. “Similar trajectory” is not “same root cause.”

RCA uses affected versus properly eligible controls, not future outcomes selected because they match the desired answer. Record cohort selection, exclusions, sample size, timing, units and matching limitations. Company route/maintenance evidence can enrich the candidate, but commonality and relative risk remain observational. Invalid controls disable the causal-looking summary and suggest the missing discriminating check.

Upgrade retrieval when measured latency/recall requires it: indexed metadata first; optionally semantic retrieval of reviewed narratives later; final candidates remain exact structured/scoped reranking. Do not put a vector database on the V1 critical path.

## 8. Planner and recovery qualification

Golden scenarios: measurement drift vs true process shift; common mode; recipe change; data schema/units change; sparse reference; contradictory peer; already completed check; unavailable peer; expired result; after-deadline check; corrupted/late source; simultaneous maintenance; repeated samples; same material across two episodes; recovered signal with open human work.

Measure check eligibility correctness, useful discrimination ranking, recommendation redundancy, engineer acceptance/rejection reasons, active effort and time to a defensible decision. A high acceptance rate alone can reflect uncritical trust; review correctness. Recovery tests must prove that missing/stale/low-confidence/pipeline-suspect observations cannot produce PASS, including the exact F05 fixture.
