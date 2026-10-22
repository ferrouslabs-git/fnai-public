# fnai — which function do I use?

Find what you want to do, then copy the call. Everything below hangs off one
`Client`.

```python
from fnai import Client
c = Client(token="fnp_...", base_url="https://<your-fnai-host>")   # token: avatar menu -> API tokens
```

## The usual order

| # | I want to… | Call |
|---|---|---|
| 1 | create a space | `c.spaces.create(name="demo")` |
| 2 | work inside a space | `c.use_space("demo")` |
| 3 | create a function, one part at a time | `fn = c.functions.new("Summarizer")` then `fn.add_input(...)`, `fn.add_prompt(...)`, `fn.add_step(...)`, `fn.add_output(...)`, `fn.submit()` |
| 4 | save it as a version | `fn.submit(message="v1")` (returns the version; its `id` is what you deploy) |
| 5 | register a tool it can use | `c.tools.create(name="lookup", description="...", target_type="http", url="https://...")` |
| 6 | create a dataset | `c.datasets.create(fn.id, name="cases", rows=[{"input": {...}}])` |
| 7 | load a dataset from a file | `c.datasets.upload(ds["id"], "cases.csv", mapping={"File Column": "input_field"})` |
| 8 | run an experiment (scores + findings) | `c.eval.experiments.run(exp["id"])` |
| 9 | compare two versions | `c.eval.experiments.compare([exp_a, exp_b])` |
| 10 | deploy (returns a curl) | `c.deployments.deploy(fn.id, v["id"])` → `out["curl"]` |
| 11 | mint a key to call it | `c.keys.invoke.mint(fn.id, name="prod")` |

## Everything else, by task

**Spaces**
- Create: `c.spaces.create(name=...)` · switch: `c.use_space(name)` · current: `c.current_space`

**Functions and versions**
- List / get: `c.functions.list()` · `c.functions.get("my_function")` (by slug) or `c.functions.get(fn_id)`
- Build one part at a time (checked as you go, against function-dsl.md). `functions.new` creates nothing on the server; the function is created when you call `submit()` (`fn.id` is `None` until then):
  ```python
  fn = c.functions.new("WT Reviewer")
  fn.add_input("review_text", type="string", required=True)
  fn.add_input("rating", type="integer", minimum=1, maximum=5)
  fn.add_prompt("You review warranty tickets...")
  fn.add_step("Read the review and the rating.")        # at least one step is required
  fn.add_step("Decide approve or reject, with a reason.")
  fn.add_output("verdict", type="string", enum=["approve", "reject"], required=True)
  fn.submit()
  ```
  A wrong rule raises `DefinitionError` on that line, e.g. `unknown key 'minimun' - did you mean 'minimum'?`
  Also: `add_section("constraints", ...)`, `add_example(...)`, `add_tool("name", max_calls=2)`, `set_mode("agent")`.
  The server needs at least one output field and one step; `submit()` tells you if either is missing.
  The function becomes an agent on its own once it has a tool.
- Your work is backed up on your computer as you build. After every change a YAML draft file is updated
  (default `.fnai/drafts/<name>.yaml` next to your script). If the script stops, pick it up again:
  `fn = c.load_yaml("Ticket Triage")` (or `c.load_yaml("path/to/file.yaml")`). On `submit()` the draft file becomes that version's copy in `.fnai/versions/<name>/v<N>.yaml` (not deleted).
  Each thing you edit has its own file: `<name>.yaml`, `<name>.v3.yaml` (editing draft v3), `<name>.from-v1.yaml` (a new version built from v1).
  Get every version as YAML from the server: `c.export_yaml("my_function")` writes `v1.yaml`, `v2.yaml`, ... Turn copies off with `Client(token, keep_versions=False)`.
  - Choose where: `c.functions.new("X", save_to="D:/work/x.yaml")` for one function, `Client(token, drafts_folder="D:/work/drafts")` for all, `save_to=False` for none.
  - Keep a readable copy anywhere, any time: `fn.save_yaml("D:/work/x.yaml")`. It stays after `submit()`, and `c.load_yaml` reads it back.
  - Loading YAML needs PyYAML (`pip install pyyaml`). Saving does not.
