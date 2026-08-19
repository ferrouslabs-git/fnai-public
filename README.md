# functionalAI SDKs

Open-source clients for [functionalAI](https://fnai.dev). All three mirror the same
namespace map and prove themselves with the same golden-path walkthrough.

| Language | Package | Install |
|---|---|---|
| Python ≥ 3.10 | [`python/`](python/) — one stdlib-only file | `pip install "git+https://github.com/ferrouslabs-git/fnai-public.git#subdirectory=python"` |
| TypeScript (Node ≥ 18) | [`typescript/`](typescript/) — zero runtime deps | clone → `cd typescript && npm install && npm run build` |
| Go ≥ 1.22 | [`go/`](go/) — stdlib only | `go get github.com/ferrouslabs-git/fnai-public/go` |

Two surfaces in each:

- **`Function`** — invoke deployed functions (public runtime API `/v1`,
  per-function `fnk_…` invoke key, minted under Deploy ▸ API keys).
- **`Client`** — the management API: create functions, version, evaluate,
  deploy, observe (`fnp_…` personal access token, minted under
  avatar → API tokens; shown once, revocable).

Namespaces (identical across languages): `functions` · `versions` ·
`eval.plans` / `eval.experiments` / `eval.scores` · `datasets` · `deployments`
· `keys.provider` / `keys.invoke` · `traces` · `auth.tokens`.

Start with each package's README and `examples/`. New here? The
[SDK quickstart](https://github.com/ferrouslabs-git/functionalAI/blob/staging/docs/sdk-quickstart.md)
walks token → install → deployed function in five minutes;
[sdk-auth](https://github.com/ferrouslabs-git/functionalAI/blob/staging/docs/sdk-auth.md)
explains the credential kinds.

---

Development happens in the private `functionalAI` monorepo (`sdk/`), synced
here on `sdk-v*` release tags. TypeScript types (`typescript/schema.d.ts`) are
generated from the backend's OpenAPI schema and drift-gated in CI — never
edited by hand. License: [Apache 2.0](LICENSE).
