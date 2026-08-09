# fnai — Python client

Zero-dependency client for the [functionalAI](https://fnai.dev) public API.

Open-source home: **[ferrouslabs-git/fnai-public](https://github.com/ferrouslabs-git/fnai-public)**  
(Source of truth for development: `sdk/python/` in the private `functionalAI` monorepo; synced here on release tags.)

## Install

```bash
# from the public repo (until published on PyPI)
pip install "git+https://github.com/ferrouslabs-git/fnai-public.git"

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

## API surface

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
