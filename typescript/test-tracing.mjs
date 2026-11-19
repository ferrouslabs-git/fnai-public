// FNA-REQ-532: the SDK's tracing helpers — trace(), traced(), wrapOpenAI / wrapAnthropic, and the background sender.
// Mocked HTTP like the other SDK tests: a fake global `fetch` records what is sent.
import assert from "node:assert/strict";
import { currentTrace, flushTraces, initTracing, trace, traced, traceStats, wrapAnthropic, wrapOpenAI } from "./dist/fnai.js";
import { _tuning } from "./dist/tracing.js";

const BASE = "http://fake.local";
_tuning.retryAfterMs = [0, 0, 0];                          // no real waiting in tests

function fakeFetch(statuses = []) {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    const body = JSON.parse(opts.body);
    calls.push({ url: String(url), headers: opts.headers, body });
    const status = statuses.length ? statuses.shift() : 201;
    return { status, ok: status < 300, json: async () => ({}) };
  };
  return calls;
}
const start = (statuses) => { const calls = fakeFetch(statuses); initTracing({ apiKey: "fnk_test", baseUrl: BASE }); return calls; };
const sent = async (calls) => { assert.equal(await flushTraces(5000), true); return calls.map((c) => c.body); };

// a trace is sent in the background with the key and the right shape
{
  const calls = start();
  const result = await trace({ q: "hi" }, async (t) => { t.output({ a: "yo" }); return 42; });
  assert.equal(result, 42);                                 // trace() returns what your code returned
  const [tr] = await sent(calls);
  assert.equal(calls[0].url, `${BASE}/v1/traces`);
  assert.equal(calls[0].headers.Authorization, "Bearer fnk_test");
  assert.deepEqual([tr.source, tr.schema_version, tr.success, tr.error], ["self_hosted", 1, true, null]);
  assert.deepEqual([tr.input, tr.output, tr.steps], [{ q: "hi" }, { a: "yo" }, []]);
  assert.equal(tr.trace_id.length, 32);
  assert.ok(!("version_id" in tr) && !("function_id" in tr));
  assert.ok(tr.created_at && tr.latency_ms >= 0);
}

// a named version is sent; non-object input/output are wrapped
{
  const calls = start();
  await trace("just text", (t) => t.output(42), { versionId: 7 });
  const [tr] = await sent(calls);
  assert.equal(tr.version_id, 7);
  assert.deepEqual([tr.input, tr.output], [{ input: "just text" }, { output: 42 }]);
}

// a block that throws is recorded as failed and the error goes on
{
  const calls = start();
  await assert.rejects(() => trace({ q: 1 }, () => { throw new TypeError("boom"); }), /boom/);
  const [tr] = await sent(calls);
  assert.equal(tr.success, false);
  assert.equal(tr.error, "TypeError: boom");
}

// error() marks the call failed without throwing; currentTrace() is the one you are inside
{
  const calls = start();
  assert.equal(currentTrace(), undefined);
  await trace({}, (t) => { assert.equal(currentTrace(), t); t.error("the model said no"); });
  assert.equal(currentTrace(), undefined);
  const [tr] = await sent(calls);
  assert.deepEqual([tr.success, tr.error], [false, "the model said no"]);
}

// steps, tokens, cost and model are totalled; at most 500 steps
{
  const calls = start();
  await trace({}, (t) => {
    t.llm("gpt-4o-mini", 10, 5, 100, 0.001);
    t.llm("gpt-4o", 20, 8, 200, 0.002);
    t.tool("search", { q: "x" }, { n: 1 }, null, 5);
    t.cost(0.5);
    for (let i = 0; i < 600; i++) t.tool("x", { i });
  });
  const [tr] = await sent(calls);
  assert.deepEqual([tr.tokens_in, tr.tokens_out, tr.model], [30, 13, "gpt-4o"]);
  assert.ok(Math.abs(tr.cost - 0.503) < 1e-9);
  assert.equal(tr.steps.length, 500);
}

