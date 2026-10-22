// The step-by-step builder: checks as you add, YAML print, the local draft file, saveYaml,
// loadYaml, rubric and memory, tools, upload, deploy curl, run with findings, compare.
// Mocked HTTP, matching this SDK's convention (see test-function-draft.mjs).
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Client, DefinitionError, FunctionDraft, Function as FnaiFunction } from "./dist/fnai.js";

const BASE = "http://fake.local";
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "fnai-ts-"));
process.env.FNAI_DRAFTS_DIR = tmp();      // keep draft files from tests out of the working folder

const json = (status, body) => ({
  ok: status >= 200 && status < 300, status, statusText: String(status),
  json: async () => body, text: async () => JSON.stringify(body),
});

/** A tiny fake server: functions, versions, submit, tools, deploy, experiments. */
function server(state = {}) {
  state.calls = [];
  state.functions ??= [];
  state.versions ??= {};
  state.tools ??= [{ name: "ocr", status: "active" }, { name: "web_search", status: "active" },
                   { name: "human_approval", status: "active" }, { name: "issue_refund", status: "active" }];
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url).replace(BASE, "");
    const body = typeof opts.body === "string" ? JSON.parse(opts.body) : opts.body;
    state.calls.push({ method: opts.method, url: u, body });
    let m;
    if (u === "/api/functions" && opts.method === "POST") {
      const fn = { id: 60 + state.functions.length, input_schema: {}, output_schema: {}, ...body };
      state.functions.push(fn);
      return json(200, fn);
    }
    if (u.split("?")[0] === "/api/functions" && opts.method === "GET") return json(200, state.functions);
    if ((m = u.match(/^\/api\/functions\/(\d+)$/)) && opts.method === "GET") return json(200, state.functions.find((f) => f.id === +m[1]));
    if ((m = u.match(/^\/api\/functions\/(\d+)\/versions$/)) && opts.method === "GET") return json(200, state.versions[m[1]] ?? []);
    if ((m = u.match(/^\/api\/functions\/(\d+)\/submit$/))) {
      const list = (state.versions[m[1]] ??= []);
      const v = { id: 500 + list.length, number: list.length + 1, status: "draft", ...body };
      list.push(v);
      return json(200, v);
    }
    if ((m = u.match(/^\/api\/versions\/(\d+)$/)) && opts.method === "PUT") {
      if (state.frozenOnPut) return json(409, { detail: "versions are immutable once deployed" });
      for (const list of Object.values(state.versions)) {
        const v = list.find((x) => x.id === +m[1]);
        if (v) { Object.assign(v, body); return json(200, v); }
      }
    }
    if ((m = u.match(/^\/api\/functions\/(\d+)\/versions$/)) && opts.method === "POST") {
      const list = (state.versions[m[1]] ??= []);
      const v = { id: 900 + list.length, number: Math.max(0, ...list.map((x) => x.number)) + 1, status: "draft", ...body };
      list.push(v);
      return json(200, v);
    }
    if ((m = u.match(/^\/api\/versions\/(\d+)\/yaml$/)) && opts.method === "GET") {
      const n = +m[1] - 100;
      const text = `meta:\n  function: fn\n  version: ${n}\nname: from the server v${n}\n`;
      return { ok: true, status: 200, statusText: "200", json: async () => ({}), text: async () => text };
    }
    if (u === "/api/tools" && opts.method === "GET") return json(200, state.tools);
    if ((m = u.match(/^\/api\/functions\/(\d+)\/deploy$/))) return json(200, { id: 9, status: "active", version_id: body.version_id });
    if (state.extra) { const r = state.extra(u, opts, body); if (r) return r; }
    throw new Error(`unexpected request: ${opts.method} ${u}`);
  };
  return state;
}
const newClient = (extra = {}) => new Client({ token: "fnp_x", baseUrl: BASE, ...extra });
const throwsDef = (fn, re) => assert.throws(fn, (e) => e instanceof DefinitionError && (!re || re.test(e.message)));

// ── checks as you add ───────────────────────────────────────────────────────
{
  server();
  const d = await newClient().functions.new("X", { saveTo: false });
  throwsDef(() => d.addInput("a", { type: "text" }), /type 'text' is not valid/);
  throwsDef(() => d.addInput("b", { minimun: 1 }), /did you mean 'minimum'/);
  throwsDef(() => d.addInput("c", { type: "string", minimum: 1 }), /needs type number or integer/);
  throwsDef(() => d.addInput("d", { type: "integer", minimum: 10, maximum: 5 }), /greater than maximum/);
  throwsDef(() => d.addInput("e", { type: "string", enum: [1, 2] }), /not of type string/);
  throwsDef(() => d.addInput("f", { type: "string", pattern: "(" }), /not a valid regular expression/);
  throwsDef(() => d.addInput("g", { type: "string", format: "phone" }), /format 'phone' is not valid/);
  throwsDef(() => d.addInput("h", { type: "array" }), /arrays need 'items'/);
  throwsDef(() => d.addInput("i", { type: "string", length: { unit: "tokens" } }), /only valid on output/);
  throwsDef(() => d.addInput("j", { type: "file" }), /file fields need media/);
  throwsDef(() => d.addInput("k", { type: "string", min_items: 1 }), /needs type array/);
  throwsDef(() => d.addOutput("l", { type: "file", media: "document" }), /input-only/);
  d.addInput("a");
  throwsDef(() => d.addInput("a"), /already defined/);
  d.addOutput("summary", { length: { unit: "tokens", target: 60, range: [40, 80] }, required: true });
  console.log("OK: every bad field is rejected as it is added, with a message that says what to fix");
}

// ── required list, nested fields, checkInput ────────────────────────────────
{
  server();
  const d = await newClient().functions.new("X", { saveTo: false });
  d.addInput("text", { required: true }).addInput("rating", { type: "integer", minimum: 1, maximum: 5 });
  d.addInput("email", { format: "email" }).addInput("tags", { type: "array", items: { type: "string" }, min_items: 1 });
  assert.deepEqual(d.checkInput({ text: "hi", rating: 3, email: "a@b.co", tags: ["x"] }), []);
  const p = d.checkInput({ rating: 9, email: "nope", tags: [] });
  assert.ok(p.includes("missing required field 'text'"));
  assert.ok(p.includes("field 'rating' must be at most 5, got 9"));
  assert.ok(p.includes("field 'email' is not a valid email"));
  assert.ok(p.includes("field 'tags' must have at least 1 items, got 0"));
  console.log("OK: checkInput runs the rules locally");
}

