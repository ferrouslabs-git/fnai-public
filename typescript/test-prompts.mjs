// FNA-REQ-531: getPrompt() / Prompt.render() / trace({ prompt }). Mocked fetch like the other SDK tests.
import assert from "node:assert/strict";
import { clearPromptCache, flushTraces, getPrompt, initTracing, trace } from "./dist/fnai.js";

const BASE = "http://fake.local";
process.env.FNAI_API_KEY = "fnk_test";

const prompt = (version = 3, over = {}) => ({
  function: "ticket_summary", name: "Ticket summary", version, version_id: 100 + version, live: true, artifact_hash: "h".repeat(64),
  template: "Summarise in one sentence: {ticket_text}. Tone: {tone}. Keep {unknown_left} as is.",
  substitution: { syntax: "curly_lowercase_v1", vars: ["ticket_text", "tone", "unknown_left"] },
  input_schema: { type: "object" }, output_schema: { type: "object" }, model: "gpt-4o-mini", settings: { temperature: 0.2 }, ...over,
});

function fake(answers) {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url: String(url), auth: opts.headers?.Authorization ?? opts.headers?.authorization });
    const [status, body] = answers[Math.min(calls.length - 1, answers.length - 1)];
    return { status, ok: status < 300, text: async () => JSON.stringify(body), json: async () => body, headers: new Map() };
  };
  return calls;
}

// fetches the live version with the key and exposes its parts
{
  clearPromptCache();
  const calls = fake([[200, prompt()]]);
  const p = await getPrompt("ticket_summary", { baseUrl: BASE });
  assert.equal(calls[0].url, `${BASE}/v1/prompts/ticket_summary?version=live`);
  assert.equal(calls[0].auth, "Bearer fnk_test");
  assert.deepEqual([p.version, p.versionId, p.live, p.model, p.temperature], [3, 103, true, "gpt-4o-mini", 0.2]);
  assert.deepEqual(p.variables, ["ticket_text", "tone", "unknown_left"]);
  // render fills blanks, leaves a name with no value as written
  assert.equal(p.render({ ticket_text: "Printer on fire", tone: "calm" }),
    "Summarise in one sentence: Printer on fire. Tone: calm. Keep {unknown_left} as is.");
  assert.ok(p.render({ ticket_text: 5, tone: "x" }).includes("5"));
}

// a named version is asked for by number; the answer is kept for a while; cacheSeconds 0 always asks
{
  clearPromptCache();
  const calls = fake([[200, prompt(2, { live: false })]]);
  const p = await getPrompt("ticket_summary", { baseUrl: BASE, version: 2 });
  assert.ok(calls[0].url.endsWith("version=2")); assert.equal(p.live, false);
  await getPrompt("ticket_summary", { baseUrl: BASE, version: 2 });
  assert.equal(calls.length, 1);
  await getPrompt("ticket_summary", { baseUrl: BASE, version: 2, cacheSeconds: 0 });
  assert.equal(calls.length, 2);
}

// a struggling server gives the last copy; a 404 is an error; no copy yet means the failure is raised
{
  clearPromptCache();
  fake([[200, prompt()], [503, { detail: "busy" }], [404, { detail: "not found" }]]);
  const first = await getPrompt("ticket_summary", { baseUrl: BASE, cacheSeconds: 0 });
  const again = await getPrompt("ticket_summary", { baseUrl: BASE, cacheSeconds: 0 });
  assert.equal(again, first);
  await assert.rejects(getPrompt("ticket_summary", { baseUrl: BASE, cacheSeconds: 0 }), (e) => e.status === 404);
  clearPromptCache();
  fake([[503, { detail: "busy" }]]);
  await assert.rejects(getPrompt("ticket_summary", { baseUrl: BASE }), (e) => e.status === 503);
}

// no key is a clear error
{
  clearPromptCache();
  delete process.env.FNAI_API_KEY;
  await assert.rejects(getPrompt("ticket_summary", { baseUrl: BASE }), /FNAI_API_KEY/);
  process.env.FNAI_API_KEY = "fnk_test";
}

// a trace names the prompt version it used; an explicit versionId still wins
{
  clearPromptCache();
  fake([[200, prompt()]]);
  const p = await getPrompt("ticket_summary", { baseUrl: BASE });
  const sent = [];
  globalThis.fetch = async (url, opts) => { sent.push(JSON.parse(opts.body)); return { status: 201, ok: true, json: async () => ({}) }; };
  initTracing({ apiKey: "fnk_test", baseUrl: BASE });
  await trace({ a: 1 }, async (t) => { t.output({ b: 2 }); }, { prompt: p });
  await trace({}, async () => {}, { prompt: p, versionId: 7 });
  assert.equal(await flushTraces(5000), true);
  assert.deepEqual(sent.map((t) => t.version_id), [103, 7]);
}

console.log("test-prompts: ok");
