# Live scoring — is the quality holding?

Experiments score a fixed set of test questions once. **Live scoring** keeps scoring real traffic after you ship: a share of the
function's NEW live calls is marked with a Score Set, over and over, so a drop shows up.

## Turn it on

Function → **Monitor** → **Live scoring** → **Turn on**, then pick a **Score Set** and a share of calls (1–100%). With no Score
Set chosen it behaves as before (a judge reviews the function's judge scores once a day). It only scores calls that arrive after
you attach a Score Set — never the backlog.

API: `PATCH /api/functions/{id} {"review_enabled": true, "review_sample_pct": 20, "live_plan_id": <Score Set id>}` (`0` clears it).

## What is scored

- **Calls made through Functional AI** and **traces your own code reports** (`POST /v1/traces`, the SDK's `trace()`), including
  ones that name no prompt version.
- **Which ones:** a fixed fingerprint of each trace's id decides, so a 20% share is about one call in five, the same call is never
  scored twice, and the choice is repeatable. A job runs about **hourly**, up to 500 new calls per run (the rest wait).
- **How:** every check in the Score Set. **Rule checks are free.** **Behaviour checks** read the steps your code reported (so a
  declared tool's "approval first" / "at most 2 calls" rules work on live traffic). **AI judges** are billed as for any review:
  your own key, or Functional AI's key with credits. An account out of credits still gets its free checks.

## What you see (Monitor → Live scoring)

- A **warning banner** when a check's pass rate over the **last 24 hours** is at least **10 points below** the 7 days before it
  (needs at least 20 scored verdicts in both periods): "Pass rate for 'approval_before:issue_refund' dropped from 90% to 70%…".
  It is worked out when the page loads.
- A **line chart** of the pass rate per day for each check (a trend needs two days of results).
- The pass rate per check and version, and the latest failures with the reason.

Results are kept for 30 days. In a **no-runtime** organization this still runs: rule checks make no model call, and judges are the
one thing that still runs on our side.
