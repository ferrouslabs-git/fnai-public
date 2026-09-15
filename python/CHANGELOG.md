# Changelog

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

## Unreleased

- **Removed: `versions.submit` / `versions.approve` / `versions.reject`.** The
  human approval step is gone from the product — `deployments.deploy` now takes
  a draft straight to live, running the deploy gate (compile, readiness, gated
  evals) and freezing the version on the way. Golden path is create → version →
  playground → deploy → mint key → run.

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
