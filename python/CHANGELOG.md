# Changelog

## 0.14.0 — 2026-11-25

- **Changed: the default address is now production, `https://app.fnai.dev`.** Until now the SDKs and the `fn` tools connected to the UAT test server when you gave no address. Code that passes no address now reaches production. To use another server, pass `base_url` / `baseUrl` / `WithBaseURL`, set `FNAI_BASE_URL` (for example `https://uat.fnai.dev`), or run `fn login --base-url ...`. The order is unchanged: your argument, then `FNAI_BASE_URL`, then the default.

## 0.13.0 — 2026-11-25

- **Fixed: `fnai.eval(..., version=N)` and `fn eval --version N` tested the wrong version (the TypeScript `version` and Go `Version` option had the same mistake and are fixed too).** The number you pass is the version *number* (2 for v2, a draft is fine), but it was sent as a version *id*, so the run pinned another function's version, or none, and sat "pending" for ten minutes. The SDK now looks the id up, and a number the function does not have is a clear error that lists the numbers it has. The server also refuses, at once, an experiment that pins a version that is not the function's (422).

- **New: choose a backup model when you deploy.** `deploy(..., backup_model="...")` (Python `deployments.deploy` and the draft's `deploy`; TypeScript `backupModel`; Go `DeployWithBackup`) sets the model used instead when the main one is down. Leave it out to change nothing (an older version-level backup keeps applying); pass `""` for no backup on purpose. Changing the backup is a redeploy of the same version, never a new version, and it now also works for functions that use tools. Deployments report it as `backup_model`.

- **New: choose the model's run settings when you deploy.** `deploy(..., model_settings={...}, backup_model_settings={...})` (TypeScript `modelSettings` / `backupModelSettings`; Go `DeployWithOptions` with `DeployOptions`) sets more than a temperature: `max_output_tokens`, `reasoning_effort` (low, medium, high) and `thinking_budget_tokens`, only the ones the chosen model accepts (`models.list()` shows each model's `settings`). `temperature=` keeps working as shorthand. A setting the model does not support, or a value out of range, is refused with the reason. Deployments report `model_settings` and `backup_model_settings`.

## 0.12.0 — 2026-11-20

- **New: the agent playbook.** `agent-playbook/PLAYBOOK.md` is the working order for a coding assistant (Claude Code, Cursor, any MCP client) testing an AI agent with Functional AI: add tracing, build a dataset from real traffic, choose the checks, run `fn eval`, read the failed rows, propose a fix, and ask before going live or spending credits. `agent-playbook/claude-code-plugin/` is a Claude Code plugin whose skill loads it; `agent-playbook/cursor/functional-ai.mdc` is the same as a Cursor rule. Our MCP server also offers four prompts (`where_are_we`, `set_up_testing`, `investigate_a_drop`, `fix_a_failing_row`) and tells a connecting assistant to open a session with where the project stands and a short list of choices. Connect the assistant to Functional AI first (Org → Connect AI Tools); the plugin carries the playbook, not the connection. The plugin installs from this repo: `/plugin marketplace add ferrouslabs-git/fnai-public`, then `/plugin install functional-ai@fnai` (a marketplace file now sits at the repo root); the Cursor rule can also be downloaded from Org → Connect AI Tools.

- **New: `fn eval` says which rows failed and why.** After a run, `fn eval` (both with and without `--task`) lists up to ten failed rows: the row's number, the check, the row's input and the check's own explanation, then a link to the run in the app. `--json` now works on every `fn eval` and prints only JSON: `{experiment_id, passed, checks, failed_total, failed_rows: [{row, check, model, input, why, trace_id}], url, compared_with}` (`compared_with` is how the run did against the baseline, when there is one), so a coding assistant can fix a row, run it again and see whether it helped. `fnai.eval(...)` returns `failed_total` and `failed_rows` too. Go `fn` does the same (`fn eval --json`, and `EvalResult.FailedRows` / `Experiments.FailedRows` in the Go SDK).

## 0.11.0 — 2026-11-19

- **New: a quickstart, reference pages and example agents for testing your own AI.** `docs/quickstart.md` takes you from two keys to a traced call and a scored test run in five minutes (Python and TypeScript); `docs/` also has pages on tracing, keys, prompts, tests run by your own code, declared tools, live scoring and space keys; `examples/` has a plain OpenAI agent and a LangGraph agent. These now ship with the SDK.

- **New: declared tools.** A tool your own code runs can be declared to Functional AI with no address, code or credential (Tools → Add tool → Declared, or `target_type: "declared"`), so a function's rules about it (`requires`, `max_calls`, `effect`) produce the recommended behaviour checks and score the steps your code reports. We never call it. See `docs/declared-tools.md`.

