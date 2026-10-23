# Changelog

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