// ── examples, tools, guards, rubric, memory ─────────────────────────────────
{
  server();
  const d = await newClient().functions.new("X", { saveTo: false });
  throwsDef(() => d.addExample({ polarity: "negative", output: { o: "bad" } }), /needs a note/);
  throwsDef(() => d.addExample({ polarity: "negative", note: "why" }), /needs output or badValue/);
  throwsDef(() => d.addExample({ input: { a: 1 } }), /needs both input and output/);
  throwsDef(() => d.addExample({ polarity: "maybe", input: {}, output: {} }), /polarity must be/);
  d.addExample({ polarity: "negative", output: { o: "bad" }, note: "too vague" });
  for (const opts of [{ maxCalls: 0 }, { effect: "dangerous" }, { requires: "no operator here" }]) throwsDef(() => d.addTool("t", opts));
  d.addTool("t");
  throwsDef(() => d.addTool("t"), /already added/);
  d.addRubric("Names the problem.");
  throwsDef(() => d.addRubric("  "), /can't be empty/);
  throwsDef(() => d.addRubric("Names the problem."), /already added/);
  throwsDef(() => d.setMemory({ per: "everyone" }), /per must be/);
  throwsDef(() => d.setMemory({ per: "caller", remember: "plan" }), /list of non-empty sentences/);
  d.setMemory({ per: "caller", never: ["card numbers"] }).setMemory({ per: "space" });
  assert.match(d.toYaml(), /rubric:\n {2}- "Names the problem."/);
  assert.match(d.toYaml(), /memory:\n {2}per: space\n$/);
  console.log("OK: examples, tool options, guards, rubric and memory are checked");
}

// ── printing: YAML + what is missing ────────────────────────────────────────
{
  server();
  const d = await newClient().functions.new("Ticket Triage", { saveTo: false });
  d.addInput("ticket_text", { required: true });
  d.addOutput("summary", { required: true }).addOutput("urgency", { type: "integer", minimum: 1, maximum: 5, required: true });
  assert.ok(d.missing().required.some((m) => m.includes("no steps")));
  d.addPrompt("You triage tickets.").addStep("Read.").addStep("Score.");
  const text = String(d);
  assert.match(text, /name: Ticket Triage/);
  assert.match(text, /ticket_text: \{ type: string, required: true \}/);
  assert.match(text, /flow:\n {4}- "Read."/);
  assert.match(text, /role: "You triage tickets."/);
  assert.match(text, /# Missing \(required\): nothing/);
  assert.match(text, /no examples/);
  assert.equal(d[Symbol.for("nodejs.util.inspect.custom")](), text);
  console.log("OK: String(fn) shows the YAML and what is missing");
}

// ── submit lists every problem; sends a v2 document ─────────────────────────
{
  const s = server();
  const d = await newClient().functions.new("X", { saveTo: false });
  await assert.rejects(() => d.submit(), (e) => e instanceof DefinitionError && e.problems[0] === "no output fields — addOutput(...)" && e.problems[1] === "no steps — addStep(...)");
  d.addOutput("o", { required: true }).addStep("s").addTool("web_serch");
  await assert.rejects(() => d.submit(), /did you mean 'web_search'/);
  const d2 = await newClient().functions.new("Y", { saveTo: false });
  d2.addInput("amount", { type: "number" }).addOutput("o", { required: true }).addStep("s");
  d2.addTool("human_approval").addTool("issue_refund", { requires: "human_approval.returned.approved == true && input.amount > 100", maxCalls: 1, effect: "side_effecting" });
  await d2.submit();
  const body = s.calls.filter((c) => c.url.endsWith("/submit")).pop().body;
  assert.equal(body.snapshot.implementation.tools[1].requires, "human_approval.returned.approved == true && input.amount > 100");
  const d3 = await newClient().functions.new("Z", { saveTo: false });
  d3.addOutput("o", { required: true }).addStep("s").addTool("issue_refund", { requires: "human_approval.called == true" });
  await assert.rejects(() => d3.submit(), /doesn't declare/);
  const d4 = await newClient().functions.new("W", { saveTo: false });
  d4.addInput("a").addOutput("o", { required: true }).addStep("s").addExample({ input: { zzz: 1 }, output: { o: "x" } });
  await assert.rejects(() => d4.submit(), /'zzz' is not declared/);
  console.log("OK: submit reports every problem at once and sends a definition the server accepts");
}

// ── load() takes schemas and steps from the latest version ──────────────────
{
  const version = { id: 500, number: 1, status: "draft",
    input_schema: { type: "object", properties: { a: { type: "string" } }, required: ["a"] },
    output_schema: { type: "object", properties: { o: { type: "string" } }, required: ["o"] },
    snapshot: { rubric: ["r1"], memory: { per: "space" }, implementation: { execution: { mode: "single_shot" }, flow: ["Do it."], sections: { role: "r" } } } };
  const s = server({ functions: [{ id: 60, slug: "x", name: "X", input_schema: {}, output_schema: {} }], versions: { 60: [version] } });
  const d = await newClient().loadDraft(60, { saveTo: false });
  d.addExample({ input: { a: "x" }, output: { o: "y" } });
  await d.submit();
  const body = s.calls.filter((c) => c.url.endsWith("/submit")).pop().body;
  assert.deepEqual(body.output_schema.required, ["o"]);
  assert.deepEqual(body.snapshot.implementation.flow, ["Do it."]);
  assert.deepEqual(body.snapshot.rubric, ["r1"]);
  d.addTool("web_search");
  assert.match(d.toYaml(), /mode: agent/);   // a loaded single_shot function flips to agent once a tool is added
  console.log("OK: loadDraft brings back schemas, steps, rubric and memory");
}

// ── the local draft file ────────────────────────────────────────────────────
{
  server();
  const dir = tmp();
  const file = path.join(dir, "triage.yaml");
  const d = await newClient().functions.new("Ticket Triage", { saveTo: file });
  assert.match(fs.readFileSync(file, "utf8"), /name: Ticket Triage/);
  assert.match(fs.readFileSync(file, "utf8"), /meta:\n {2}function: ticket_triage/);
  d.addInput("ticket_text", { required: true });
  assert.match(fs.readFileSync(file, "utf8"), /ticket_text: \{ type: string, required: true \}/);
  for (const change of [() => d.addPrompt("You triage."), () => d.addStep("Read."), () => d.addRubric("r"), () => d.setMemory({ per: "space" }),
                        () => d.addTool("t"), () => d.addSection("constraints", "c"), () => d.addExample({ input: { ticket_text: "x" }, output: {} })]) {
    const before = fs.readFileSync(file, "utf8"); change(); assert.notEqual(before, fs.readFileSync(file, "utf8"));
  }
  const before = fs.readFileSync(file, "utf8");
  throwsDef(() => d.addInput("ticket_text"));
  assert.equal(fs.readFileSync(file, "utf8"), before);     // a rejected call leaves the file alone
  console.log("OK: the draft file is created with the function and follows every change");
}

// ── where the file goes ─────────────────────────────────────────────────────
{
  server();
  const dir = tmp();
  const a = await newClient().functions.new("One", { saveTo: path.join(dir, "mine") });
  assert.ok(fs.existsSync(path.join(dir, "mine", `${a.slug}.yaml`)));                 // a folder: named after the function
  const b = await newClient({ draftsFolder: path.join(dir, "all") }).functions.new("Two");
  assert.ok(fs.existsSync(path.join(dir, "all", `${b.slug}.yaml`)));                 // the client-wide folder
  const c = await newClient({ draftsFolder: path.join(dir, "all2") }).functions.new("Three", { saveTo: path.join(dir, "x.yaml") });
  assert.ok(fs.existsSync(path.join(dir, "x.yaml")) && !fs.existsSync(path.join(dir, "all2")));  // saveTo wins
  const d = await newClient().functions.new("Four");
  assert.ok(fs.existsSync(path.join(process.env.FNAI_DRAFTS_DIR, `${d.slug}.yaml`)));            // the default
  const e = await newClient().functions.new("Five", { saveTo: false });
  assert.equal(e.draftPath, null);
  console.log("OK: the user chooses where the file goes (saveTo, draftsFolder, default, off)");
}

// ── submit deletes the draft file, saveYaml copy stays; saveYaml forms ──────
{
  server();
  const dir = tmp();
  const file = path.join(dir, "draft.yaml");
  const copy = path.join(dir, "copy.yaml");
  const d = await newClient().functions.new("Ticket Triage", { saveTo: file });
  d.addInput("a").addOutput("o", { required: true }).addStep("s");
  d.saveYaml(copy);
  await d.submit();
  assert.ok(!fs.existsSync(file) && fs.existsSync(copy));
  const out = d.saveYaml(path.join(dir, "deep", "er", "f.yaml"));
  assert.ok(fs.existsSync(out));
  assert.equal(path.basename(d.saveYaml(path.join(dir, "folder"))), `${d.slug}.yaml`);
  console.log("OK: submit deletes the draft file, a saveYaml copy stays");
}

// ── a write problem warns once and never stops the script ───────────────────
{
  server();
  const dir = tmp();
  const blocker = path.join(dir, "blocker");
  fs.writeFileSync(blocker, "a file, not a folder");
  const warnings = [];
  const orig = console.warn;
  console.warn = (m) => warnings.push(String(m));
  const d = await newClient().functions.new("X", { saveTo: path.join(blocker, "x.yaml") });
  d.addInput("a").addInput("b");
  console.warn = orig;
  assert.equal(warnings.filter((w) => w.includes("draft file")).length, 1);
  assert.throws(() => d.saveYaml(path.join(blocker, "y.yaml")), /couldn't write/);   // an explicit save fails loudly
  console.log("OK: a write problem warns once; saveYaml is allowed to fail loudly");
}

// ── loadYaml: the same function back ────────────────────────────────────────
{
  const dir = tmp();
  const s = server();
  const client = newClient({ draftsFolder: path.join(dir, "dr") });
  const d = await client.functions.new("Ticket Triage", { saveTo: path.join(dir, "d.yaml") });
  d.addInput("ticket_text", { required: true }).addInput("amount", { type: "number", minimum: 0 });
  d.addOutput("summary", { required: true }).addOutput("urgency", { type: "integer", minimum: 1, maximum: 5, required: true });
  d.addPrompt("You triage tickets.").addSection("definitions", "Urgency: 1 to 5.").addSection("constraints", "Never invent facts.").addSection("edge_cases", "Empty ticket: urgency 1.");
  d.addStep("Read.").addStep("Score.");
  d.addExample({ input: { ticket_text: "crash" }, output: { summary: "crash", urgency: 4 } });
  d.addExample({ polarity: "negative", output: { summary: "idk", urgency: 3 }, note: "vague" });
  d.addTool("human_approval", { maxCalls: 2 }).addRubric("Names the problem.").setMemory({ per: "caller", never: ["card numbers"] });
  const loaded = await client.loadYaml(path.join(dir, "d.yaml"), { saveTo: false });
  assert.equal(loaded.toYaml(), d.toYaml());
  assert.deepEqual(loaded.snapshot(), d.snapshot());
  // by name, from the drafts folder
  const e = await client.functions.new("Other Fn");
  e.addInput("a");
  assert.match((await client.loadYaml("Other Fn", { saveTo: false })).toYaml(), /a: \{ type: string \}/);
  await assert.rejects(() => client.loadYaml("nothing here"), /no YAML file/);
  console.log("OK: loadYaml gives back the same function (by path or by name)");

  // loading a copy never overwrites or deletes it
  const copy = path.join(dir, "copy.yaml");
  d.saveYaml(copy);
  const text = fs.readFileSync(copy, "utf8");
  const l2 = await client.loadYaml(copy);
  l2.addStep("Another step.");
  await l2.submit();
  assert.equal(fs.readFileSync(copy, "utf8"), text);
  console.log("OK: loading a copy never overwrites or deletes it");

  // refuses a file it would have to drop parts from; applies the same checks as add calls
  fs.writeFileSync(path.join(dir, "bad.yaml"), "name: X\ninput:\n  a: { type: text }\n");
  await assert.rejects(() => client.loadYaml(path.join(dir, "bad.yaml")), /type 'text' is not valid/);
  console.log("OK: loadYaml applies the same checks");
}

// ── loadYaml does not create the function; an existing one is continued ─────
{
  const dir = tmp();
  const s = server();
  const client = newClient();
  const file = path.join(dir, "x.yaml");
  fs.writeFileSync(file, "meta:\n  function: late_one\nname: Late One\ninput:\n  a: { type: string }\noutput:\n  o: { type: string, required: true }\nimplementation:\n  flow:\n    - \"s\"\n");
  const loaded = await client.loadYaml(file, { saveTo: false });
  assert.equal(loaded.id, undefined);
  assert.equal(s.calls.filter((c) => c.method === "POST").length, 0);   // nothing created yet
  s.functions.push({ id: 77, slug: "late_one", name: "Late One", input_schema: {}, output_schema: {} });
  const again = await client.loadYaml(file, { saveTo: false });
  assert.equal(again.id, 77);                                           // an existing function is continued
  console.log("OK: loadYaml creates nothing; an existing function is continued");
}

// ── look a function up by its slug ──────────────────────────────────────────
{
  const version = { id: 500, number: 1, input_schema: { type: "object", properties: {} }, output_schema: { type: "object", properties: {} },
    snapshot: { implementation: { flow: ["x"] } } };
  const s = server({ functions: [{ id: 60, slug: "invoice_extractor", name: "Invoice Extractor", input_schema: {}, output_schema: {} },
                                 { id: 61, slug: "ticket_triage", name: "Ticket Triage", input_schema: {}, output_schema: {} }],
                     versions: { 60: [version], 61: [version] } });
  const c = newClient();
  const bySlug = await c.functions.get("ticket_triage");
  assert.equal(bySlug.id, 61);
  assert.match(String(bySlug), /name: Ticket Triage/);
  const callsBefore = s.calls.length;
  assert.equal((await c.functions.get(60)).id, 60);
  assert.ok(!s.calls.slice(callsBefore).some((x) => x.url.split("?")[0] === "/api/functions"));   // an id never triggers a lookup
  await assert.rejects(() => c.functions.get("invoice_extracter"), (e) => e.status === 404
    && /no function with slug "invoice_extracter"/.test(e.message) && /did you mean 'invoice_extractor'/.test(e.message) && /ticket_triage/.test(e.message));
  await assert.rejects(() => c.functions.get("Invoice Extractor"), /no function with slug/);          // a display name is not matched
  await assert.rejects(() => c.functions.get("60"), /pass the number 60/);
  for (const bad of [0, -1, 1.5, null, undefined]) await assert.rejects(() => c.functions.get(bad));  // bad ids are still refused locally
  assert.equal((await c.loadDraft("invoice_extractor", { saveTo: false })).id, 60);
  s.functions.length = 0;
  await assert.rejects(() => c.functions.get("anything"), /no functions in this space yet/);
  console.log("OK: functions.get / loadDraft accept a slug; an unknown one says so, suggests and lists");
}

// ── load any version: which version a save goes to ──────────────────────────
{
  const mk = (n, status, flow) => ({ id: 100 + n, number: n, status,
    input_schema: { type: "object", properties: { a: { type: "string" } } },
    output_schema: { type: "object", properties: { o: { type: "string" } }, required: ["o"] },
    snapshot: { implementation: { flow, sections: { role: `role of v${n}` } } } });
  const setup = () => server({ functions: [{ id: 60, slug: "fn", name: "Fn", input_schema: {}, output_schema: {} }],
                              versions: { 60: [mk(1, "approved", ["v1 step"]), mk(2, "approved", ["v2 step"]), mk(3, "draft", ["v3 step"])] } });
  const flowOf = (s, n) => s.versions[60].find((v) => v.number === n).snapshot.implementation.flow;
  const writes = (s, method, re) => s.calls.filter((c) => c.method === method && re.test(c.url));

  // no version: the latest, saved by the server's rule
  let s = setup();
  let d = await newClient().loadDraft("fn", { saveTo: false });
  assert.deepEqual(d.flow, ["v3 step"]);
  await d.submit();
  assert.equal(writes(s, "POST", /\/submit$/).length, 1);

  // a frozen version: its own content, saved as a NEW version, nothing else touched
  s = setup();
  d = await newClient().loadDraft("fn", { version: 1, saveTo: false });
  assert.deepEqual(d.flow, ["v1 step"]);
  assert.match(String(d), /^# based on v1 — saving creates a new version/);
  d.addStep("a new step");
  const created = await d.submit("from v1");
  assert.equal(created.number, 4);
  assert.equal(writes(s, "POST", /\/functions\/60\/versions$/)[0].body.parent_id, 101);   // recorded as based on v1
  assert.equal(writes(s, "PUT", /./).length, 0);
  assert.deepEqual(flowOf(s, 3), ["v3 step"]);                                              // the unfinished v3 is safe
  assert.deepEqual(flowOf(s, 1), ["v1 step"]);
  // after the first save the new draft is updated in place
  d.addStep("more");
  await d.submit();
  assert.equal(writes(s, "POST", /\/functions\/60\/versions$/).length, 1);
  assert.deepEqual(writes(s, "PUT", /./).map((c) => c.url), [`/api/versions/${created.id}`]);

  // a draft: edited in place (and not the latest draft)
  s = setup();
  s.versions[60].push({ id: 104, number: 4, status: "draft", snapshot: { implementation: { flow: ["v4 step"] } } });
  d = await newClient().loadDraft("fn", { version: 3, saveTo: false });
  assert.match(String(d), /^# editing draft v3 — saving updates it/);
  d.addStep("edited");
  assert.equal((await d.submit()).number, 3);
  assert.deepEqual(writes(s, "PUT", /./).map((c) => c.url), ["/api/versions/103"]);
  assert.deepEqual(flowOf(s, 4), ["v4 step"]);

  // a missing or bad version
  s = setup();
  await assert.rejects(() => newClient().loadDraft("fn", { version: 9 }), (e) => e.status === 404 && /no version 9/.test(e.message) && /versions: 1, 2, 3/.test(e.message));
  for (const bad of [0, -1, 1.5, "1"]) await assert.rejects(() => newClient().loadDraft("fn", { version: bad }), /version must be/);

  // a draft deployed meanwhile
  s = setup();
  d = await newClient().loadDraft("fn", { version: 3, saveTo: false });
  d.addStep("x");
  s.frozenOnPut = true;
  await assert.rejects(() => d.submit(), /has been deployed since you loaded it/);
  assert.ok(d.flow.includes("x"));

  // the draft file remembers the base, so a stopped script cannot overwrite
  s = setup();
  s.functions.length = 0; s.functions.push({ id: 60, slug: "fn", name: "Fn", input_schema: {}, output_schema: {} });
  const dir = tmp();
  const file = path.join(dir, "d.yaml");
  d = await newClient().loadDraft("fn", { version: 1, saveTo: file });
  d.addStep("x");
  assert.match(fs.readFileSync(file, "utf8"), /^meta:\n {2}function: fn\n {2}based_on: 1\n/);
  const again = await newClient().loadYaml(file, { saveTo: false });          // a fresh run after the script stopped
  assert.match(String(again), /^# based on v1/);
  await again.submit();
  assert.equal(writes(s, "POST", /\/functions\/60\/versions$/).length, 1);
  assert.equal(writes(s, "POST", /\/submit$/).length, 0);
  console.log("OK: loadDraft({ version }) — a frozen version becomes a new one, a draft is edited in place, v3 is never overwritten");
}

// ── submit({ newVersion: true }) branches without touching the version it grew from ──
{
  const mk = (n, status) => ({ id: 100 + n, number: n, status, input_schema: { type: "object", properties: { a: { type: "string" } } },
    output_schema: { type: "object", properties: { o: { type: "string" } }, required: ["o"] },
    snapshot: { implementation: { flow: [`v${n} step`] } } });
  const setup = (...statuses) => server({ functions: [{ id: 60, slug: "fn", name: "Fn", input_schema: {}, output_schema: {} }],
    versions: { 60: statuses.map((st, i) => mk(i + 1, st)) } });
  const posts = (s) => s.calls.filter((c) => c.method === "POST" && /\/functions\/60\/versions$/.test(c.url));
  let s = setup("draft");
  let d = await newClient().loadDraft("fn", { saveTo: false });
  d.addStep("a new idea");
  const v2 = await d.submit("try this", { newVersion: true });
  assert.equal(v2.number, 2);
  assert.equal(posts(s)[0].body.parent_id, 101);
  assert.deepEqual(s.versions[60].find((v) => v.number === 1).snapshot.implementation.flow, ["v1 step"]);   // untouched
  assert.equal(s.calls.filter((c) => c.method === "PUT" || /\/submit$/.test(c.url)).length, 0);
  d.addStep("y"); await d.submit();                                                                         // plain: updates v2
  assert.deepEqual(s.calls.filter((c) => c.method === "PUT").map((c) => c.url), [`/api/versions/${v2.id}`]);
  d.addStep("z"); assert.equal((await d.submit("", { newVersion: true })).number, 3);                      // branch again
  assert.deepEqual(posts(s).map((c) => c.body.parent_id), [101, v2.id]);
  s = setup("approved", "approved", "draft");
  d = await newClient().loadDraft("fn", { version: 1, saveTo: false });
  d.addStep("x");
  assert.equal((await d.submit("", { newVersion: true })).number, 4);
  assert.equal(posts(s)[0].body.parent_id, 101);
  s = setup("draft");
  d = await newClient().loadDraft("fn", { saveTo: false });
  d.addStep("x"); await d.submit();                                                                        // without the flag: in place
  assert.equal(s.calls.filter((c) => /\/submit$/.test(c.url)).length, 1);
  assert.equal(posts(s).length, 0);
  console.log("OK: submit({ newVersion: true }) branches; the version it grew from is untouched");
}

// ── every version keeps its YAML ─────────────────────────────────────────────
{
  const mk = (n, status) => ({ id: 100 + n, number: n, status, input_schema: { type: "object", properties: { a: { type: "string" } } },
    output_schema: { type: "object", properties: { o: { type: "string" } }, required: ["o"] }, snapshot: { implementation: { flow: [`v${n} step`] } } });
  const setup = (...statuses) => server({ functions: [{ id: 60, slug: "fn", name: "Fn", input_schema: {}, output_schema: {} }],
    versions: { 60: statuses.map((st, i) => mk(i + 1, st)) } });
  const root = tmp();
  const drafts = path.join(root, "drafts");
  const list = (dir) => (fs.existsSync(dir) ? fs.readdirSync(dir).filter((f) => f.endsWith(".yaml")).sort() : []);
  const client = (extra = {}) => newClient({ draftsFolder: drafts, ...extra });

  // each thing you edit has its own file
  setup("approved", "approved", "draft");
  const a = await client().loadDraft("fn", { version: 1 }); a.addStep("x");
  const b = await client().loadDraft("fn", { version: 3 }); b.addStep("y");
  const c0 = await client().loadDraft("fn"); c0.addStep("z");
  assert.deepEqual(list(drafts), ["fn.from-v1.yaml", "fn.v3.yaml", "fn.yaml"]);
  assert.ok(fs.readFileSync(path.join(drafts, "fn.from-v1.yaml"), "utf8").includes("v1 step"));
  assert.ok(!fs.readFileSync(path.join(drafts, "fn.v3.yaml"), "utf8").includes("v1 step"));      // they did not overwrite each other

  // saving turns the working file into that version's copy
  fs.rmSync(drafts, { recursive: true, force: true });
  setup("approved");
  let d = await client().loadDraft("fn", { version: 1 }); d.addStep("a new step");
  const v = await d.submit("from v1");
  assert.deepEqual(list(drafts), []);                                                              // the working file is gone...
  const record = path.join(root, "versions", "fn", `v${v.number}.yaml`);                          // ...and became the copy
  assert.ok(fs.existsSync(record));
  assert.ok(fs.readFileSync(record, "utf8").startsWith(`meta:\n  function: fn\n  version: ${v.number}\n`));
  assert.ok(fs.readFileSync(record, "utf8").includes("a new step"));

  // editing a draft again refreshes its copy
  setup("approved", "draft");
  d = await client().loadDraft("fn", { version: 2 }); d.addStep("first"); await d.submit();
  const v2 = path.join(root, "versions", "fn", "v2.yaml");
  assert.ok(fs.readFileSync(v2, "utf8").includes("first"));
  d.addStep("second"); await d.submit();
  assert.ok(fs.readFileSync(v2, "utf8").includes("second"));

  // copies off: the file is just removed. saveTo: false: no files at all
  fs.rmSync(root, { recursive: true, force: true });
  setup("approved");
  d = await client({ keepVersions: false }).loadDraft("fn", { version: 1 }); d.addStep("x"); await d.submit();
  assert.deepEqual(list(drafts), []);
  assert.ok(!fs.existsSync(path.join(root, "versions")));
  fs.rmSync(root, { recursive: true, force: true });
  setup("approved");
  d = await client().loadDraft("fn", { version: 1, saveTo: false }); d.addStep("x"); await d.submit();
  assert.ok(!fs.existsSync(drafts) && !fs.existsSync(path.join(root, "versions")));

  // where the copies go
  setup("approved");
  d = await newClient({ draftsFolder: path.join(root, "work") }).loadDraft("fn", { version: 1 }); d.addStep("x");
  const vv = await d.submit();
  assert.ok(fs.existsSync(path.join(root, "work", "versions", "fn", `v${vv.number}.yaml`)));       // drafts folder not called "drafts"
  setup("approved");
  d = await client({ versionsFolder: path.join(root, "history") }).loadDraft("fn", { version: 1 }); d.addStep("x");
  const v3 = await d.submit();
  assert.ok(fs.existsSync(path.join(root, "history", "fn", `v${v3.number}.yaml`)));

  // loadYaml finds a draft by name, asks when unsure, and a saved copy knows which version it is
  fs.rmSync(root, { recursive: true, force: true });
  setup("approved", "draft");
  d = await client().loadDraft("fn", { version: 2 }); d.addStep("y");
  assert.match(String(await client().loadYaml("fn", { saveTo: false })), /^# editing draft v2/);
  const e = await client().loadDraft("fn", { version: 1 }); e.addStep("x");
  await assert.rejects(() => client().loadYaml("fn"), /2 drafts match/);
  setup("approved");
  d = await client().loadDraft("fn", { version: 1 }); d.addStep("x"); const saved = await d.submit();
  const again = await client().loadYaml(path.join(root, "versions", "fn", `v${saved.number}.yaml`), { saveTo: false });
  assert.ok(String(again).split("\n")[0].includes(`v${saved.number}`));

  // exportYaml: every version from the server
  setup("approved", "approved", "draft");
  const paths = await client({ versionsFolder: path.join(root, "vers") }).exportYaml("fn");
  assert.deepEqual(paths.map((p) => path.basename(p)), ["v1.yaml", "v2.yaml", "v3.yaml"]);
  assert.ok(fs.readFileSync(path.join(root, "vers", "fn", "v2.yaml"), "utf8").includes("from the server v2"));
  assert.equal((await client().exportYaml(60, path.join(root, "elsewhere"))).length, 3);
  setup();
  await assert.rejects(() => client().exportYaml(60), /no versions yet/);
  console.log("OK: every version keeps its YAML: one working file per edit, a copy on save, an export from the server");
}

// ── nothing is lost when loading a version ───────────────────────────────────
{
  const SNAP = { name: "X", needs: ["tool_calling"], rubric: ["r"], memory: { per: "caller" },
    input: { url: { type: "string", required: true }, page_text: { type: "string", derived: true, sensitive: true }, secret: { type: "string", sensitive: true } },
    output: { summary: { type: "string", required: true, sensitive: true } },
    implementation: { execution: { mode: "agent" }, preprocessors: [{ tool: "web_fetch", from: "url", into: "page_text" }],
      flow: ["f"], sections: { role: "r", definitions: ["d"] }, tools: [{ name: "web_fetch", max_calls: 2 }, { server: "crm", max_calls: 20 }] } };
  const version = (snap = SNAP) => ({ id: 1, number: 1, status: "approved",
    input_schema: { type: "object", properties: { url: { type: "string" }, secret: { type: "string" } }, required: ["url"] },
    output_schema: { type: "object", properties: { summary: { type: "string" } }, required: ["summary"] }, snapshot: snap });
  const setup = (snap) => server({ functions: [{ id: 60, slug: "x", name: "X", description: "", input_schema: {}, output_schema: {} }],
    versions: { 60: [version(snap)] }, tools: [{ name: "web_fetch", status: "active" }, { name: "issue_refund", status: "active" }] });

  let s = setup();
  let d = await newClient().loadDraft("x", { saveTo: false });
  d.addStep("another step");
  const snap = d.snapshot();
  assert.deepEqual(snap.needs, ["tool_calling"]);
  assert.deepEqual(snap.implementation.preprocessors, SNAP.implementation.preprocessors);
  assert.deepEqual(snap.implementation.tools, SNAP.implementation.tools);                 // incl. the server: entry
  assert.deepEqual(snap.input.page_text, { type: "string", derived: true, sensitive: true });
  assert.equal(snap.input.secret.sensitive, true);
  assert.equal(snap.output.summary.sensitive, true);
  assert.deepEqual(Object.keys(snap.input), ["url", "page_text", "secret"]);              // original order
  assert.deepEqual(snap.implementation.flow, ["f", "another step"]);
  // a derived field is not a caller input
  await d.submit();
  const body = s.calls.filter((c) => c.url.endsWith("/submit")).pop().body;
  assert.ok(!("page_text" in body.input_schema.properties));
  assert.deepEqual(d.checkInput({ url: "https://x" }), []);
  // a loaded name cannot be added again
  assert.throws(() => d.addInput("page_text"), DefinitionError);
  // printing shows them
  const text = String(d);
  for (const want of ["needs: [tool_calling]", "preprocessors:", "{ tool: web_fetch, from: url, into: page_text }", "derived: true", "server: crm"]) assert.ok(text.includes(want), want);
  // sensitive added through the builder is kept too
  s = setup();
  const e = await newClient().functions.new("Y", { saveTo: false });
  e.addInput("card", { sensitive: true }).addOutput("o", { required: true }).addStep("s");
  assert.equal(e.snapshot().input.card.sensitive, true);
  // a guard on a derived input, next to a server entry, is not a false alarm
  s = setup();
  d = await newClient().loadDraft("x", { saveTo: false });
  d.addTool("issue_refund", { requires: 'input.page_text == "x"' });
  await d.submit();
  // it survives a stopped script
  s = setup();
  const dir = tmp();
  d = await newClient().loadDraft("x", { saveTo: path.join(dir, "d.yaml") });
  d.addStep("another step");
  const again = await newClient().loadYaml(path.join(dir, "d.yaml"), { saveTo: false });
  assert.deepEqual(again.snapshot(), d.snapshot());
  // a version in the old editor format is refused, not blanked
  setup({ impl: "prompt", sections: [{ type: "role", content: "hi" }] });
  await assert.rejects(() => newClient().loadDraft("x", { saveTo: false }), /older editor format/);
  // functions.get prints them too
  setup();
  const rec = String(await newClient().functions.get(60));
  assert.ok(rec.includes("needs: [tool_calling]") && rec.includes("preprocessors:"));
  console.log("OK: loading a version keeps needs, preprocessors, derived, sensitive and server tools");
}

// ── Function.check names the field without running ──────────────────────────
{
  globalThis.fetch = async (url) => json(200, { input_schema: { type: "object", required: ["text"],
    properties: { text: { type: "string" }, rating: { type: "integer", maximum: 5 } } } });
  const f = new FnaiFunction("wt", { apiKey: "fnk_x", baseUrl: BASE });
  assert.deepEqual(await f.check({ text: "hi", rating: 3 }), []);
  assert.deepEqual(await f.check({ rating: 9 }), ["missing required field 'text'", "field 'rating' must be at most 5, got 9"]);
  assert.deepEqual(await f.check({ text: 5 }), ["field 'text' must be a string, got number"]);
  console.log("OK: Function.check names the bad field without running");
}

// ── functions.get prints the latest version ─────────────────────────────────
{
  const version = { id: 500, number: 1, input_schema: { type: "object", properties: { a: { type: "string" } }, required: ["a"] },
    output_schema: { type: "object", properties: { o: { type: "string" } }, required: ["o"] },
    snapshot: { implementation: { execution: { mode: "single_shot" }, flow: ["Do it."], sections: { role: "r" }, tools: [] } } };
  server({ functions: [{ id: 60, slug: "x", name: "X", input_schema: {}, output_schema: {} }], versions: { 60: [version] } });
  const fn = await newClient().functions.get(60);
  assert.equal(fn.id, 60);
  assert.match(String(fn), /flow:\n {4}- "Do it."/);
  assert.match(String(fn), /role: "r"/);
  server();
  const rec = await newClient().functions.create({ slug: "s", name: "S", input_schema: { type: "object", properties: { a: { type: "string" } }, required: ["a"] }, output_schema: { type: "object", properties: {} } });
  assert.equal(rec.slug, "s");
  assert.match(String(rec), /a: \{ type: string, required: true \}/);
  assert.match(String(rec), /no output fields/);
  console.log("OK: functions.get / create print as YAML, with the fields still on the object");
}

// ── upload, deploy curl, run with findings, compare, tools ──────────────────
{
  const dir = tmp();
  const csv = path.join(dir, "cases.csv");
  fs.writeFileSync(csv, "q,a\nhi,yo\n");
  const s = server({ functions: [{ id: 3, slug: "hi", name: "Hi", input_schema: { type: "object", properties: { a: { type: "string" } } } }] });
  s.versions[3] = [];
  s.extra = (u, opts, body) => {
    if (u === "/api/datasets/7/upload") { s.form = opts.body; return json(200, { added: 1, skipped: 0, errors: [] }); }
    if (u === "/api/experiments/9/run") return json(200, { id: 9, status: "completed" });
    if (u === "/api/experiments/9/findings") return json(200, [{ title: "t" }]);
    const ex = u.match(/^\/api\/experiments\/(\d+)$/);
    if (ex) return json(200, s.exps[ex[1]]);
    if (u === "/api/tools" && opts.method === "POST") return json(200, { id: 1, ...body });
  };
  const c = newClient();
  const up = await c.datasets.upload(7, csv, { mapping: { q: "question" }, expectedMapping: { a: "answer" } });
  assert.deepEqual(up, { added: 1, skipped: 0, errors: [] });
  assert.equal(s.form.get("mapping"), JSON.stringify({ q: "question" }));
  assert.equal(s.form.get("file").name, "cases.csv");
  await assert.rejects(() => c.datasets.upload(7, path.join(dir, "x.txt"), { mapping: { a: "b" } }), /unsupported file type/);
  const dep = await c.deployments.deploy(3, 4);
  assert.equal(dep.endpoint, `${BASE}/v1/run/hi`);
  assert.match(dep.curl, /curl -X POST http:\/\/fake\.local\/v1\/run\/hi/);
  assert.match(dep.curl, /"input": \{"a":"\.\.\."\}/);
  const run = await c.eval.experiments.run(9);
  assert.deepEqual(run.findings, [{ title: "t" }]);
  assert.equal((await c.eval.experiments.run(9, { includeFindings: false })).findings, undefined);
  const mk = (id, pass, cost) => ({ id, name: `e${id}`, status: "completed", cost,
    results: { n_rows: 10, latency: { p95: 2000 }, per_version: { [id]: { number: id } }, checks: [{ check_name: "valid", pass_rate: pass }] } });
  s.exps = { 1: mk(1, 0.6, 1.0), 2: mk(2, 0.9, 2.0) };
  const cmp = await c.eval.experiments.compare([1, 2]);
  const rows = Object.fromEntries(cmp.rows.map((r) => [r.metric, r]));
  assert.deepEqual(cmp.experiments.map((e) => e.version), ["v1", "v2"]);
  assert.equal(rows["Pass rate %"].best, 1);
  assert.equal(rows["Cost per run $"].best, 0);
  assert.deepEqual(rows["check: valid %"].values, [60, 90]);
  await assert.rejects(() => c.eval.experiments.compare([1]), /at least two/);
  s.exps[3] = { ...mk(3, 1, 1), status: "running" };
  await assert.rejects(() => c.eval.experiments.compare([1, 3]), /only completed/);
  assert.throws(() => c.tools.create({ name: "t", description: "d", target_type: "http", url: "https://e.x" }), /input_schema/);
  assert.throws(() => c.tools.create({ name: "t", description: "d", target_type: "python", code: "def run(a): pass" }), /input_schema/);
  assert.throws(() => c.tools.create({ name: "t", description: "d", target_type: "ftp" }), /target_type/);
  await c.tools.create({ name: "t", description: "d", target_type: "http", url: "https://e.x", input_schema: { type: "object", properties: {} } });
  console.log("OK: upload, deploy curl, run with findings, compare, tools");
}

// ── removeInput / removeOutput / removeStep, and loadDraft({ fresh: true }) ──
{
  const mk = (status) => ({ id: 101, number: 1, status,
    input_schema: { type: "object", required: ["name"], properties: { name: { type: "string" }, rating: { type: "integer" }, email: { type: "string" } } },
    output_schema: { type: "object", required: ["o"], properties: { o: { type: "string" }, p: { type: "string" } } },
    snapshot: { input: { name: { type: "string", required: true }, rating: { type: "integer" }, email: { type: "string" } },
                output: { o: { type: "string", required: true }, p: { type: "string" } },
                implementation: { flow: ["one", "two", "three"], sections: { role: "old role" } } } });
  const setup = (status = "approved") => server({ functions: [{ id: 60, slug: "fn", name: "Fn", input_schema: {}, output_schema: {} }], versions: { 60: [mk(status)] } });
  const quiet = async (fn) => { const log = console.log; const out = []; console.log = (...a) => out.push(a.join(" ")); try { return [await fn(), out]; } finally { console.log = log; } };

  // a new version with only `name`
  let s = setup();
  let d = await newClient().loadDraft("fn", { saveTo: false });
  d.removeInput("rating").removeInput("email");
  const v2 = await d.submit("only name", { newVersion: true });
  assert.equal(v2.number, 2);
  const sent = s.calls.find((c) => c.method === "POST" && /\/functions\/60\/versions$/.test(c.url)).body;
  assert.deepEqual(Object.keys(sent.input_schema.properties), ["name"]);
  assert.deepEqual(sent.input_schema.required, ["name"]);
  assert.deepEqual(Object.keys(s.versions[60][0].input_schema.properties), ["name", "rating", "email"]);   // v1 untouched

  s = setup();
  d = await newClient().loadDraft("fn", { saveTo: false });
  d.removeInput("name");
  assert.equal(d.toYaml().includes("  name:"), false);
  assert.equal("required" in d.inputSchema, false);
  assert.throws(() => d.removeInput("nope"), (e) => e instanceof DefinitionError);
  assert.throws(() => d.removeOutput("nope"), (e) => e instanceof DefinitionError);
  d.removeOutput("p");
  assert.deepEqual(Object.keys(d.outputSchema.properties), ["o"]);

  s = setup();
  d = await newClient().loadDraft("fn", { saveTo: false });
  d.removeStep(2); assert.deepEqual(d.flow, ["one", "three"]);
  d.removeStep("one"); assert.deepEqual(d.flow, ["three"]);
  for (const bad of [0, 5, "missing"]) assert.throws(() => d.removeStep(bad), (e) => e instanceof DefinitionError);

  s = setup();
  d = await newClient().loadDraft("fn", { saveTo: false });
  d.addExample({ input: { rating: 3 }, output: { o: "x" } }); d.removeInput("rating");
  await assert.rejects(() => d.submit("", { newVersion: true }), (e) => e instanceof DefinitionError && /rating/.test(e.message));

  // fresh
  s = setup("draft");                                              // the case that would lose data
  d = await newClient().loadDraft("fn", { saveTo: false, fresh: true });
  assert.deepEqual(d.inputSchema, {}); assert.deepEqual(d.flow, []);
  assert.equal(s.calls.filter((c) => c.method !== "GET").length, 0);                           // nothing created yet
  d.addInput("name", { required: true }).addOutput("o", { required: true }).addStep("s");
  const [v, out] = await quiet(() => d.submit("smaller"));
  assert.equal(v.number, 2);
  assert.equal(s.calls.filter((c) => c.method === "PUT").length, 0);
  assert.equal(s.calls.find((c) => /\/functions\/60\/versions$/.test(c.url) && c.method === "POST").body.parent_id, 101);
  assert.deepEqual(Object.keys(s.versions[60][0].input_schema.properties), ["name", "rating", "email"]);
  assert.ok(out.some((l) => l.includes("- input rating")) && out.some((l) => l.includes("- prompt")));
  d.addStep("t"); await d.submit();                                                           // saved: plain saves update v2
  assert.deepEqual(s.calls.filter((c) => c.method === "PUT").map((c) => c.url), [`/api/versions/${v.id}`]);

  s = setup();
  d = await newClient().loadDraft("fn", { saveTo: false, fresh: true });
  d.addOutput("o", { required: true }).addStep("s");
  await assert.rejects(() => d.submit("", { newVersion: false }), /fresh/);
  d.addInput("company");
  d.addInput("name");
  const ch = d.changes();
  assert.ok(ch.includes("+ input company") && ch.includes("- input rating") && ch.includes("~ input name"));
  d = await newClient().loadDraft("fn", { saveTo: false, fresh: true }); d.addInput("company");
  await assert.rejects(() => d.submit(), (e) => e instanceof DefinitionError && /no output fields/.test(e.message) && /no steps/.test(e.message));

  s = setup();
  d = await newClient().loadDraft("fn", { saveTo: false });
  assert.deepEqual(d.changes(), []);
  d.removeInput("rating"); d.addInput("company");
  assert.deepEqual(d.changes(), ["- input rating", "+ input company"]);
  console.log("OK: remove calls and fresh start");
}

console.log("\nAll builder tests passed.");
