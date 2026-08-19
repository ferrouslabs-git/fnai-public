# fnai — Python client

Zero-dependency Python client for [functionalAI](https://fnai.dev). One file, two surfaces:

- **`Function`** — invoke deployed functions (public runtime API, `fnk_…` invoke key)
- **`Client`** — the management API: create functions, version, evaluate, deploy,
  observe (`fnp_…` personal access token)

Open-source home: **[ferrouslabs-git/fnai-public](https://github.com/ferrouslabs-git/fnai-public)**  
(Source of truth for development: `sdk/python/` in the private `functionalAI` monorepo; synced here on release tags.)

## Install

```bash
# from the public repo (until published on PyPI)
pip install "git+https://github.com/ferrouslabs-git/fnai-public.git#subdirectory=python"

# or copy fnai.py into your project (stdlib only — no install needed)
```

## Quickstart

```python
from fnai import Function

fn = Function(
    "doc_summarizer",
    api_key="fnk_…",                    # Deploy ▸ API keys → Mint
    base_url="https://staging.fnai.dev",
)

# 1. Discover the contract (what the deployed version accepts/returns)
c = fn.contract()
print(c["version"], list(c["input_schema"]["properties"]))

# 2. Invoke — kwargs become the input object, validated server-side
result = fn.run(document="…the text to summarize…")
if result.ok:
    print(result.output)
    print("cost:", result.meta["cost_usd"], "trace:", result.trace_id)
else:
    print("execution failed:", result.error)
```

See [`examples/quickstart.py`](examples/quickstart.py).

## Management: the `Client`

Everything you can do in the app, scriptable. Auth is a **personal access
token**: in the app, open the avatar menu → **API tokens** → Mint (the
`fnp_…` plaintext is shown once; it carries your normal permissions and can
be revoked per token).

```python
from fnai import Client

c = Client(token="fnp_…", base_url="https://staging.fnai.dev")

fn = c.functions.create(slug="summarizer", name="Summarizer",
                        input_schema=IN, output_schema=OUT)
v = c.functions.create_version(fn["id"], snapshot=SNAPSHOT, message="v1")
c.versions.playground(v["id"], input={"document": "…"})     # try before shipping
c.versions.submit(v["id"]); c.versions.approve(v["id"])
c.deployments.deploy(fn["id"], v["id"])

key = c.keys.invoke.mint(fn["id"], name="prod")              # fnk_… shown once
result = c.function(fn["slug"], api_key=key["key"]).run(document="…")

plan = c.eval.plans.create(fn["id"], name="baseline")        # evaluation loop
exp = c.eval.experiments.create(fn["id"], version_ids=[v["id"]],
                                models=[{"name": "mock-small"}])
c.eval.experiments.run(exp["id"])

print(c.functions.health(fn["id"]))                          # observe
```

Namespaces: `functions` · `versions` · `eval.plans` / `eval.experiments` /
`eval.scores` · `datasets` · `deployments` · `keys.provider` / `keys.invoke` ·
`traces` · `auth.tokens`. Full walkthrough:
[`examples/management.py`](examples/management.py).

## API surface (runtime)

| Method | HTTP | Purpose |
|---|---|---|
| `fn.contract()` | `GET /v1/run/{slug}/contract` | Active deployment schemas |
| `fn.run(**input)` | `POST /v1/run/{slug}` | Execute active deployment |

Semantics:
- API-level problems raise `FnaiError` (`.status`, `.detail`): 401 bad key, 404 unknown/undeployed function, 422 input doesn't match the contract, 504 execution timeout.
- An **execution** failure is not an exception — check `result.ok` / `result.status`.
- Deploys and rollbacks take effect behind the same call — your code never changes when a new version goes live.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
