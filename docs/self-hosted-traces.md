# Self-hosted traces

A function you run on your own servers can report each call to us, so it appears in the monitor next
to hosted calls. Nothing runs on our side for these calls: we store what you send, hide what the
function marks as sensitive, and show it.

## Send a trace

`POST /v1/traces` with the function's own `fnk_` key (the same one used for `/v1/run`), or with a space key (`fns_…`, **Send traces**) that names the function in a `"function": "<slug>"` field of the body ([keys.md](keys.md)):

```bash
curl -X POST https://uat.fnai.dev/v1/traces \
  -H "Authorization: Bearer fnk_…" -H "Content-Type: application/json" \
  -d '{
    "trace_id": "0f8e2c5a9b7d4e3f8a1b2c3d4e5f6a7b",
    "schema_version": 1,
    "source": "self_hosted",
    "function_id": 3, "version_id": 4, "artifact_hash": "<the version's 64-character hash>",
    "input": {"document": "…"}, "output": {"summary": "…"},
    "success": true, "error": null,
    "steps": [{"type": "llm", "model": "gpt-4o-mini"}],
    "tokens_in": 12, "tokens_out": 8, "cost": 0.0003, "latency_ms": 840,
    "created_at": "2026-10-20T09:30:00Z"
  }'
```

The SDKs fill in `trace_id`, `schema_version`, `source` and `created_at` for you
(`fn.report_trace(...)`, `fn.reportTrace(...)`, `fn.ReportTrace(...)`).

The shape is the trace envelope (`TRACE_SCHEMA` in `backend/app/harness/trace.py`). The answer is
`201 {"trace_id": "…", "duplicate": false}`.

## What is checked

| Answer | When |
|---|---|
| 401 | The key is missing, wrong or switched off. |
| 413 | The trace is larger than 1 MB. |
| 422 | `schema_version` is not the one this server accepts (the answer says which). A field is missing, has the wrong type, or is not part of the format. `source` is anything but `self_hosted`. `experiment_id` is set. More than 500 steps. `version_id` is not a version of this function. `artifact_hash` does not match that version. `created_at` is in the future or not a date. |
| 403 | `function_id` is not the function the key belongs to. A key can only report for its own function. |
| 409 | That `trace_id` is already used by someone else. |

Nothing is stored when a trace is refused.

## Good to know

- **Sending twice is harmless.** The same `trace_id` again answers `200 {"duplicate": true}` and stores
  nothing new, so a sender can retry freely.
- **`created_at` is when the call happened**, not when you reported it. Report late and the call still
  lands in the right time window.
- **Hidden fields.** Fields the function marks `sensitive: true` are replaced with `[redacted]` before
  anything is stored, even if you send the real value.
- **Where it shows.** In the function's Monitor, under Traces, with the source filter **self-hosted**,
  and in the function's health numbers (calls, errors, latency, cost) alongside everything else.
- **Not a hosted call.** A reported trace uses no credits and does not count against the key's call
  limits. Its cost is whatever you report. It does not create a call record, so it is not in the
  Runtime calls list or the per-key call metrics.
- **One version of the format.** The server accepts the current `schema_version` only. When the format
  moves on, senders on the old one get a 422 that says which version is accepted.