- See what you have and what is missing: `print(fn)` shows the YAML and a "Missing" list.
  Works on `c.functions.create(...)` and `c.functions.get(id)` results too.
- Test a sample without calling the server: `fn.check_input({"rating": 9})` -> list of problems.
- Resume an existing one: `c.load_draft("my_function")` (by slug) or `c.load_draft(fn_id)`. You get the **latest** version's content.
- Start from another version: `c.load_draft("my_function", version=1)`. A deployed (frozen) version is never changed: saving
  creates a **new** version based on it, and never overwrites another draft. A draft version (`version=3`) is edited in place.
  `print(fn)` says which it will do.
- Branch without deploying: `fn.submit("try this", new_version=True)` always creates a **new** version, even when the one you
  started from is a draft. That version is left as it was and the new one records it as its parent. Nothing is deployed. Rollback (`c.deployments.rollback`) only moves what is live; it creates no version.
- Change a loaded version before saving it as a new one: `fn.remove_input("rating")`, `fn.remove_output("p")`, `fn.remove_step(2)`
  (by number, or by exact text). Adding a field that is already there is an error, so a loaded draft is for adding NEW parts and removing old ones.
- Start a new version from a blank page: `c.load_draft("my_function", fresh=True)` keeps the function (same name and slug) but starts it
  **empty**, so only what you add is in the new version. A fresh draft always saves as a **new** version and never overwrites the one
  it grew from. `submit()` first prints what was dropped (`- input rating`), added (`+ input company`) or changed (`~ input name`);
  `fn.changes()` gives the same list any time after a load. Parts you do not re-add (prompt, tools, memory) show up as dropped.
- One-call create still works: `c.functions.create(slug=..., name=..., input_schema=..., output_schema=...)`
- Versions: `c.functions.versions(fn_id)` · try one first: `c.versions.playground(v_id, input={...})`

**Tools** (what a function can reach for)
- Use a built-in tool by name: `fn.add_tool("web_search")`. See what exists: `[t["name"] for t in c.tools.list()]`.
  `submit()` checks the names and suggests the right one if you mistype.
- Make your own, three kinds:
  ```python
  # 1. a web address that already exists
  c.tools.create(name="order_lookup", description="Look up an order's status.",
                 target_type="http", url="https://api.example.com/orders", method="POST",
                 effect="read_only",
                 input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
                 credential={"token": "..."})        # stored securely, never returned

  # 2. a little Python (no network, standard library only)
  c.tools.create(name="vat", description="Add 20% VAT.", target_type="python",
                 effect="read_only",
                 input_schema={"type": "object", "properties": {"net": {"type": "number"}}, "required": ["net"]},
                 code="def run(args):\n    return {'gross': round(args['net'] * 1.2, 2)}")

  # 3. an MCP server: target_type="mcp", url=..., mcp_tool_name=...
  ```
  `effect` says what it does: `read_only`, `idempotent` (safe to repeat) or `side_effecting` (the default; it changes something).
- Try before saving (a real call, nothing stored): `c.tools.test(target_type="http", url=..., sample_args={...})`
- `c.tools.list()` · `c.tools.get(id)` · `c.tools.update(id, description=...)` · who uses it: `c.tools.usage(id)`
- Then attach: `fn.add_tool("order_lookup")`.

**Datasets**
- `c.datasets.list(fn_id)` · `.rows(ds_id)` · `.add_row(ds_id, input={...}, expected={...})`
- From a file (.csv, .json array of objects, .xlsx):
  `c.datasets.upload(ds_id, "file.csv", mapping={"col": "input_field"}, expected_mapping={"col": "output_field"})`
  Columns are never guessed — you say which file column feeds which field.
- Freeze a copy: `c.datasets.snapshot(ds_id)`

