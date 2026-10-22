# fnai (Go) — which function do I use?

Find what you want to do, then copy the call. Everything hangs off one `Client`.

```go
import fnai "github.com/ferrouslabs-git/fnai-public/go"

c := fnai.NewClient("fnp_...", fnai.WithBaseURL("https://<your-fnai-host>")) // token: avatar menu -> API tokens
```

## Building a function, one part at a time

```go
fn, _ := c.Functions.New("WT Reviewer")                     // starts a function on your computer; the server creates it at Submit()
fn.AddInput("review_text", map[string]any{"type": "string", "required": true})
fn.AddInput("rating", map[string]any{"type": "integer", "minimum": 1, "maximum": 5})
fn.AddPrompt("You review warranty tickets.")                // who the function is
fn.AddStep("Decide approve or reject, with a reason.")      // at least one step is required
fn.AddOutput("verdict", map[string]any{"enum": []any{"approve", "reject"}, "required": true})
fmt.Println(fn)                                             // the YAML, and what is still missing
v, err := fn.Submit("v1")                                   // saves everything in one go
```

Every `Add…` call returns an `error`. It is checked **as you make the call** against the function-definition
rules; a mistake returns a `*fnai.DefinitionError` that says what to fix, e.g.
`unknown key 'minimun' — did you mean 'minimum'?`. `Submit()` checks the whole function and lists every
problem at once. Test a sample without calling the server: `fn.CheckInput(map[string]any{"rating": 9})`.

| I want to add… | Call |
|---|---|
| an input / an output | `AddInput(name, spec)` / `AddOutput(name, spec)` — spec keys: `type`, `required`, `minimum`, `maximum`, `enum`, `format`, `pattern`, `items`, … |
| who the function is | `AddPrompt(text)` |
| what my words mean / rules / odd cases | `AddSection("definitions" \| "constraints" \| "edge_cases", text)`, repeatable |
| a good or bad example | `AddExample(fnai.ExampleOpts{Input, Output})` · `{Polarity: "negative", Output, Note}` |
| the steps, in order | `AddStep(text)`, repeatable |
| a tool | `AddTool(name, fnai.ToolOptions{MaxCalls, Requires, Effect, Config})` |
| what a good answer looks like | `AddRubric(text)`, repeatable |
| memory between runs | `SetMemory(fnai.Memory{Per: "caller" \| "space" \| "organisation", Remember, Never})` |
| one pass or a loop | `SetMode("single_shot" \| "agent")` (it becomes `agent` by itself once a tool is added) |

## Your work is backed up on your computer

After every change a YAML draft file is updated (default `.fnai/drafts/<name>.yaml` next to your program).
If the program stops, pick it up again; On `Submit()` the draft file becomes that version's copy in `.fnai/versions/<name>/v<N>.yaml` (not deleted). Each thing
you edit has its own file (`<name>.yaml`, `<name>.v3.yaml`, `<name>.from-v1.yaml`). Every version as YAML, from the server:
`c.ExportYAMLBySlug("my_function", "")`. Turn copies off with `fnai.WithoutVersionCopies()`.

```go
fn, _ := c.Functions.New("Ticket Triage")          // draft file created
fn.AddInput(...)                                    // file updated
//   ... the program stops ...
fn, _ = yamlload.Load(c, "Ticket Triage")          // or yamlload.Load(c, "path/to/file.yaml")
fn.Submit("")
fn.SaveYAML("D:/work/triage.yaml")                 // a readable copy anywhere, any time (it stays)
```

- Choose where: `c.Functions.New("X", fnai.SaveTo("D:/work/x.yaml"))` for one function,
  `fnai.NewClient(token, fnai.WithDraftsFolder("D:/work/drafts"))` for all, `fnai.NoDraftFile()` for none.
- Loading YAML is in its own package, `yamlload` (it uses the `gopkg.in/yaml.v3` library). The main `fnai`
  package has no dependencies, and saving YAML does not need `yamlload`.

## Everything else

| I want to… | Call |
|---|---|
| create a space / work inside it | `c.Spaces.Create(name)` · `c.UseSpace(name)` |
| find a function by its slug | `c.Functions.GetBySlug("my_function")` (`Get(id)` takes the id) |
| resume an existing function (its latest version) | `c.LoadDraftBySlug("my_function")` (`LoadDraft(id)` takes the id) |
| branch without deploying | `fn.SubmitAsNew("try this")` — always a **new** version; the one you started from is left as it was |
| change a loaded version | `fn.RemoveInput("rating")` · `fn.RemoveOutput("p")` · `fn.RemoveStep(2)` (number) · `fn.RemoveStepText("exact text")` |
| start a new version from a blank page | `c.LoadDraft(id, fnai.WithFresh())` — same function, **empty**; only what you add is kept. `Submit` and `SubmitAsNew` both save a **new** version for a fresh draft. It first prints what was dropped (`- input rating`), added (`+ input company`) or changed (`~ input name`); `fn.Changes()` returns the same list |
| start from another version | `c.LoadDraftBySlug("my_function", fnai.WithVersion(1))` — a deployed version is never changed: `Submit` creates a **new** version from it. A draft version is edited in place. |
| see a function as YAML + what is missing | `c.Functions.Describe(fnID)` |
| register a tool (web address, MCP, Python) | `c.Tools.Create(map[string]any{"name": …, "description": …, "target_type": "http", "url": …, "input_schema": …})` · `c.Tools.Test(...)` |
| create a dataset / load it from a file | `c.Datasets.Create(fnID, name, rows)` · `c.Datasets.Upload(dsID, "cases.csv", map[string]string{"File Column": "input_field"}, nil)` |
| run an experiment (scores + findings) | `c.Eval.Experiments.Run(expID)` → `result.Findings` |
| compare two versions | `c.Eval.Experiments.Compare([]int{expA, expB})` — one experiment per version, same dataset and Score Set |
| deploy (returns a curl) | `c.Deployments.Deploy(fnID, versionID)` → `out.Endpoint`, `out.Curl` |
| mint a key to call it | `c.Keys.Invoke.Mint(fnID, "prod")` |
| call a deployed function | `fnai.NewFunction(slug, apiKey, fnai.WithBaseURL(...)).Run(input)` · check first with `.Check(input)` |

An experiment pins exactly one version, and `Experiments.Create` **runs it straight away** unless you pass
`"run": false` in the pins; then call `Run(expID)`.
