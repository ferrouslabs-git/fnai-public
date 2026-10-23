# Changelog

## 0.9.0 — 2026-10-23

- **New: a builder script can be run again, and a save of what is already there changes nothing.** `c.draft(name=..., existing="continue")` (`existing: "continue"` in TypeScript) carries on with a function that already exists instead of refusing. `submit()` compares your definition with the version it is based on: if it is identical it sends nothing, prints "No changes: vN is unchanged" and returns that version; if it differs it first prints what changed ("+ step Be brief.", "- input rating", "~ output total") and then saves. The server does the same for the plain save (`/api/functions/{id}/submit`, which now also returns `unchanged`), so the app and older SDKs behave the same. `submit(new_version=True)` still always makes a new version.

- **New: pretend tool answers for experiments.** A dataset row can carry `tool_mocks` (what each tool should pretend to answer), and an experiment can run with `tool_mode="mocked"` so no real tool runs. See `add_row` / `addRow` and `experiments.create`.

- **New: page and filter traces.** `c.traces.list({ functionId, beforeId, status, success, model, tool, since, until })`. Each trace now has an `id`; pass the last one you have as `beforeId` for the next page. `tool: "echo"` finds the runs whose steps include that tool.
- **New: a call links to its trace.** Runtime call records carry a `trace_id`.

## 0.8.0 — 2026-10-22

- **Changed: deploying takes a model.** A function saved by the builder or the AI tools names no model, and production calls used to quietly run on the mock. `c.deployments.deploy(fnId, versionId, { model: "gpt-4o-mini", temperature: 0.2 })` and the draft's `deploy(note, { model })` choose the model the deployment serves with. Without one the space's default model is used, and with neither the deploy is refused.
- **New: `c.models.list()`** lists every model you can name, with its provider, its price and whether this space has a key for it.

- **New: report a call that ran on your own servers.** `fn.report_trace(trace)` (Python) / `fn.reportTrace(trace)` (TypeScript) sends it to `POST /v1/traces`, so it shows in the monitor next to hosted calls. `trace_id`, `schema_version`, `source` (`self_hosted`) and `created_at` are filled in when you leave them out. Sending the same `trace_id` again is harmless.

## 0.7.0 — 2026-10-13

- **New: change a loaded version.** `removeInput`, `removeOutput`, `removeStep`, `loadDraft(..., { fresh: true })` and `fn.changes()`. A loaded draft can now lose parts, not only gain them.
  `fresh` keeps the function but starts it empty; it always saves as a NEW version (it can't overwrite the one it
  grew from), and `submit()` prints what was dropped, added or changed first. `load_draft` also no longer shares
  its schema objects with the version it loaded.
- **Changed: every saved version keeps its YAML.** Saving no longer deletes the draft file: it becomes that
  version's copy, `.fnai/versions/<name>/v<N>.yaml` (`versionsFolder` / `$FNAI_VERSIONS_DIR` to move it,
  `keepVersions: false` to turn it off). Each thing you edit has its own working file (`<name>.yaml`,
  `<name>.v3.yaml`, `<name>.from-v1.yaml`), so two drafts of one function never overwrite each other.
  `loadYaml("name")` finds a draft whatever it is called and asks when two match.
- **New: `client.exportYaml("my_function")`** writes every version as `v1.yaml`, `v2.yaml`, ... from the server.
- **New: `submit(message, { newVersion: true })`.** Always creates a NEW version, even if the one you started from is
  a draft; that version is left as it was and the new one records it as its parent. Nothing is deployed.
- **Fixed: loading a version no longer drops parts the builder has no call for.** `loadDraft` (and a saved
  draft file) now carry `needs`, `preprocessors`, `derived` and `sensitive` fields and `server:` tool entries
  through to the next version, in their original order. `String(fn)` shows them. A version saved in the older
  editor format is refused with a clear message instead of loading blank. `loadYaml` accepts files that have
  these parts.
- **New: start from any version.** `client.loadDraft("my_function", { version: 1 })` loads that version's
  content. A deployed (frozen) version is never changed: saving creates a NEW version based on it (its
  parent is recorded) and never overwrites another draft. A draft version is edited in place.
  `String(fn)` says which, and the draft file remembers it.
- **New: look a function up by its slug.** `client.functions.get("my_function")` and
  `client.loadDraft("my_function")` accept a slug as well as an id. An unknown slug throws a 404 that
  says so, suggests the closest slug and lists the functions in the space.