- **New: space keys (`fns_…`) — one key for every function in a space.** Made in the app (space → Space keys) with the things it may do ticked: read prompts, send traces, use functions, read functions, run tests, manage datasets, create functions, save versions. It works with `get_prompt`, tracing, `Function.run` and `fnai.eval` / `evaluate` like the other keys, but only inside its own space. Because it covers many functions, name the one: Python `init_tracing(api_key="fns_…", function="my_function")` (or `function=` on each `trace()`), TypeScript `initTracing({ apiKey, function })` (or `{ function }` on `trace()`); a trace body may carry `"function": "<slug>"`. See `docs/space-keys.md`.

- **New: fetch the live prompt and fill it in on your own computer.** `p = fnai.get_prompt("ticket_summary", "live")` (or a version number; key: `FNAI_API_KEY`) returns the prompt text, the model and temperature it was deployed with, the input and output shapes and the version; `p.render(ticket_text=…)` fills in the `{variables}` locally by the same rule as Functional AI's own renderer. Deploying or rolling back in the app changes what "live" returns, with no change to your code. The answer is kept for 60 seconds and the last copy is used if we can't be reached. `fnai.trace(input=…, prompt=p)` names the version a call used. TypeScript: `getPrompt("ticket_summary")`, `p.render({…})`, `trace(input, fn, { prompt: p })`. Go: `fnai.NewFunction(slug, key).Prompt("live")`, `p.Render(…)`. Server: `GET /v1/prompts/{function}?version=live|N`. See `docs/prompts-for-your-own-code.md`.

## 0.10.0 — 2026-11-18

- **New: run a dataset through your own code and get it scored.** `result = fnai.eval("my_function", dataset="tickets", task=my_agent, score_set=4, concurrency=4)` (also `c.eval.run(...)`) takes a snapshot, hands each row's input to `task(input)`, records the steps your `@traced` tools and wrapped model clients take, sends every result back, finishes the experiment and returns `{experiment_id, rows, task_failed, not_run, pass_rate, checks, url}`. A task that raises fails that row only; `resume=<experiment id>` carries on a run that stopped part-way. Needs `FNAI_TOKEN` (or `token=`). `fn eval <function-id> --task module:function [--dataset … --score-set … --concurrency N --min-pass-rate 0.9 --json]` does the same from the command line and exits nonzero when the pass rate is under the threshold.

- **New: tracing helpers for code that runs on your own servers.** Python: `fnai.init_tracing(api_key=…)`, `with fnai.trace(input=…) as t: t.output(…)`, `@fnai.traced` for tools, `fnai.wrap_openai(client)` / `fnai.wrap_anthropic(client)` for model calls. TypeScript: `initTracing`, `await trace(input, async (t) => …)`, `traced(fn)`, `wrapOpenAI`, `wrapAnthropic`. Each call is recorded as one trace with its LLM and tool steps and sent from a background queue with retries; tracing never raises into your code, never slows it down, and never changes what it returns. `version_id` is optional, and the key now names the function, so `function_id` is optional too. See the README. Go follows.

- **New: which row passed or failed each score, and why.** `c.experiments.check_results(exp_id, score_id=…, result="failed", row=…, model=…, version_id=…)` returns one entry per row, repeat, model, version and score, failures first: the row's number in the dataset version, its input (sensitive fields hidden), the score, `result` (`"passed"`, `"failed"` or `"not_applicable"`), the check's own explanation and the run's `trace_id`. It reads every page for you.

