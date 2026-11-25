// FNA-REQ-530: evaluate() / client.eval.run() — a dataset run through your own code. Mocked fetch like the other SDK tests.
import assert from "node:assert/strict";
import { evaluate, evalTuning, traced } from "./dist/fnai.js";
import { currentTrace } from "./dist/fnai.js";

const BASE = "http://fake.local";
evalTuning.retryMs = [0, 0, 0];
evalTuning.pollMs = 0;
process.env.FNAI_TOKEN = "fnp_test";

function fake({ rows = 3, passRate = 1, received = [], putStatuses = [], finishStatuses = [] } = {}) {
  const st = { put: {}, created: [], finished: 0, auth: [], polls: 0 };
  globalThis.fetch = async (url, opts) => {
    const u = new URL(String(url));
    const p = u.pathname, m = opts.method;
    st.auth.push(opts.headers?.Authorization ?? opts.headers?.authorization);
    const body = opts.body ? JSON.parse(opts.body) : {};
    const reply = (obj, status = 200) => ({ status, ok: status < 300, text: async () => JSON.stringify(obj), json: async () => obj, headers: new Map() });
    if (m === "GET" && p === "/api/functions/5/datasets") return reply([{ id: 1, name: "old" }, { id: 2, name: "tickets" }]);
    if (m === "GET" && p === "/api/functions/5/versions") return reply([{ id: 502, number: 2 }, { id: 503, number: 3 }]);
    if (m === "GET" && p === "/api/functions/5/scores") return reply([{ id: 11 }, { id: 12 }]);
    if (m === "POST" && p === "/api/datasets/2/snapshot") return reply({ id: 77 });
    if (m === "POST" && p === "/api/functions/5/experiments") { st.created.push(body); return reply({ id: 9 }); }
    if (m === "GET" && p === "/api/experiments/9/user-run/rows")
      return reply({ rows: Array.from({ length: rows }, (_, i) => ({ row: i + 1, input: { q: `question ${i + 1}` } })), repeats: 1, received });
    if (m === "PUT" && p.startsWith("/api/experiments/9/user-run/rows/")) {
      const status = putStatuses.length ? putStatuses.shift() : 200;
      if (status < 300) st.put[Number(p.split("/").pop())] = body;
      return reply(status < 300 ? { ok: true } : { detail: "busy" }, status);
    }
    if (m === "POST" && p === "/api/experiments/9/user-run/finish" && finishStatuses.length) return reply({ detail: "busy" }, finishStatuses.shift());
    if (m === "POST" && p === "/api/experiments/9/user-run/finish") { st.finished++; return reply({ id: 9, status: "pending" }); }
    if (m === "GET" && p === "/api/experiments/9") { st.polls++; return reply({ id: 9, status: st.polls >= 2 ? "completed" : "running" }); }
    if (m === "GET" && p === "/api/functions/5/score-results") return reply([{ score: "has answer", pass_rate: passRate }]);
    return reply({ detail: `nope ${m} ${p}` }, 404);
  };
  return st;
}

// `version` is a version NUMBER: the run pins that version's id, and an unknown number is a clear error
{
  const st = fake();
  await evaluate(5, { task: async () => ({ answer: "x" }), dataset: "tickets", scoreSet: 4, version: 3, baseUrl: BASE });
  assert.deepEqual(st.created[0].version_ids, [503]);        // not [3]: a number is not an id
  const st2 = fake();
  await assert.rejects(() => evaluate(5, { task: async () => ({ answer: "x" }), dataset: "tickets", scoreSet: 4, version: 9, baseUrl: BASE }),
    /no version 9 \(it has: 2, 3\)/);
  assert.deepEqual(st2.created, []);
}

