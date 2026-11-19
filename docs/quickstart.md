# Quickstart: test and watch your own AI in five minutes

Your AI runs on **your** computers, with your own model key. Functional AI keeps the parts that are easier to share: the test
questions, the scoring, the prompts and a record of what your AI did. You send it the answers; it scores them and shows you.

By the end you will have:

1. a **trace** of a real call from your code, visible in the app, and
2. a **scored test run** of your AI on four test questions, in the app.

Python and TypeScript are shown side by side. You need Python 3.10+ or Node 18+, and a Functional AI account.

## 1. Get two keys

Both come from the app. Each is shown **once**: copy it when it appears.

- **Your personal token** (`fnp_…`) is for you, to set things up. Click your avatar → **API tokens** → name it → **Mint**.
- **A space key** (`fns_…`) is for your servers and automated tests. Open your space (the **Workbench** list) → **Space keys** →
  name it `quickstart`, tick **Send traces**, **Run tests** and **Read functions** → **Create key**. One key works for every
  function in the space and can do only what you ticked. More in [space-keys.md](space-keys.md).

```bash
export FNAI_TOKEN=fnp_…          # your personal token
export FNAI_SPACE_KEY=fns_…      # the space key
```

## 2. Install

**Python**

```bash
pip install "git+https://github.com/ferrouslabs-git/fnai-public.git#subdirectory=python"
```

**TypeScript**

```bash
git clone https://github.com/ferrouslabs-git/fnai-public.git
(cd fnai-public/typescript && npm install && npm run build)
npm install ./fnai-public/typescript        # in your own project
```

## 3. Set up once

This creates a function (the contract and prompt Functional AI keeps), a dataset of four test tickets with the right answers,
and one check that compares your AI's answer with the right one. You can do the same in the app instead (Workbench → New
function; Datasets; Scoring).

**Python** — save as `setup.py` and run `python setup.py`:

```python
# Run once. Creates the function (the contract and prompt we keep), 4 test rows, and one check.
import os
import fnai

c = fnai.Client(os.environ["FNAI_TOKEN"])            # your personal token (fnp_…)
if os.environ.get("FNAI_SPACE"):
    c.use_space(os.environ["FNAI_SPACE"])            # only if you are not in your default space
SLUG = os.environ.get("SLUG", "support_bot")

fn = c.functions.create(
    slug=SLUG, name="Support bot",
    input_schema={"type": "object", "properties": {"ticket": {"type": "string"}}},
    output_schema={"type": "object", "required": ["category"], "properties": {"category": {"type": "string"}}})
c.functions.create_version(fn["id"], snapshot={
    "sections": [{"type": "role", "title": "Role",
                  "content": "Put this support ticket in one category (billing, bug or other): {ticket}"}],
    "impl": "prompt", "model_binding": {"model": "mock-small", "temperature": 0.2}, "tools": []},
    message="first version")

c.datasets.create(fn["id"], name="tickets", rows=[
    {"input": {"ticket": "I was charged twice this month"}, "expected": {"category": "billing"}},
    {"input": {"ticket": "The export button does nothing"}, "expected": {"category": "bug"}},
    {"input": {"ticket": "Where is your office?"}, "expected": {"category": "other"}},
    {"input": {"ticket": "My invoice has the wrong address"}, "expected": {"category": "billing"}}])

c.eval.scores.create(fn["id"], name="right category", tier="det",
                     config={"rule": "exact_match", "field": "category"})
print("ready:", fn["slug"])
```

**TypeScript** — save as `setup.mjs` and run `node setup.mjs`:

```js
// Run once. Creates the function (the contract and prompt we keep), 4 test rows, and one check.
import { Client } from "fnai";

const c = new Client({ token: process.env.FNAI_TOKEN });          // your personal token (fnp_…)
if (process.env.FNAI_SPACE) await c.useSpace(process.env.FNAI_SPACE);   // only if you are not in your default space
const SLUG = process.env.SLUG ?? "support_bot";

const fn = await c.functions.create({
  slug: SLUG, name: "Support bot",
  input_schema: { type: "object", properties: { ticket: { type: "string" } } },
  output_schema: { type: "object", required: ["category"], properties: { category: { type: "string" } } },
});
await c.functions.createVersion(fn.id, {
  message: "first version",
  snapshot: {
    sections: [{ type: "role", title: "Role", content: "Put this support ticket in one category (billing, bug or other): {ticket}" }],
    impl: "prompt", model_binding: { model: "mock-small", temperature: 0.2 }, tools: [],
  },
});
await c.datasets.create(fn.id, { name: "tickets", rows: [
  { input: { ticket: "I was charged twice this month" }, expected: { category: "billing" } },
  { input: { ticket: "The export button does nothing" }, expected: { category: "bug" } },
  { input: { ticket: "Where is your office?" }, expected: { category: "other" } },
  { input: { ticket: "My invoice has the wrong address" }, expected: { category: "billing" } },
] });
await c.eval.scores.create(fn.id, { name: "right category", tier: "det", config: { rule: "exact_match", field: "category" } });
console.log("ready:", fn.slug);
```