// values JSON cannot hold become text, not errors
{
  const calls = start();
  const circular = {}; circular.self = circular;
  await trace({ big: 10n, when: new Date(0), m: new Map([["a", 1]]), circular }, (t) => t.output({ ok: true }));
  const [tr] = await sent(calls);
  assert.equal(tr.input.big, "10");
  assert.deepEqual(tr.input.m, { a: 1 });
}

// traced(): sync and async tools become tool steps; the value is unchanged; errors are recorded and thrown again
{
  const calls = start();
  const search = traced((query, limit = 3) => ({ hits: Array(limit).fill(query) }), { name: "search" });
  const fetchIt = traced(async function fetchIt(url) { await null; return url.toUpperCase(); });
  const boom = traced(() => { throw new RangeError("missing"); }, { name: "lookup" });
  await trace({}, async (t) => {
    assert.deepEqual(search("cats", 2), { hits: ["cats", "cats"] });
    assert.equal(await fetchIt("a"), "A");
    assert.throws(() => boom(1), /missing/);
    t.output({ a: 1 });
  });
  const [tr] = await sent(calls);
  const [s1, s2, s3] = tr.steps;
  assert.deepEqual([s1.type, s1.tool, s1.ok, s1.output], ["tool", "search", true, { hits: ["cats", "cats"] }]);
  assert.deepEqual([s2.tool, s2.output], ["fetchIt", "A"]);
  assert.deepEqual([s3.tool, s3.ok], ["lookup", false]);
  assert.match(s3.error, /missing/);
}

// outside a trace a traced function just runs and nothing is sent
{
  const calls = start();
  assert.equal(traced((n) => n * 2)(4), 8);
  assert.deepEqual(await sent(calls), []);
}

// wrapOpenAI / wrapAnthropic: LLM steps; the provider's own promise comes back untouched
{
  const calls = start();
  const completions = { create: (kw) => Promise.resolve({ model: kw.model, usage: { prompt_tokens: 11, completion_tokens: 4 }, choices: ["hi"] }) };
  const openai = { chat: { completions } };
  assert.equal(wrapOpenAI(wrapOpenAI(openai)), openai);       // same client; wrapping twice records once
  const anthropic = wrapAnthropic({ messages: { create: async (kw) => ({ model: kw.model, usage: { input_tokens: 7, output_tokens: 3 } }) } });
  const resp = await trace({}, async (t) => {
    const r = await openai.chat.completions.create({ model: "gpt-4o-mini", messages: [] });
    await anthropic.messages.create({ model: "claude-sonnet-4-5", messages: [] });
    t.output({ a: 1 });
    return r;
  });
  assert.deepEqual(resp.choices, ["hi"]);
  const [tr] = await sent(calls);
  assert.equal(tr.steps.length, 2);
  assert.deepEqual([tr.steps[0].type, tr.steps[0].model, tr.steps[0].tokens_in, tr.steps[0].tokens_out], ["llm", "gpt-4o-mini", 11, 4]);
  assert.deepEqual([tr.steps[1].model, tr.steps[1].tokens_in, tr.steps[1].tokens_out], ["claude-sonnet-4-5", 7, 3]);
  assert.deepEqual([tr.tokens_in, tr.tokens_out], [18, 7]);
}

// a provider error is not swallowed; a call outside a trace still works and records nothing; odd clients are left alone
{
  const calls = start();
  const bad = wrapOpenAI({ chat: { completions: { create: async () => { throw new Error("rate limited"); } } } });
  await trace({}, async () => { await assert.rejects(() => bad.chat.completions.create({ model: "m" }), /rate limited/); });
  const ok = wrapOpenAI({ chat: { completions: { create: async () => ({ choices: ["hi"] }) } } });
  assert.deepEqual((await ok.chat.completions.create({ model: "m" })).choices, ["hi"]);
  const odd = {};
  assert.equal(wrapOpenAI(odd), odd);
  assert.equal(wrapAnthropic(odd), odd);
  const [tr] = await sent(calls);
  assert.equal(tr.steps.length, 0);                           // the failed call made no step, the outside call none
}