// every row goes through the task, results are sent, the experiment is finished and summarised
{
  const st = fake();
  const r = await evaluate(5, { task: async (i) => ({ answer: i.q.toUpperCase() }), dataset: "tickets", scoreSet: 4, baseUrl: BASE });
  assert.deepEqual(st.created, [{ mode: "user_run", snapshot_id: 77, repeats: 1, version_ids: [], plan_id: 4 }]);
  assert.deepEqual(Object.keys(st.put), ["1", "2", "3"]);
  assert.deepEqual(st.put[2].output, { answer: "QUESTION 2" });
  assert.equal(st.put[2].success, true);
  assert.equal(st.finished, 1);
  assert.deepEqual([r.experiment_id, r.rows, r.task_failed, r.not_run, r.pass_rate], [9, 3, 0, 0, 1]);
  assert.equal(r.status, "completed");        // it waited for the scoring
  assert.ok(st.polls >= 2);
  assert.ok(r.url.endsWith("/fn/5?panel=experiments"));
}

// a task that throws fails that row only
{
  const st = fake();
  const r = await evaluate(5, { task: (i) => { if (i.q.endsWith("2")) throw new Error("boom"); return { ok: true }; }, dataset: "tickets", scoreSet: 4, baseUrl: BASE });
  assert.equal(r.task_failed, 1);
  assert.equal(st.put[2].success, false);
  assert.match(st.put[2].error, /Error: boom/);
  assert.equal(st.put[2].output, null);
  assert.equal(st.put[1].success && st.put[3].success, true);
  assert.equal(st.finished, 1);
}

// steps from traced tools and llm calls travel with the row; a plain return is wrapped
{
  const st = fake();
  const lookup = traced(async (order) => ({ status: "paid" }), { name: "lookup" });
  await evaluate(5, {
    task: async (i) => { await lookup(i.q); currentTrace().llm("gpt-4o-mini", 100, 10, 0, 0.001); return "done"; },
    dataset: "tickets", scoreSet: 4, baseUrl: BASE,
  });
  const b = st.put[1];
  assert.deepEqual(b.steps.map((s) => s.type), ["tool", "llm"]);
  assert.equal(b.steps[0].tool, "lookup");
  assert.deepEqual([b.tokens_in, b.tokens_out, b.model], [100, 10, "gpt-4o-mini"]);
  assert.deepEqual(b.output, { output: "done" });
}

// concurrency runs every row exactly once; resume skips rows already received
{
  const st = fake();
  const seen = [];
  await evaluate(5, { task: (i) => { seen.push(i.q); return { a: 1 }; }, dataset: "tickets", scoreSet: 4, concurrency: 3, baseUrl: BASE });
  assert.deepEqual(seen.sort(), ["question 1", "question 2", "question 3"]);
  const st2 = fake({ received: [{ row: 1, repeat: 1 }] });
  const seen2 = [];
  const r = await evaluate(5, { task: (i) => { seen2.push(i.q); return { a: 1 }; }, resume: 9, baseUrl: BASE });
  assert.deepEqual(seen2, ["question 2", "question 3"]);
  assert.equal(r.sent, 3);
}

// a busy server is retried; without a score set every check is used; a missing token is a clear error
{
  const st = fake({ putStatuses: [503, 503] });
  await evaluate(5, { task: () => ({ a: 1 }), dataset: "tickets", baseUrl: BASE });
  assert.deepEqual(st.created[0].score_ids, [11, 12]);
  assert.equal(Object.keys(st.put).length, 3);
  delete process.env.FNAI_TOKEN;
  await assert.rejects(evaluate(5, { task: () => ({}), baseUrl: BASE }), /FNAI_TOKEN/);
  process.env.FNAI_TOKEN = "x";
  await assert.rejects(evaluate(5, { task: () => ({}), dataset: "nope", baseUrl: BASE }), /no dataset named/);
}

// a busy server when finishing is retried and the run still completes
{
  const st = fake({ finishStatuses: [503, 503] });
  const r = await evaluate(5, { task: () => ({ a: 1 }), dataset: "tickets", scoreSet: 4, baseUrl: BASE });
  assert.deepEqual([r.status, r.sent, st.finished], ["completed", 3, 1]);
}

console.log("test-eval: ok");
