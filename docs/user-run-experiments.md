# Experiments run by your own code

Your agent, your model calls and your tools run on **your** computers. Functional AI keeps the dataset, the Score Set and the
results: it hands you the rows, takes back what your code produced, and scores it with the same rule checks and AI judges as a
hosted experiment. We call no model except the AI judges.

Results, pass rates, findings, row-by-row explanations, baselines and the cheaper-model suggestion all work as usual.

## The short way: one call from the SDK

You don't have to make the three calls yourself. The SDKs do all of it: take a snapshot, create the experiment, run each row
through your function, record its steps, send every result, finish, and return the summary.

```python
result = fnai.eval("support_triage", dataset="tickets", task=my_agent, score_set=4, concurrency=4)   # Python
```
```ts
const r = await evaluate("support_triage", { dataset: "tickets", task: myAgent, scoreSet: 4 });         // TypeScript
```
```go
res, err := c.Eval.Run(5, func(in map[string]any, rec *fnai.EvalRecorder) (any, error) { … }, fnai.EvalOptions{Dataset: "tickets", ScoreSet: 4})
```

A task that raises (or returns an error) fails that row only; the run goes on. If a run stops part-way, call it again with
`resume` (Go: `Resume`) set to the experiment id and rows already sent are skipped. From the command line:
`fn eval 5 --task my_agent:run --dataset tickets --score-set 4 --min-pass-rate 0.9` (Python `fn`: `module:function`; Go `fn`: a command
that reads the row's JSON on stdin and prints the answer JSON). It exits nonzero when the pass rate is below the threshold.

**Failed rows (`fn eval`, `fnai.eval`, Go `Eval.Run`).** After a run you get the first ten failed rows: the row's number, the check, the row's input, and the check's own explanation, then a link to the run. `fn eval … --json` prints only JSON, so a coding assistant can read it, fix the row and run it again:

```
{"experiment_id": 9, "passed": false, "checks": [...], "failed_total": 1,
 "failed_rows": [{"row": 2, "check": "supplier is right", "model": "my-agent", "input": {"document": "invoice 2"},
                  "why": "supplier was 'Wrong Co', expected 'Acme'", "trace_id": "…"}],
 "url": "https://…/fn/5?panel=experiments"}
```

(Long inputs are cut at 400 characters; the link has the rest. Go: `res.FailedRows`, `res.FailedTotal`.)

## The three calls

All three use your normal sign-in (a personal access token, `fnp_…`) and need the `experiments.run` permission.

**1. Create it** (once). Pick a dataset version and a Score Set; there is no model to choose.

```
POST /api/functions/{function_id}/experiments
{ "mode": "user_run", "version_ids": [], "snapshot_id": 12, "plan_id": 4, "name": "nightly check", "repeats": 1 }
```

Send `"version_ids": []` (the field must be present): an empty list means the function's newest version, and the results are linked to it. Or name one. The experiment is created
**waiting for results**: nothing runs on our side.

**2. Get the rows**, run each one yourself, and **send each result back**.

```
GET /api/experiments/{id}/user-run/rows
→ { "total_rows": 3, "repeats": 1, "rows": [{"row": 1, "input": {...}}, ...], "received": [{"row": 1, "repeat": 1}] }

PUT /api/experiments/{id}/user-run/rows/{row}          (row is 1-based)
{ "output": {...}, "steps": [{"type": "llm", "model": "gpt-4o-mini", "tokens_in": 120, "tokens_out": 9},
                              {"type": "tool", "tool": "search", "args": {...}, "ok": true, "output": {...}}],
  "model": "my-agent", "tokens_in": 120, "tokens_out": 9, "cost": 0.0002, "latency_ms": 840,
  "error": null, "repeat": 1 }
```

- Only the inputs are given out, never the expected answers.
- Sending a row again **replaces** it; it never doubles. `received` in the GET lists what already arrived, so a retrying
  sender can skip those.
- `steps` use the trace step format. Behaviour checks (which tool was called, in what order, with what arguments, approval
  before an action) score from these steps. Values the function marks sensitive are scrubbed from the steps before they are stored.
- `model` is the name your code used; results are grouped by it. `cost` is what you report (it feeds the cheaper-model suggestion).
- An `error` (or `"success": false`) records a row that failed on your side; it is scored as a failed run, like a hosted one.

**3. Finish.** We score everything that arrived.

```
POST /api/experiments/{id}/user-run/finish
```

Rows you never sent show as **not run**: they are not in the pass rate and are not failures. Finishing twice is harmless. A
finished experiment cannot take more results; clone it to run again.

## In the app

The experiment shows **Run by your own code** while it waits (with how many results have arrived), then the usual results,
with a line saying how many rows came back and which were not run.
