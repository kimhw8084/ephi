# EPHI CHG-134 R3 browser qualification report

Status: `VERIFY / BLOCKED_PRODUCT_DEFECT`

This report is bound to Project OS request `ephi-o3-attention-episode-w1-verify1`, Fabric job `CF-7cf9a06942d8b293fcaa7f4d`, candidate source `ef959f577bc1aa223b260ac32cb42a18eadfaf9f`, candidate tree `3755ca31a2d9ba5b8e473fa67ab1338b00623055`, base `cdf8bb54eeb42926029a9394814d79d17a282bd9`, and branch `codex/ephi-o3-attention-episode-w1-verify1`.

## Executed results

Desktop `1440x900` CSS pixels / DPR 1 and mobile `390x844` CSS pixels / DPR 1 were executed with bundled Playwright Chromium against the real NiceGUI Base application and explicit development PostgreSQL/identity bindings. Attention, coherent Episode brief selection, durable Claim/Acknowledge, restart/reconnect persistence, two-process stale conflict, current authorization revocation, degraded source display, bounded console behavior, keyboard focus and overflow checks are recorded in `browser-evidence.json`.

The decisive defect is S08: after PostgreSQL was stopped and then restored, the required UI Refresh did not recover the source. The page remained `Stale data · 56 records`; the server logged `psycopg.errors.AdminShutdown` followed by `psycopg.OperationalError: the connection is closed` from `PostgreSQLAttentionStore.fetch_attention_rows`. The application holds a long-lived psycopg connection and does not re-establish it after source restart. This is a real product/runtime defect in the exact candidate and must be handled by a separate FIX run.

S07 is retained as an executed degraded-state PASS: source loss did not render `HEALTHY`, empty-success or numeric zero. S05 and S06 are expected conflict/denial states, not failures. S09 is `NOT_RUN_AFTER_DEFECT`; no GO or audit acceptance is claimed.

## Evidence carrier

This carrier is intended for independent Project OS retrieval only. It is a dedicated `project-os-artifacts/ephi/ephi-o3-attention-episode-w1-verify1` Git artifact ref, not product `main`, not the verify branch, and not a Notion/AR-2 sidecar. The outer `git-publish-receipt.json` records the remote commit/tree, immutable carrier inventory and hashes after publication. Publication does not change the candidate checkout.

## Source integrity

The verify worktree was restored to clean after removing only the untracked `.nicegui/` runtime storage directory created by the local Base server. No tracked candidate/source bytes were modified, no candidate commit was amended/rebased/squashed, and `git diff --check` passed.
