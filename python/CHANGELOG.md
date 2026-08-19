# Changelog

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
