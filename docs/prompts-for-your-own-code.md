# Prompts for code that runs on your own computers

Your prompt stays versioned in Functional AI. Your code fetches the version that is **live**, fills in the `{variables}`
itself and calls its own model. Nothing runs on our side, and no model is called by us.

```python
import fnai
p = fnai.get_prompt("ticket_summary", "live")          # key: FNAI_API_KEY (the function's fnk_… invoke key)
text = p.render(ticket_text=ticket)                    # filled in here
answer = my_model_call(model=p.model, temperature=p.temperature, prompt=text)
with fnai.trace(input={"ticket_text": ticket}, prompt=p) as t:     # the trace names the version it used
    t.output({"summary": answer})
```

TypeScript: `const p = await getPrompt("ticket_summary")`, `p.render({ ticket_text })`, `trace(input, fn, { prompt: p })`.
Go: `p, err := fnai.NewFunction("ticket_summary", key).Prompt("live")`, `p.Render(map[string]any{…})`, and put `p.VersionID`
in the trace you report.

## What "live" means

"Live" is the version last deployed in the app (the Deployments screen). Deploy another version and the next fetch
returns it; roll back and it returns the previous one. History and rollback are the deployment history — there is no
separate switch. Your code and its key never change.

## The route

`GET /v1/prompts/{function}?version=live` (or a version number), with `Authorization: Bearer fnk_…` — the same key and the
same 401 / 404 rules as `POST /v1/run`.

```
{ "function": "ticket_summary", "version": 3, "version_id": 103, "live": true, "artifact_hash": "…",
  "template": "Summarise in one sentence: {ticket_text}", "substitution": {"syntax": "…", "vars": ["ticket_text"]},
  "input_schema": {…}, "output_schema": {…}, "model": "gpt-4o-mini", "settings": {"temperature": 0.2} }
```

- Only **approved** versions are served (a draft, or a number that does not exist, is a 404). `live` with nothing deployed is a 404.
- A named version comes back with the model and temperature it was last deployed with; `live` says whether it is the live one.
- `model` is the model chosen at deploy — one from the model list. It is `null` when nothing names one (choose your own).
- `settings` holds the temperature today; more (reasoning effort, maximum output) will join it without breaking anyone.

## Filling in the blanks

`{name}` becomes the value you give; a name you gave no value for is left as written. That is exactly our own renderer's
rule, so a prompt reads the same wherever it is filled in. Values are turned into text (`str()` in Python, `String()` in
TypeScript, `fmt.Sprint` in Go): pass strings if you need identical text everywhere.

## Keeping it fast and safe

A fetched prompt is kept for 60 seconds (`cache_seconds=` / `cacheSeconds` / `PromptCacheTTL`; 0 = always ask). If
Functional AI can't be reached or is struggling, the copy you already had is used, so a network problem does not stop
your code. A 404 or 401 is never hidden.