- **New: every call says whose provider key paid.** A call's `meta` now has `funded_by`: `"own"` (your organization's key), `"functional_ai"` (Functional AI's key, because your organization chose it for that provider) or `null` (a test model that uses no key). It is always the last field of `meta`, for live calls (`run`, and a finished background run) and for the playground. `RunResult.funded_by` (`fundedBy` in TypeScript, `Meta.FundedBy` in Go) reads it. The Python SDK prints one warning per run of your program the first time it sees `functional_ai`, and the `fn` tool prints a one-line note. An older server that does not send the field gives `None` / `null`.

## 0.9.0 — 2026-10-23

- **New: a builder script can be run again, and a save of what is already there changes nothing.** `c.draft(name=..., existing="continue")` (`existing: "continue"` in TypeScript) carries on with a function that already exists instead of refusing. `submit()` compares your definition with the version it is based on: if it is identical it sends nothing, prints "No changes: vN is unchanged" and returns that version; if it differs it first prints what changed ("+ step Be brief.", "- input rating", "~ output total") and then saves. The server does the same for the plain save (`/api/functions/{id}/submit`, which now also returns `unchanged`), so the app and older SDKs behave the same. `submit(new_version=True)` still always makes a new version.

- **New: pretend tool answers for experiments.** A dataset row can carry `tool_mocks` (what each tool should pretend to answer), and an experiment can run with `tool_mode="mocked"` so no real tool runs. See `add_row` / `addRow` and `experiments.create`.

- **New: page and filter traces.** `c.traces.list(fn_id, before_id=…, status=…, success=…, model=…, tool=…, since=…, until=…)`. Each trace now has an `id`; pass the last one you have as `before_id` for the next page. `tool="echo"` finds the runs whose steps include that tool.
- **New: a call links to its trace.** Runtime call records (`/api/invocations`) carry a `trace_id`.

- **Changed: `fnai-mcp` is now a thin bridge to the hosted MCP server.** It no longer carries its own copy of the tools, so it has all of them (including the experiment tools, `handoff` and `patch_draft`) and can never drift from the hosted server again. Same command and the same `FNAI_TOKEN` / `FNAI_BASE_URL`. Changes made through it are recorded as coming from an AI tool (`mcp`), not from an SDK script, and on an Enterprise plan they need the same elevation as an OAuth connection. Claude Code can also skip the bridge and connect to the hosted URL directly: `claude mcp add --transport http fnai <base url>/mcp --header "Authorization: Bearer fnp_..."`.

## 0.8.0 — 2026-10-22

- **Changed: deploying takes a model.** A function saved by the builder, `load_yaml` or the AI tools names no model, and production calls used to quietly run on the mock. Now `fn.deploy("note", model="gpt-4o-mini")` and `c.deployments.deploy(fn_id, version_id, model="gpt-4o-mini", temperature=0.2)` choose the model the deployment serves with. Without one the space's default model is used, and with neither the deploy is refused. `fn deploy <fn> <version> --model gpt-4o-mini` does the same.
- **New: `c.models.list()`** (and `fn models`) lists every model you can name, with its provider, its price and whether this space has a key for it. A model name that is not on the list is refused with the available names. Add a provider key with `c.keys.provider.add(...)`.

- **New: report a call that ran on your own servers.** `fn.report_trace(trace)` (Python) / `fn.reportTrace(trace)` (TypeScript) sends it to `POST /v1/traces`, so it shows in the monitor next to hosted calls. `trace_id`, `schema_version`, `source` (`self_hosted`) and `created_at` are filled in when you leave them out. Sending the same `trace_id` again is harmless.

## 0.7.0 — 2026-10-13

- **New: change a loaded version.** `remove_input`, `remove_output`, `remove_step`, `load_draft(..., fresh=True)` and `fn.changes()`. A loaded draft can now lose parts, not only gain them.
  `fresh` keeps the function but starts it empty; it always saves as a NEW version (it can't overwrite the one it
  grew from), and `submit()` prints what was dropped, added or changed first. `load_draft` also no longer shares
  its schema objects with the version it loaded.
- **Changed: every saved version keeps its YAML.** Saving no longer deletes the draft file: it becomes that
  version's copy, `.fnai/versions/<name>/v<N>.yaml` (`versions_folder=` / `$FNAI_VERSIONS_DIR` to move it,
  `keep_versions=False` to turn it off). Each thing you edit has its own working file, so two drafts of one
  function never overwrite each other: `<name>.yaml`, `<name>.v3.yaml` (editing draft v3),
  `<name>.from-v1.yaml` (a new version built from v1). `load_yaml("name")` finds a draft whatever it is called
  and asks when two match; a saved copy knows which version it is.
- **New: `client.export_yaml("my_function")`** writes every version as `v1.yaml`, `v2.yaml`, ... from the server.
- **New: `submit(new_version=True)`.** Always creates a NEW version, even if the one you started from is a draft;
  that version is left as it was and the new one records it as its parent. Nothing is deployed.
- **Fixed: loading a version no longer drops parts the builder has no call for.** `load_draft` (and a saved
  draft file) now carry `needs`, `preprocessors`, `derived` and `sensitive` fields and `server:` tool entries
  through to the next version, in their original order. `print(fn)` shows them. A version saved in the older
  editor format is refused with a clear message instead of loading blank. `load_yaml` accepts files that have
  these parts.
- **New: start from any version.** `c.load_draft("my_function", version=1)` loads that version's content.
  A deployed (frozen) version is never changed: saving creates a NEW version based on it (its parent is
  recorded) and never overwrites another draft. A draft version is edited in place. `print(fn)` says
  which, and the draft file remembers it, so a stopped script cannot turn into an overwrite.
- **New: look a function up by its slug.** `c.functions.get("my_function")` and
  `c.load_draft("my_function")` accept a slug as well as an id. An unknown slug raises a 404 that says
  so, suggests the closest slug and lists the functions in the space.
- **Changed: `functions.new()` no longer creates anything on the server.** The function is created
  when you call `submit()`, so a stopped script leaves no empty function behind (`fn.id` is `None` until
  then). `new()` only does a read-only check that the name is free (archived functions keep their name,
  so they count). A failed save after the function was created is remembered, so a retry does not create
  it twice.
