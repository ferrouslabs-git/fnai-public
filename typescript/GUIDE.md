# fnai (TypeScript) — which function do I use?

Find what you want to do, then copy the call. Everything hangs off one `Client`.

```ts
import { Client } from "fnai";
const c = new Client({ token: "fnp_...", baseUrl: "https://<your-fnai-host>" }); // token: avatar menu -> API tokens
```

## Building a function, one part at a time

```ts
const fn = await c.functions.new("WT Reviewer");               // starts a function on your computer; the server creates it at submit()
fn.addInput("review_text", { type: "string", required: true });
fn.addInput("rating", { type: "integer", minimum: 1, maximum: 5 });
fn.addPrompt("You review warranty tickets.");                  // who the function is
fn.addStep("Decide approve or reject, with a reason.");        // at least one step is required
fn.addOutput("verdict", { enum: ["approve", "reject"], required: true });
console.log(String(fn));                                       // the YAML, and what is still missing
const v = await fn.submit("v1");                               // saves everything in one go
```

| I want to add… | Call |
|---|---|
| an input / an output | `addInput(name, {type, required, minimum, maximum, enum, format, pattern, items, …})` / `addOutput(...)` |
| who the function is | `addPrompt(text)` |
| what my words mean / rules / odd cases | `addSection("definitions" \| "constraints" \| "edge_cases", text)`, repeatable |
| a good or bad example | `addExample({input, output})` · `addExample({polarity: "negative", output, note})` |
| the steps, in order | `addStep(text)`, repeatable |
| a tool | `addTool(name, {maxCalls, requires, effect, config})` |
| what a good answer looks like | `addRubric(text)`, repeatable |
| memory between runs | `setMemory({per: "caller" \| "space" \| "organisation", remember, never})` |
| one pass or a loop | `setMode("single_shot" \| "agent")` (it becomes `agent` by itself once a tool is added) |

Every call is **checked as you make it** against the function-definition rules. A mistake throws a
`DefinitionError` that says what to fix, e.g. `unknown key 'minimun' — did you mean 'minimum'?`.
`submit()` checks the whole function and lists every problem at once. Test a sample without calling the
server: `fn.checkInput({ rating: 9 })`.

## Your work is backed up on your computer

After every change a YAML draft file is updated (default `.fnai/drafts/<name>.yaml` next to your script).
If the script stops, pick it up again; On `submit()` the draft file becomes that version's copy in `.fnai/versions/<name>/v<N>.yaml` (not deleted). Each thing
you edit has its own file (`<name>.yaml`, `<name>.v3.yaml`, `<name>.from-v1.yaml`). Every version as YAML, from the server:
`await c.exportYaml("my_function")`. Turn copies off with `new Client({ token, keepVersions: false })`.

```ts
let fn = await c.functions.new("Ticket Triage");               // draft file created
fn.addInput(...);                                              // file updated
//   ... the script stops ...
fn = await c.loadYaml("Ticket Triage");                        // or c.loadYaml("path/to/file.yaml")
await fn.submit();
fn.saveYaml("D:/work/triage.yaml");                            // a readable copy anywhere, any time (it stays)
```

- Choose where: `c.functions.new("X", { saveTo: "D:/work/x.yaml" })` for one function,
  `new Client({ token, draftsFolder: "D:/work/drafts" })` for all, `saveTo: false` for none.
- Loading YAML needs `js-yaml` (`npm install js-yaml`). Saving does not.

## Everything else

| I want to… | Call |
|---|---|
| create a space / work inside it | `c.spaces.create({name})` · `await c.useSpace(name)` |
| find a function by its slug | `c.functions.get("my_function")` (or by id) |
| resume an existing function (its latest version) | `c.loadDraft("my_function")` (or by id) |
| branch without deploying | `await fn.submit("try this", { newVersion: true })` — always a **new** version; the one you started from is left as it was |
| change a loaded version | `fn.removeInput("rating")` · `fn.removeOutput("p")` · `fn.removeStep(2)` (number, or exact text) |
| start a new version from a blank page | `await c.loadDraft("my_function", { fresh: true })` — same function, **empty**; only what you add is kept. Always saves as a **new** version. `submit()` first prints what was dropped (`- input rating`), added (`+ input company`) or changed (`~ input name`); `fn.changes()` returns the same list |
| start from another version | `c.loadDraft("my_function", { version: 1 })` — a deployed version is never changed: saving creates a **new** version from it. A draft version is edited in place. |
| register a tool (web address, MCP, Python) | `c.tools.create({name, description, target_type: "http", url, input_schema, …})` · `c.tools.test({...})` |
| create a dataset / load it from a file | `c.datasets.create(fnId, {name, rows})` · `c.datasets.upload(dsId, "cases.csv", {mapping: {"File Column": "input_field"}})` |
| run an experiment (scores + findings) | `c.eval.experiments.run(expId)` → `result.findings` |
| compare two versions | `c.eval.experiments.compare([expA, expB])` — one experiment per version, same dataset and Score Set |
| deploy (returns a curl) | `c.deployments.deploy(fnId, versionId)` → `out.endpoint`, `out.curl` |
| mint a key to call it | `c.keys.invoke.mint(fnId, "prod")` |
| call a deployed function | `new Function(slug, {apiKey, baseUrl}).run(input)` · check first with `.check(input)` |
| anything missing | the typed methods above cover the API; see `schema.d.ts` |

An experiment pins exactly one version, and `experiments.create` **runs it straight away**. To save one
as a draft first, pass `run: false`, then call `run(expId)`.
