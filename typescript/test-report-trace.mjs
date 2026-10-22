// REQ-202: Function.reportTrace posts a trace from the customer's own servers to /v1/traces.
// Mocked HTTP, like the other SDK tests: fake global `fetch`, assert on what is sent.
import assert from "node:assert/strict";
import { Function as FnaiFunction, FnaiError } from "./dist/fnai.js";

const BASE = "http://fake.local";

function fakeFetch(handler) {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url: String(url), ...opts });
    return handler(String(url), opts, calls.length);
  };
  return calls;
}
const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300, status, statusText: String(status),
  json: async () => body, text: async () => JSON.stringify(body),
});

const fn = () => new FnaiFunction("demo", { apiKey: "fnk_x", baseUrl: BASE });

// posts to the ingest path with the function's key, and returns the server's answer
{
  const calls = fakeFetch(() => jsonResponse(201, { trace_id: "a".repeat(32), duplicate: false }));
  const out = await fn().reportTrace({ trace_id: "a".repeat(32), function_id: 3, version_id: 4 });
  assert.deepEqual(out, { trace_id: "a".repeat(32), duplicate: false });
  assert.equal(calls[0].url, `${BASE}/v1/traces`);
  assert.equal(calls[0].method, "POST");
  assert.match(JSON.stringify(calls[0].headers), /fnk_x/);
}

// fills in what is left out
{
  const calls = fakeFetch(() => jsonResponse(201, { trace_id: "x", duplicate: false }));
  await fn().reportTrace({ function_id: 3, version_id: 4 });
  const sent = JSON.parse(calls[0].body);
  assert.equal(sent.schema_version, 1);
  assert.equal(sent.source, "self_hosted");
  assert.equal(sent.trace_id.length, 32);
  assert.match(sent.created_at, /^\d{4}-\d{2}-\d{2}T/);
  assert.equal(sent.function_id, 3);
  assert.equal(sent.version_id, 4);
}

// keeps what the caller set
{
  const calls = fakeFetch(() => jsonResponse(200, { trace_id: "x", duplicate: true }));
  const out = await fn().reportTrace({ trace_id: "b".repeat(32), created_at: "2026-10-20T09:00:00Z", function_id: 3 });
  const sent = JSON.parse(calls[0].body);
  assert.equal(sent.trace_id, "b".repeat(32));
  assert.equal(sent.created_at, "2026-10-20T09:00:00Z");
  assert.equal(out.duplicate, true);
}

// two traces without an id get different ids
{
  const calls = fakeFetch(() => jsonResponse(201, { trace_id: "x", duplicate: false }));
  await fn().reportTrace({ function_id: 3 });
  await fn().reportTrace({ function_id: 3 });
  assert.notEqual(JSON.parse(calls[0].body).trace_id, JSON.parse(calls[1].body).trace_id);
}

// a refusal reaches the caller with its status and reason
{
  fakeFetch(() => jsonResponse(422, { detail: "source: a reported trace must be 'self_hosted'" }));
  await assert.rejects(() => fn().reportTrace({ function_id: 3 }),
    (e) => e instanceof FnaiError && e.status === 422 && /self_hosted/.test(String(e.message)));
}

console.log("report-trace: ok");