If you are not in your default space, set `FNAI_SPACE=<space name>` for this step only. The space key already knows its space.

## 4. Trace a real call, then score your AI

`classify` stands in for your AI. Replace it with your own code (an OpenAI call, an agent, anything that takes a question and
returns an answer). Two things happen:

- each real call is **traced**, so you can see it in the app;
- `eval` runs your AI on every test question, sends the answers back, and waits for the score.

**Python** — save as `run.py` and run `python run.py`:

```python
import os
import fnai

def classify(ticket: str) -> dict:                   # YOUR AI goes here (a stand-in that needs no key)
    t = ticket.lower()
    if "charge" in t or "invoice" in t:
        return {"category": "billing"}
    if "button" in t or "error" in t:
        return {"category": "bug"}
    return {"category": "other"}

# trace each real call (the space key names no function, so say which)
fnai.init_tracing(api_key=os.environ["FNAI_SPACE_KEY"], function=os.environ.get("SLUG", "support_bot"))
def handle(ticket: str) -> dict:
    with fnai.trace(input={"ticket": ticket}) as t:
        answer = classify(ticket)
        t.output(answer)
    return answer
print(handle("I was charged twice"))
fnai.flush_traces()

# score your AI on the test rows
result = fnai.eval(os.environ.get("SLUG", "support_bot"), dataset="tickets",
                   task=lambda row: classify(row["ticket"]), token=os.environ["FNAI_SPACE_KEY"])
print(result["pass_rate"], result["url"])
```

**TypeScript** — save as `run.mjs` and run `node run.mjs`:

```js
import { evaluate, flushTraces, initTracing, trace } from "fnai";

const classify = (ticket) => {                       // YOUR AI goes here (a stand-in that needs no key)
  const t = ticket.toLowerCase();
  if (t.includes("charge") || t.includes("invoice")) return { category: "billing" };
  if (t.includes("button") || t.includes("error")) return { category: "bug" };
  return { category: "other" };
};

// trace each real call (the space key names no function, so say which)
initTracing({ apiKey: process.env.FNAI_SPACE_KEY, function: process.env.SLUG ?? "support_bot" });
async function handle(ticket) {
  return trace({ ticket }, async (t) => {
    const answer = classify(ticket);
    t.output(answer);
    return answer;
  });
}
console.log(await handle("I was charged twice"));
await flushTraces();

// score your AI on the test rows
const result = await evaluate(process.env.SLUG ?? "support_bot", {
  dataset: "tickets", task: (row) => classify(row.ticket), token: process.env.FNAI_SPACE_KEY,
});
console.log(result.pass_rate, result.url);
```

You should see something like:

```
{'category': 'billing'}
1.0 https://…/fn/277?panel=experiments
```

`1.0` means all four test questions passed. Change `classify` so it gets one wrong, run it again, and the number drops.

## 5. See it in the app

- **Experiments** (the link printed above): your test run, labelled **Run by your own code**. Open a failed check to see
  which question failed and why.
- **Monitor**: the traced call, next to any hosted calls, with its input, output and steps.

## What next

| I want to… | Read |
|---|---|
| keep my prompts in Functional AI and fetch the live version | [prompts-for-your-own-code.md](prompts-for-your-own-code.md) |
| record each model call and tool call my agent makes | [tracing.md](tracing.md) |
| score the **behaviour** of an agent with tools ("asked for approval first") | [declared-tools.md](declared-tools.md) |
| keep scoring real traffic and be told when quality drops | [live-scoring.md](live-scoring.md) |
| run the tests from CI without the SDK | [user-run-experiments.md](user-run-experiments.md) |
| understand which key to use where | [keys.md](keys.md) |
| start from a complete agent | [OpenAI agent](../examples/openai_agent.py), [LangGraph agent](../examples/langgraph_agent.py) |

## If something goes wrong

- **`401` or "not signed in"**: the key is missing, mistyped or revoked. Make a new one.
- **`403 … lacks 'evals.run'`** (or another permission): the space key was made without that box ticked. Make a new key.
- **"no function with slug …"**: the function is in another space. A space key only sees its own space; for your personal token set `FNAI_SPACE`.
- **"this function has no checks yet"**: the setup step did not finish. Run it again with a new function name.
