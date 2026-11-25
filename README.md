# functionalAI SDKs

Open-source clients for [functionalAI](https://fnai.dev). All three mirror the same
namespace map and prove themselves with the same golden-path walkthrough.

| Language | Package | Install |
|---|---|---|
| Python ≥ 3.10 | [`python/`](python/) — one stdlib-only file | `pip install "git+https://github.com/ferrouslabs-git/fnai-public.git#subdirectory=python"` |
| TypeScript (Node ≥ 18) | [`typescript/`](typescript/) — zero runtime deps | clone → `cd typescript && npm install && npm run build` |
| Go ≥ 1.22 | [`go/`](go/) — stdlib only | `go get github.com/ferrouslabs-git/fnai-public/go` |

## Install

Pick your language. Nothing else is needed (the Python and Go clients use only the standard library; the TypeScript client has no runtime packages).

**Python (3.10 or newer)**
```bash
pip install "git+https://github.com/ferrouslabs-git/fnai-public.git#subdirectory=python"
python -c "import fnai; print(fnai.__version__)"      # prints the installed version
```
This also installs the `fn` command-line tool and `fnai-mcp`. Update with the same command plus `--upgrade`; remove with `pip uninstall fnai`.
No install at all? Copy [`python/fnai.py`](python/fnai.py) into your project.

**TypeScript (Node 18 or newer)**
```bash
git clone https://github.com/ferrouslabs-git/fnai-public.git
cd fnai-public/typescript && npm install && npm run build      # builds dist/
cd /path/to/your/project && npm install /path/to/fnai-public/typescript
```
Update: `git pull` in the clone, run `npm run build` again.

**Go (1.22 or newer)**
```bash
go get github.com/ferrouslabs-git/fnai-public/go
```
Update with `go get -u github.com/ferrouslabs-git/fnai-public/go`.

**Then, two things before your first call**
1. Get a key in the app: a `fnk_…` invoke key (Deploy ▸ API keys) to run a deployed function, or an `fnp_…` personal access token (avatar → API tokens) for the management `Client`.
2. The SDK talks to the address you pass as `base_url`, else the `FNAI_BASE_URL` environment variable, else its built-in default. Set `FNAI_BASE_URL` to point at a different server.

Two surfaces in each:

- **`Function`** — invoke deployed functions (public runtime API `/v1`,
  per-function `fnk_…` invoke key, minted under Deploy ▸ API keys).
- **`Client`** — the management API: create functions, version, evaluate,
  deploy, observe (`fnp_…` personal access token, minted under
  avatar → API tokens; shown once, revocable).

Namespaces (identical across languages): `functions` · `versions` ·
`eval.plans` / `eval.experiments` / `eval.scores` · `datasets` · `deployments`
· `keys.provider` / `keys.invoke` · `traces` · `auth.tokens`.

**Testing an AI that runs on your own computers?** Start with the [five-minute quickstart](docs/quickstart.md): trace a call and
get a scored test run, in Python or TypeScript. The other pages are in [docs/](docs/README.md).

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