- **New: build a function one part at a time.** `client.functions.new(name)`, then `add_input` /
  `add_output` / `add_prompt` / `add_section` / `add_example` / `add_step` / `add_tool` / `add_rubric` /
  `set_memory` / `set_mode`, then `submit()`. Every call is checked against the function-definition
  rules as you make it and raises `DefinitionError` saying what to fix.
- **Fixed: the draft builder sent a shape the server refuses** (`required` inside each field, no
  `flow`, tool options at the wrong level). It now sends a complete v2 definition.
- **New: `print(fn)`** shows the YAML and what is missing; `functions.get` / `create` results print the
  same way and are still dicts.
- **New: a draft file on your computer**, kept up to date after every change (default
  `.fnai/drafts/<slug>.yaml`; `save_to=`, `drafts_folder=`, or `FNAI_DRAFTS_DIR`). `submit()` deletes
  it. `fn.save_yaml(path)` writes a readable copy; `client.load_yaml(path_or_name)` reads one back
  (needs `pyyaml`).
- **New: `fn.check_input(sample)`**, **`Function.check(...)`**, **`client.tools`**,
  **`datasets.upload`**, **`deployments.deploy`** returns `endpoint` + `curl`, **`experiments.run`**
  returns findings with the scores, **`experiments.compare([...])`**.

## 0.3.0 — 2026-08-26

- **New: `parse_prompt_file(path)`.** Reads a `.yaml`/`.yml`/`.json` prompt
  file — the same format the app's canvas Import button reads — and returns
  `{sections, input_schema?, output_schema?}`. Read-and-validate only: no
  network call, nothing created. Raises `PromptFileError` naming exactly
  what's wrong if the file is missing something required or malformed.
  `.json` needs no extra package; `.yaml`/`.yml` needs `pyyaml` installed
  separately (`pip install pyyaml`) — this SDK stays zero-dependency by
  default either way.
- **New: `create_function_from_doc(client, slug=, name=, doc=, ...)`.** Takes
  the dict `parse_prompt_file()` returned and creates the function + its
  first version. This is the step that talks to the API — the two are
  deliberately separate so you can inspect or edit the parsed data first.
- **New: `create_function(client, slug=, name=, sections=, input_schema=, output_schema=, ...)`.**
  The manual counterpart — same result, but you supply the sections/schemas
  directly instead of loading them from a file.

## Earlier changes that were never listed under a version

- **Removed: `versions.submit` / `versions.approve` / `versions.reject`.** The
  human approval step is gone from the product — `deployments.deploy` now takes
  a draft straight to live, running the deploy gate (compile, readiness, gated
  evals) and freezing the version on the way. Golden path is create → version →
  playground → deploy → mint key → run.
- **New: `client.use_space(name)` / `client.current_space`.** Scope every
  later call to one space by name instead of whatever the token's default
  space resolves to — validated against a fresh `me()` call every time, so
  an unknown name raises `SpaceNotFoundError` rather than silently binding.
  Opt-in and fully backward-compatible: skip it and nothing changes, no
  `X-Space` header is ever sent.
- **New: client-side input validation everywhere.** Every method that
  creates or changes something (`functions.create`, `spaces.create`,
  `datasets.create`/`add_row`, `eval.scores.create`, `eval.experiments.create`,
  …) now validates required fields, ids, and — for `functions.create` /
  `spaces.create` — slug/name uniqueness, raising `ValueError` before any
  request goes out.
- **`create_from_yaml`/`create_from_yaml_text` now skip a no-op re-import.**
  Re-pushing the exact same document you last pulled (ignoring `meta`,
  which is re-stamped fresh on every pull) returns the existing version
  instead of minting a duplicate. A genuine edit still creates a new
  version exactly as before — versions stay immutable history either way,
  this only avoids wasting a version number on an unchanged file.

## 0.2.0 — 2026-08-19

- **New: `Client` — the management API surface.** Namespaced, mirroring the API
  families: `functions`, `versions`, `eval.plans` / `eval.experiments` /
  `eval.scores`, `datasets`, `deployments`, `keys.provider` / `keys.invoke`,
  `traces`, `auth.tokens`, plus `me()` and `function()` to cross to the runtime
  surface. Authenticates with a personal access token (`fnp_…`, minted in the
  app under avatar → API tokens).
- `Function` (runtime surface, `fnk_…` invoke keys) is unchanged.
- Shared transport with consistent `FnaiError` behavior across both surfaces.
- New example: `examples/management.py` — the golden path (create → version →
  playground → approve → deploy → mint key → run → observe) in one script.
- Still zero runtime dependencies (stdlib only), Python ≥ 3.10.

## 0.1.0 — 2026-08-10

- Initial release: `Function.run()` / `Function.contract()` against the public
  invoke API (`/v1/run/{slug}`), `fnk_…` key auth, `RunResult` with cost /
  token / trace metadata.