**Comparing two versions**
- An experiment tests exactly **one** version. To compare v1 and v2, run one experiment on each (same dataset, same Score Set), then:
  `c.eval.experiments.compare([exp_v1["id"], exp_v2["id"]])`
  -> `rows`: trust score, pass rate, cost per run, p95 latency, each dimension, each check, with the winner of each row (`best` = index).

**Evaluating** (Score Sets and Contract Tests)
- Score Sets: `c.eval.plans.create(fn_id, name=...)` · `c.eval.plans.add_score(plan_id, ...)`
- Experiments: `c.eval.experiments.create(fn_id, version_ids=[v_id], snapshot_id=..., plan_id=..., models=[{"name": "mock-small"}])`
  This **runs it straight away** and returns the finished experiment. To save it as a draft first, pass `run=False`, then `c.eval.experiments.run(exp_id)`.
- Run: `c.eval.experiments.run(exp_id)` → scores **and** `result["findings"]`
  (`include_findings=False` for scores only; `.findings(exp_id)` to fetch them later)

**Deploying**
- `c.deployments.deploy(fn_id, v_id)` → `out["endpoint"]` and `out["curl"]`
- Curl again later, with your key in it: `c.deployments.curl(fn_id, api_key="fnk_...")`
- Now live: `c.deployments.current(fn_id)` · history: `.history(fn_id)` · undo: `.rollback(fn_id)`

**Calling a deployed function** (needs an `fnk_` key, not your `fnp_` token)
```python
from fnai import Function
f = Function("summarizer", api_key="fnk_...", base_url="https://<host>")
f.check(document="...")   # [] or problems naming the field - no model call, no cost
f.run(document="...")     # a bad input is refused first: HTTP 422 "field 'rating' must be at most 5, got 9"
```

**Keys:** model-provider keys `c.keys.provider.*` · runtime keys `c.keys.invoke.*`
**Observing:** `c.traces.list()` · `c.functions.health(fn_id)`
**Anything missing:** `c.raw("GET", "/api/...")` calls any API path directly.

## End to end

Every call below was run against a live server.

```python
c.spaces.create(name="demo"); c.use_space("demo")

fn = c.functions.new("WT Reviewer")
fn.add_input("review_text", required=True)
fn.add_input("rating", type="integer", minimum=1, maximum=5)
fn.add_prompt("You review warranty tickets.")
fn.add_step("Decide approve or reject, with a reason.")
fn.add_output("verdict", enum=["approve", "reject"], required=True)
print(fn)                                    # YAML + what is missing
v = fn.submit(message="v1")

ds = c.datasets.create(fn.id, name="cases")
c.datasets.upload(ds["id"], "cases.csv",
                  mapping={"text": "review_text", "stars": "rating"},
                  expected_mapping={"want": "verdict"})
snap = c.datasets.snapshot(ds["id"])

plan = c.eval.plans.create(fn.id, name="basics")                      # a Score Set
c.eval.plans.add_score(plan["id"], check="enum", target="verdict",
                       gate=False, params={"values": ["approve", "reject"]})

exp = c.eval.experiments.create(fn.id, version_ids=[v["id"]], snapshot_id=snap["id"],
                                plan_id=plan["id"], models=[{"name": "mock-small"}], run=False)
res = c.eval.experiments.run(exp["id"])      # scores in res["results"], findings in res["findings"]

out = c.deployments.deploy(fn.id, v["id"])   # now live
print(out["curl"])

# change it: v1 is deployed, so submit() starts v2
d = c.load_draft(fn.id)
d.add_section("constraints", "Always give a one-sentence reason.")
v2 = d.submit(message="v2")
exp2 = c.eval.experiments.create(fn.id, version_ids=[v2["id"]], snapshot_id=snap["id"],
                                 plan_id=plan["id"], models=[{"name": "mock-small"}])   # runs now
for row in c.eval.experiments.compare([exp["id"], exp2["id"]])["rows"]:
    print(row)                               # each row names the winner (best = index)
```
