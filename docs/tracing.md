# Tracing: record what your code does

A **trace** is one call of your AI: what it was given, what it answered, and the steps in between (each model call, each tool
call). Send one per call and it shows in the app's **Monitor**, can be scored with a Score Set ([live-scoring.md](live-scoring.md)),
and is searchable. Nothing runs on our side: we store what you send and hide any field your function marks as sensitive.

Tracing never gets in your way. A problem in it (no network, a refused trace, a bug here) is logged and **never raised into your
code**, never slows it down (traces go out from a background queue, with retries), and never changes what your code returns.
Turning the network off loses traces and nothing else.

## Python

```python
import fnai

fnai.init_tracing(api_key="fns_…", function="support_bot")   # or set FNAI_API_KEY; see "Which key" below

client = fnai.wrap_openai(OpenAI())          # every model call becomes a recorded step (or fnai.wrap_anthropic)

@fnai.traced                                  # every call of this tool becomes a recorded step
def search(query): ...

def handle(question):
    with fnai.trace(input={"question": question}) as t:      # one call of your agent
        answer = run_agent(question)
        t.output({"answer": answer})
    return answer

fnai.flush_traces()                           # before a short script or a serverless handler ends
```

Inside the `with` block you can also add things yourself:

| Call | Records |
|---|---|
| `t.output(value)` | the answer (an object, or any value, which is wrapped as `{"output": value}`) |
| `t.tool(name, args, output=…, error=…)` | one tool call |
| `t.llm(model, tokens_in, tokens_out, latency_ms, cost)` | one model call |
| `t.cost(usd)` | extra cost you know about |
| `t.error(problem)` | the call failed, and why. A block that raises is recorded as failed and your exception goes on unchanged. |

`fnai.trace(input=…, version_id=…)` links the call to a prompt version; `fnai.trace(input=…, prompt=p)` does the same from a prompt
you fetched with `get_prompt` ([prompts-for-your-own-code.md](prompts-for-your-own-code.md)). Leave both out if you have none.
`fnai.trace_stats()` says how many were sent, dropped and are waiting. `FNAI_TRACING=0` switches it all off without touching code.

## TypeScript

```ts
import { initTracing, trace, traced, wrapOpenAI, flushTraces } from "fnai";

initTracing({ apiKey: "fns_…", function: "support_bot" });     // or set FNAI_API_KEY
const openai = wrapOpenAI(new OpenAI());
const search = traced(async (query: string) => { /* … */ });

const answer = await trace({ question }, async (t) => {
  const a = await runAgent(question);
  t.output({ answer: a });
  return a;
});
await flushTraces();
```

`t.tool(...)`, `t.llm(...)`, `t.cost(...)` and `t.error(...)` match Python. `trace(input, fn, { versionId, prompt, function })`.

## Go

Go has no helpers yet: build the trace and send it with `Function.ReportTrace(trace)`. The envelope is in
[self-hosted-traces.md](self-hosted-traces.md).

## Which key

- A **function key** (`fnk_…`) belongs to one function, so there is nothing else to say: `init_tracing(api_key="fnk_…")`.
- A **space key** (`fns_…`, with **Send traces**) covers every function in the space, so say which one: `function="support_bot"` at
  `init_tracing`, or `function=` on each `trace()`. See [keys.md](keys.md).

## Limits

At most **500 steps** per trace and 1 MB per trace; long text is cut at 20,000 characters per value; the queue holds 1,000 traces
(more are dropped, and counted in `trace_stats()`). A trace with the same `trace_id` twice is stored once.
