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
c.deployments.deploy(fn["id"], v["id"])                      # deploy IS the gate — freezes the draft

key = c.keys.invoke.mint(fn["id"], name="prod")              # fnk_… shown once
result = c.function(fn["slug"], api_key=key["key"]).run(document="…")

plan = c.eval.plans.create(fn["id"], name="baseline")        # evaluation loop
exp = c.eval.experiments.create(fn["id"], version_ids=[v["id"]],
                                models=[{"name": "mock-small"}])
c.eval.experiments.run(exp["id"])

print(c.functions.health(fn["id"]))                          # observe
```

Namespaces: `functions` · `spaces` · `versions` · `eval.plans` /
`eval.experiments` / `eval.scores` · `datasets` · `deployments` ·
`keys.provider` / `keys.invoke` · `traces` · `auth.tokens`. Full walkthrough:
[`examples/management.py`](examples/management.py).

### Working with more than one space

Every call above runs against whichever space this token's account
defaults to — fine as long as the account only has one. If it has several,
pick one explicitly:

```python
me = c.me()
print([sp["name"] for sp in me["spaces"]])   # what you can choose from

c.use_space("Marketing")          # validated against the account's real
                                   # spaces every call — raises
                                   # SpaceNotFoundError on a bad name
print(c.current_space)            # "Marketing"

c.functions.create(...)           # now created in "Marketing", not the default
c.use_space(None)                 # back to the account's default
```

`use_space()` is opt-in: skip it entirely and nothing changes — no
`X-Space` header is ever sent, same as before this existed.

### Input validation

Every method that creates or changes something validates its required
fields, ids, and (for `functions.create`/`spaces.create`) name/slug
uniqueness client-side, raising a plain `ValueError` before any request
goes out — e.g. `c.spaces.create(name="")` or a slug that already exists
in the current space fails immediately with a message naming exactly
what's wrong, rather than a generic 422 from the server.

## API surface (runtime)

| Method | HTTP | Purpose |
|---|---|---|
| `fn.contract()` | `GET /v1/run/{slug}/contract` | Active deployment schemas |
| `fn.run(**input)` | `POST /v1/run/{slug}` | Execute active deployment |
| `fn.report_trace(trace)` | `POST /v1/traces` | Report a call that ran on your own servers (see `docs/self-hosted-traces.md`) |

Semantics:
- API-level problems raise `FnaiError` (`.status`, `.detail`): 401 bad key, 404 unknown/undeployed function, 422 input doesn't match the contract, 504 execution timeout.
- An **execution** failure is not an exception — check `result.ok` / `result.status`.
- Deploys and rollbacks take effect behind the same call — your code never changes when a new version goes live.

## Command line (`fn`)

`fn_cli.py` is the verified implementation of REQ-55's `fn` command-line
tool: `login`, `init`, `new`, `ls`, `run`, `eval`, `deploy`, `rollback`,
`pin`, `diff`, `logs`. (There's also a hand-written Go version at
`sdk/go/cmd/fn` — same design, but built without a Go toolchain available
to compile it. This Python one has actually been run, end-to-end, against
a live server.)

```bash
python3 fn_cli.py init myfunction.yaml     # no login needed
python3 fn_cli.py login                    # paste an fnp_… token
python3 fn_cli.py new myfunction.yaml
python3 fn_cli.py eval <function-id>       # exits nonzero on failure — use in CI
```

### Packaging as a single binary

The ticket asks for a self-contained binary, not a script that needs a
Python interpreter on PATH. [PyInstaller](https://pyinstaller.org) does
that:

```bash
pip install pyinstaller
pyinstaller --onefile --name fn fn_cli.py
./dist/fn help
```

Verified: the resulting `dist/fn` runs standalone commands (`help`,
`init`) and live API calls (`ls` against a real server) with `PATH`
stripped down to just `/usr/bin:/bin` — no `python3` reachable at all.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

## Report a call that ran on your own servers

```python
fn.report_trace({
    "function_id": 3, "version_id": 4, "artifact_hash": "…64 characters…",
    "input": {"document": "…"}, "output": {"summary": "…"}, "success": True,
    "steps": [{"type": "llm"}], "tokens_in": 12, "tokens_out": 8, "cost": 0.0003, "latency_ms": 840,
})   # → {"trace_id": "…", "duplicate": False}; the same trace_id again is harmless
```

It shows in the monitor next to hosted calls. See `docs/self-hosted-traces.md`.