// two calls running at the same time keep their own steps
{
  const calls = start();
  const tool = traced(async (who) => { await new Promise((r) => setTimeout(r, who === "a" ? 20 : 5)); return who; }, { name: "wait" });
  await Promise.all(["a", "b"].map((who) => trace({ who }, async (t) => { await tool(who); t.output({ who }); })));
  const trs = await sent(calls);
  for (const tr of trs) {
    assert.equal(tr.steps.length, 1);
    assert.equal(tr.steps[0].args.args[0], tr.input.who);     // each trace holds its own tool step
  }
}

// fail-open: refused for good = dropped once, never retried or thrown
{
  const calls = start([422]);
  await trace({}, (t) => t.output({ a: 1 }));
  await sent(calls);
  assert.equal(calls.length, 1);
  assert.equal(traceStats().dropped, 1);
}

// fail-open: a struggling server is retried then succeeds; gives up after the last retry
{
  const calls = start([503, 500]);
  await trace({}, () => {});
  await sent(calls);
  assert.equal(calls.length, 3);
  assert.deepEqual(traceStats(), { sent: 1, dropped: 0, waiting: 0 });
  const again = start(Array(10).fill(500));
  await trace({}, () => {});
  await sent(again);
  assert.equal(again.length, 4);
  assert.equal(traceStats().dropped, 1);
}

// fail-open: no network loses the trace and nothing else
{
  globalThis.fetch = async () => { throw new TypeError("fetch failed"); };
  initTracing({ apiKey: "fnk_test", baseUrl: BASE });
  const out = await trace({ q: 1 }, (t) => { t.output({ a: 1 }); return "my own code ran"; });
  assert.equal(out, "my own code ran");
  assert.equal(await flushTraces(5000), true);
  assert.equal(traceStats().dropped, 1);
}

// fail-open: a full queue drops instead of blocking
{
  let release;
  const gate = new Promise((r) => { release = r; });
  globalThis.fetch = async () => { await gate; return { status: 201 }; };
  initTracing({ apiKey: "fnk_test", baseUrl: BASE, maxQueue: 1 });
  const t0 = Date.now();
  for (let i = 0; i < 20; i++) await trace({}, () => {});
  assert.ok(Date.now() - t0 < 1000);                          // never waited on the network
  assert.ok(traceStats().dropped >= 1);
  release();
  await flushTraces(5000);
}

// switches: no key sends nothing; the key can come from the environment; FNAI_TRACING=0 turns it off
{
  const calls = fakeFetch();
  delete process.env.FNAI_API_KEY;
  initTracing({ baseUrl: BASE });
  await trace({ q: 1 }, (t) => t.output({ a: 1 }));
  assert.equal(calls.length, 0);
  assert.deepEqual(traceStats(), { sent: 0, dropped: 0, waiting: 0 });

  process.env.FNAI_API_KEY = "fnk_env";
  initTracing({ baseUrl: BASE });
  await trace({}, () => {});
  await flushTraces(5000);
  assert.equal(calls[0].headers.Authorization, "Bearer fnk_env");
  process.env.FNAI_TRACING = "0";
  initTracing({ baseUrl: BASE });
  await trace({}, () => {});
  await flushTraces(5000);
  assert.equal(calls.length, 1);                              // the second was never sent
  delete process.env.FNAI_API_KEY; delete process.env.FNAI_TRACING;
}


// FNA-REQ-534: a space key (fns_…) covers every function in the space, so each trace names its function; a function key does not
{
  const calls = fakeFetch();
  initTracing({ apiKey: "fns_test", baseUrl: BASE, function: "ticket_summary" });
  await trace({ a: 1 }, async (t) => { t.output({ b: 2 }); });
  await trace({ a: 1 }, async () => {}, { function: "other_fn" });            // a trace's own name wins
  assert.equal(await flushTraces(5000), true);
  assert.deepEqual(calls.map((c) => c.body.function), ["ticket_summary", "other_fn"]);
  const calls2 = fakeFetch();
  initTracing({ apiKey: "fnk_test", baseUrl: BASE, function: "ticket_summary" });
  await trace({ a: 1 }, async () => {}, { function: "ticket_summary" });
  assert.equal(await flushTraces(5000), true);
  assert.ok(!("function" in calls2[0].body));                                    // an older server refuses unknown fields
}

console.log("tracing: ok");