- **Changed: `functions.new()` no longer creates anything on the server.** The function is created
  when you call `submit()`, so a stopped script leaves no empty function behind (`fn.id` is `undefined`
  until then). `new()` only does a read-only check that the name is free (archived functions count). A
  failed save after the function was created is remembered, so a retry does not create it twice.
- **New: build a function one part at a time.** `client.functions.new(name)` starts an empty
  function; then `addInput` / `addOutput` / `addPrompt` / `addSection` / `addExample` / `addStep` /
  `addTool` / `addRubric` / `setMemory` / `setMode`, then `submit()`. Every call is checked against
  the function-definition rules (function-dsl.md) as you make it and throws `DefinitionError` saying
  what to fix. `submit()` checks the whole function and lists every problem at once.
- **Fixed: the draft builder sent a shape the server refuses.** `required` was stored inside each
  field (the server reads a list on the schema, so it was silently ignored), there was no way to add
  the steps (`flow`) the server needs, and `addTool` options went at the wrong level. It now sends a
  complete v2 definition.
- **New: print a function.** `String(fn)` / `console.log(fn)` shows the YAML and a list of what is
  missing. `functions.get` / `functions.create` results print the same way, and are still plain
  objects (`fn.id`).
- **New: a draft file on your computer.** While you build, a YAML draft is kept up to date after every
  change (default `.fnai/drafts/<slug>.yaml`; choose with `saveTo`, `draftsFolder`, or
  `FNAI_DRAFTS_DIR`; `saveTo: false` turns it off). `submit()` deletes it. `fn.saveYaml(path)` writes a
  readable copy any time; `client.loadYaml(pathOrName)` reads a file back into a full draft (needs
  `js-yaml`, an optional install).
- **New: `fn.checkInput(sample)`** and **`FnaiFunction.check(input)`** test an input against the rules
  without calling the server. Errors now show the server's validation detail instead of `[object Object]`.
- **New: `client.tools`** (`list`, `get`, `create`, `test`, `update`, `usage`),
  **`datasets.upload`** (CSV / JSON / Excel with a column mapping), **`deployments.deploy`** now returns
  `endpoint` and `curl` (and `deployments.curl`), **`experiments.run`** returns the findings with the
  scores, and **`experiments.compare([idA, idB])`** compares finished experiments side by side.

- **New: `Spaces` class (`client.spaces.create({ name })`).** Didn't exist
  in this SDK before at all — the Python client already had it.
- **New: `client.useSpace(name)` / `client.currentSpace`.** Scope every
  later call to one space by name instead of whatever the token's default
  space resolves to — validated against a fresh `me()` call every time, so
  an unknown name throws `SpaceNotFoundError` rather than silently binding.
  Opt-in and fully backward-compatible: skip it and nothing changes, no
  `X-Space` header is ever sent.
- **New: client-side input validation everywhere.** Every method that
  creates or changes something (`functions.create`, `spaces.create`,
  `datasets.create`/`addRow`, `eval.scores.create`, `eval.experiments.create`,
  …) now validates required fields, ids, and — for `functions.create` /
  `spaces.create` — slug/name uniqueness, throwing before any request goes
  out.
- **`createFromYaml`/`createFromYamlText` now skip a no-op re-import.**
  Re-pushing the exact same document you last pulled (ignoring `meta`,
  which is re-stamped fresh on every pull) returns the existing version
  instead of minting a duplicate. A genuine edit still creates a new
  version exactly as before — versions stay immutable history either way,
  this only avoids wasting a version number on an unchanged file.

## 0.3.0 — 2026-08-26

- **New: `parsePromptFile(path)`.** Reads a `.yaml`/`.yml`/`.json` prompt
  file — the same format the app's canvas Import button reads — and returns
  `{sections, input_schema?, output_schema?}`. Read-and-validate only: no
  network call, nothing created. Throws `PromptFileError` naming exactly
  what's wrong if the file is missing something required or malformed.
  `.json` needs no extra package; `.yaml`/`.yml` needs `js-yaml` installed
  separately (`npm install js-yaml`) — this SDK stays zero-dependency by
  default either way.
- **New: `createFunctionFromDoc(client, { slug, name, doc, ... })`.** Takes
  the object `parsePromptFile()` returned and creates the function + its
  first version. This is the step that talks to the API — the two are
  deliberately separate so you can inspect or edit the parsed data first.
- **New: `createFunction(client, { slug, name, sections, inputSchema?, outputSchema?, ... })`.**
  The manual counterpart — same result, but you supply the sections/schemas
  directly instead of loading them from a file.

## 0.2.0 — 2026-08-19

- Initial `Client` management surface, mirroring the Python SDK
  namespace-for-namespace.
