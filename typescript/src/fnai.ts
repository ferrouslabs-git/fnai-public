/**
 * fnai — TypeScript client for functionalAI.
 *
 * Zero runtime dependencies (global fetch, Node ≥ 18 or any modern runtime).
 * Two surfaces, mirroring the Python client (sdk/python/fnai.py) exactly:
 *
 * `FnaiFunction` (exported also as `Function`) — the public runtime surface,
 * keyed by a per-function invoke key (fnk_…):
 *
 *     import { Client, FnaiFunction } from "fnai";
 *     const fn = new FnaiFunction("doc_summarizer", { apiKey: "fnk_…",
 *                                                     baseUrl: "https://staging.fnai.dev" });
 *     const result = await fn.run({ document: "…" });
 *     if (result.ok) console.log(result.output, result.meta.cost_usd);
 *
 * `Client` — the management surface, keyed by a personal access token
 * (fnp_…, minted in the app under avatar → API tokens):
 *
 *     const c = new Client({ token: "fnp_…", baseUrl: "https://staging.fnai.dev" });
 *     const fn = await c.functions.create({ slug, name, input_schema, output_schema });
 *     const v = await c.functions.createVersion(fn.id, { snapshot, message: "v1" });
 *     await c.versions.playground(v.id, { input: { document: "…" } });
 *     await c.deployments.deploy(fn.id, v.id);   // deploy IS the gate — freezes the draft
 *     const key = await c.keys.invoke.mint(fn.id, "prod");
 *
 * Types come from schema.d.ts, generated from the backend's OpenAPI schema by
 * tools/gen_sdk.py — regenerate there, never edit it by hand.
 *
 * Error semantics match Python: API-level failures throw FnaiError
 * (.status/.detail); an EXECUTION failure is not an exception — check
 * result.ok / result.status on RunResult.
 */
import type { components } from "../schema.d.js";
import {
  DefinitionError, DIMENSIONS, EFFECTS, MEMORY_SCOPES, checkField, closestMatch, comparisonRow, describe,
  exampleProblems, extrasOf, guardProblems, MANAGED_IMPL, MANAGED_TOP, missingLists, projectField, renderYaml, schemaFields, trustOf,
  validateSample, yamlScalar, type YamlParts,
} from "./definition.js";

export { DefinitionError };

type S = components["schemas"];

export const VERSION = "0.8.0";
const DEFAULT_TIMEOUT_MS = 90_000; // server caps execution at 60s; transport headroom

export class FnaiError extends Error {
  constructor(public status: number, public detail: string) {
    super(`HTTP ${status}: ${detail}`);
    this.name = "FnaiError";
  }
}

/** Raised by Client.useSpace(name) when no space with that name exists for
 *  this account. Checked fresh against GET /api/me every call — never a
 *  cached guess — so a stale or mistyped name can never silently bind. */
export class SpaceNotFoundError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SpaceNotFoundError";
  }
}

type Json = Record<string, unknown>;

// ── shared input validation — every method below that takes a required
// field, an id, or a body validates it with one of these BEFORE making a
// request, so a caller gets a clear, immediate error instead of a confusing
// 404/422 (or, worse, a silently-wrong request) from the backend. ──────────

function requireField(value: unknown, field: string): void {
  if (value === undefined || value === null || value === "" ||
      (typeof value === "string" && !value.trim())) {
    throw new Error(`${field} is required and cannot be empty.`);
  }
}

function isPlainRecord(v: unknown): v is Json {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function requireRecord(value: unknown, field: string, opts: { allowEmpty?: boolean } = {}): void {
  if (!isPlainRecord(value)) {
    throw new Error(`${field} must be an object, got ${value === null ? "null" : typeof value}.`);
  }
  if (!opts.allowEmpty && Object.keys(value).length === 0) {
    throw new Error(`${field} cannot be empty.`);
  }
}

/** Every {fn,version,dataset,score,plan,experiment,key}Id argument goes
 *  through this — an id is a positive integer, never a string someone
 *  forgot to convert. */
function requireId(value: unknown, field: string): void {
  if (typeof value !== "number" || !Number.isInteger(value) || value <= 0) {
    throw new Error(`${field} must be a positive integer id, got ${JSON.stringify(value)}.`);
  }
}

class Http {
  // Private field (# — real JS/TS privacy, not just a naming convention):
  // useSpace() is the ONLY way to change it, and it always validates the
  // name first. Opt-in: a caller who never touches useSpace() never sends
  // an X-Space header at all — exactly today's behavior — and the backend
  // resolves its own default space, same as before this feature existed.
  #space: string | null = null;

  constructor(private base: string, private token: string,
              private timeoutMs: number) {
    this.base = base.replace(/\/+$/, "");
  }

  get space(): string | null {
    return this.#space;
  }

  get baseUrl(): string {
    return this.base;
  }

  /** Where a copy of every saved version goes (Client option `versionsFolder`). */
  versionsFolder?: string;
  /** false: no copies of saved versions; the draft file is just removed on save. */
  keepVersions = true;

  /** Client.useSpace()'s actual implementation. Validated against the
   *  account's REAL spaces on every call (a fresh GET /api/me, not a cached
   *  list a caller could have gone stale) — so this can never bind to a
   *  space that doesn't exist or that this token can't see. */
  async useSpace(name: string | null): Promise<void> {
    if (name === null) {
      this.#space = null;
      return;
    }
    const me = await this.request<{ spaces: { id: string; name: string }[] }>("GET", "/api/me");
    const names = new Set(me.spaces.map((sp) => sp.name));
    if (!names.has(name)) {
      const choices = [...names].sort();
      throw new SpaceNotFoundError(
        `no space named ${JSON.stringify(name)} for this account — ` +
        `choices: ${choices.length ? JSON.stringify(choices) : "(none yet — create one first)"}`);
    }
    this.#space = name;
  }

  private headers(contentType: string): Record<string, string> {
    const h: Record<string, string> = {
      Authorization: `Bearer ${this.token}`,
      "Content-Type": contentType,
      "User-Agent": `fnai-ts/${VERSION}`,
    };
    if (this.#space) h["X-Space"] = this.#space;
    return h;
  }

  async request<T>(method: string, path: string, body?: Json,
                   query?: Record<string, string | number | boolean | undefined>): Promise<T> {
    let url = this.base + path;
    if (query) {
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(query)) {
        if (v !== undefined && v !== null) qs.set(k, String(v));
      }
      const s = qs.toString();
      if (s) url += `?${s}`;
    }
    let resp: Response;
    try {
      resp = await fetch(url, {
        method,
        headers: this.headers("application/json"),
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (e) {
      throw new FnaiError(0, `cannot reach ${this.base}: ${(e as Error).message}`);
    }
    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        const parsed = (await resp.json()) as { detail?: unknown };
        if (parsed && parsed.detail !== undefined) detail = typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail);
      } catch { /* non-JSON error body — keep statusText */ }
      throw new FnaiError(resp.status, detail);
    }
    const raw = await resp.text();
    return (raw ? JSON.parse(raw) : undefined) as T;
  }

  /** multipart/form-data — for the dataset upload endpoint. Same auth/error handling as request(). */
  async requestForm<T>(method: string, path: string, form: FormData): Promise<T> {
    const headers = this.headers("application/json");
    delete headers["Content-Type"];      // fetch sets the multipart boundary itself
    let resp: Response;
    try {
      resp = await fetch(this.base + path, { method, headers, body: form,
                                             signal: AbortSignal.timeout(this.timeoutMs) });
    } catch (e) {
      throw new FnaiError(0, `cannot reach ${this.base}: ${(e as Error).message}`);
    }
    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        const parsed = (await resp.json()) as { detail?: unknown };
        if (parsed && parsed.detail !== undefined) detail = typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail);
      } catch { /* non-JSON error body — keep statusText */ }
      throw new FnaiError(resp.status, detail);
    }
    const raw = await resp.text();
    return (raw ? JSON.parse(raw) : undefined) as T;
  }

  /** Raw text in/out — for the YAML push/pull endpoints (REQ-44), which speak
   *  text/yaml rather than JSON. Same auth/error handling as request(), just
   *  no (de)serialization either direction. */
  async requestText(method: string, path: string, body?: string,
                    contentType = "text/yaml"): Promise<string> {
    let resp: Response;
    try {
      resp = await fetch(this.base + path, {
        method,
        headers: this.headers(contentType),
        body,
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (e) {
      throw new FnaiError(0, `cannot reach ${this.base}: ${(e as Error).message}`);
    }
    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        const parsed = (await resp.json()) as { detail?: unknown };
        if (parsed && parsed.detail !== undefined) detail = typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail);
      } catch { /* non-JSON error body — keep statusText */ }
      throw new FnaiError(resp.status, detail);
    }
    return resp.text();
  }
}

// ── runtime surface (fnk_* invoke keys) ─────────────────────────────────────

export class RunResult {
  constructor(
    public output: Json | null,
    public status: string,                 // "success" | "failed"
    public meta: S["RunMetaOut"],
    public error: string | null = null,
  ) {}

  get ok(): boolean {
    return this.status === "success";
  }

  get traceId(): string | undefined {
    return this.meta?.trace_id;
  }
}

export interface FunctionOptions {
  apiKey: string;
  baseUrl?: string;
  timeoutMs?: number;
}

/** A handle on one deployed function (public runtime surface). */
export class FnaiFunction {
  private http: Http;

  constructor(public slug: string, opts: FunctionOptions) {
    this.http = new Http(opts.baseUrl ?? "http://localhost:8000",
                         opts.apiKey, opts.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  }

  /** The ACTIVE deployment's contract — changes atomically with deploys. */
  contract(): Promise<S["ContractOut"]> {
    return this.http.request("GET", `/v1/run/${this.slug}/contract`);
  }

  /** Check an input against the deployed contract WITHOUT running the function (no model call,
   *  no cost). Returns the problems, each naming the field, e.g. "field 'rating' must be at most
   *  5, got 9"; [] means it would pass the input check. The server runs the same check on every
   *  real call and answers 422 with the same messages. */
  async check(input: Json): Promise<string[]> {
    const c = (await this.contract()) as unknown as Json;
    return validateSample((c.input_schema as Json) ?? {}, input);
  }

  /** Invoke the active deployment; `input` is validated server-side. */
  async run(input: Json): Promise<RunResult> {
    const body = await this.http.request<S["RunOut"]>(
      "POST", `/v1/run/${this.slug}`, { input });
    return new RunResult((body.output ?? null) as Json | null,
                         body.status ?? "failed", body.meta,
                         body.error ?? null);
  }

  /** Report one call that ran on YOUR servers, so it shows in the monitor next to hosted calls.
   *  You give what only you know: function_id, version_id, artifact_hash, input, output, success,
   *  error, steps, tokens_in, tokens_out, cost and latency_ms. `trace_id`, `schema_version` (1),
   *  `source` ("self_hosted") and `created_at` (now) are filled in when you leave them out; set
   *  created_at yourself when you report a call later than it happened. Hidden (sensitive) fields
   *  are hidden on our side. Resolves to {trace_id, duplicate}. Reporting the same trace_id again is
   *  harmless, so retry freely. The server answers 422 with the reason for a trace it refuses. */
  reportTrace(trace: Json): Promise<S["TraceReceivedOut"]> {
    const body = {
      trace_id: globalThis.crypto.randomUUID().replace(/-/g, ""),
      schema_version: 1,
      source: "self_hosted",
      created_at: new Date().toISOString(),
      ...trace,
    };
    return this.http.request("POST", "/v1/traces", body);
  }
}

export { FnaiFunction as Function };

// ── management surface (fnp_* personal access tokens) ───────────────────────

/** A function's id from an id (returned as is) or its slug, e.g. "my_function". A slug is looked up among
 *  this space's functions; an unknown one says so and lists the real ones. */
async function resolveFunctionId(h: Http, ref: number | string): Promise<number> {
  if (typeof ref === "string") {
    requireField(ref, "slug");
    const functions = (await h.request<Json[]>("GET", "/api/functions")) ?? [];
    const found = functions.find((f) => f.slug === ref);
    if (found) return found.id as number;
    const slugs = functions.map((f) => f.slug as string).filter(Boolean).sort();
    const near = closestMatch(ref, slugs);
    let hint = near ? ` — did you mean '${near}'?` : "";
    if (/^\d+$/.test(ref)) hint += ` (to look up by id, pass the number ${ref}, not the text)`;
    const listing = slugs.length ? slugs.slice(0, 20).join(", ") + (slugs.length > 20 ? " …" : "") : "no functions in this space yet";
    throw new FnaiError(404, `no function with slug ${JSON.stringify(ref)} in this space${hint} (functions here: ${listing})`);
  }
  requireId(ref, "fnId");
  return ref;
}

class Functions {
  constructor(private h: Http, private draftsFolder?: string) {}

  list(): Promise<S["FunctionOut"][]> {
    return this.h.request("GET", "/api/functions");
  }

  /** The function, by id or by slug (`c.functions.get("my_function")`), as an object with its fields
   *  (`id`, `slug`, …); print it (`String(fn)`) to see its YAML and what is missing. A slug is looked
   *  up in the space you are in. */
  async get(fnRef: number | string): Promise<S["FunctionOut"] & FunctionRecord> {
    const fnId = await resolveFunctionId(this.h, fnRef);
    const fn = await this.h.request<Json>("GET", `/api/functions/${fnId}`);
    const versions = await this.h.request<Json[]>("GET", `/api/functions/${fnId}/versions`);
    const latest = versions.length ? versions.reduce((a, b) => ((b.number as number) > (a.number as number) ? b : a)) : null;
    return new FunctionRecord(fn, latest) as S["FunctionOut"] & FunctionRecord;
  }

  /** Start a function and build it one part at a time: addInput / addPrompt / addStep /
   *  addOutput …, then submit(). Nothing is created on the server until submit(); `fn.id` is
   *  undefined until then.
   *
   *  While you build, a draft file (YAML) on your computer is kept up to date after every
   *  change, so work survives a stopped script. By default it is `.fnai/drafts/<slug>.yaml` next
   *  to where the script runs. Choose another place with `saveTo: "path/file.yaml"` (or a
   *  folder), or set `draftsFolder` on the Client. `saveTo: false` turns it off. submit()
   *  turns the draft file into that version's copy (.fnai/versions/<slug>/v<N>.yaml); load a draft back with
   *  client.loadYaml(nameOrPath). */
  new(name: string, opts: { slug?: string; description?: string } & DraftOptions = {}): Promise<FunctionDraft> {
    return FunctionDraft.create(this.h, { name, draftsFolder: this.draftsFolder, ...opts });
  }

  async create(body: S["FunctionCreate"]): Promise<S["FunctionOut"] & FunctionRecord> {
    const b = body as unknown as Json;
    requireField(b.slug, "slug");
    requireField(b.name, "name");
    requireRecord(b.input_schema, "input_schema", { allowEmpty: true });
    requireRecord(b.output_schema, "output_schema", { allowEmpty: true });
    const existing = (await this.list()).find((f) => f.slug === b.slug);
    if (existing) {
      throw new Error(`a function with slug ${JSON.stringify(b.slug)} already exists ` +
        `(id=${existing.id}) — pick a different slug, or use that function's id ` +
        `instead of creating another.`);
    }
    return new FunctionRecord(await this.h.request<Json>("POST", "/api/functions", b)) as S["FunctionOut"] & FunctionRecord;
  }

  update(fnId: number, fields: S["FunctionPatch"]): Promise<S["FunctionOut"]> {
    requireId(fnId, "fnId");
    if (Object.keys(fields as unknown as Json).length === 0) {
      throw new Error("update() needs at least one field to change.");
    }
    return this.h.request("PATCH", `/api/functions/${fnId}`, fields as unknown as Json);
  }

  archive(fnId: number): Promise<void> {
    requireId(fnId, "fnId");
    return this.h.request("DELETE", `/api/functions/${fnId}`);
  }

  versions(fnId: number): Promise<S["VersionOut"][]> {
    requireId(fnId, "fnId");
    return this.h.request("GET", `/api/functions/${fnId}/versions`);
  }

  createVersion(fnId: number, body: S["VersionCreate"]): Promise<S["VersionOut"]> {
    requireId(fnId, "fnId");
    const b = body as unknown as Json;
    requireRecord(b.snapshot, "snapshot");
    if (b.parent_id !== undefined && b.parent_id !== null) requireId(b.parent_id, "parent_id");
    return this.h.request("POST", `/api/functions/${fnId}/versions`, b);
  }

  health(fnId: number, windowHours = 24): Promise<S["HealthOut"]> {
    requireId(fnId, "fnId");
    return this.h.request("GET", `/api/functions/${fnId}/health`,
                          undefined, { window_hours: windowHours });
  }

  /** `fn types` — caller-side types for the function's deployed contract. */
  types(fnId: number, lang: "typescript" | "python" | "go" = "typescript"):
      Promise<S["TypesOut"]> {
    requireId(fnId, "fnId");
    return this.h.request("GET", `/api/functions/${fnId}/types`,
                          undefined, { lang });
  }

  // ── v2 DSL: push/pull a function as one YAML document (REQ-44/272/273) ──
  // Deliberately thin: this SDK does not parse or validate the DSL itself —
  // doing so would be exactly the "five separate guesses" problem PRD 09
  // exists to end. The file's bytes go to the backend, which owns the one
  // real parser (Definition.from_yaml) and the one real check
  // (validate_definition); whatever it says is authoritative.

  /** Push a function up from a local YAML file — the whole "define a
   *  function" loop's first step (PRD 09's UX walkthrough). Throws
   *  FnaiError(409, ...) naming every diagnostic code if the file doesn't
   *  pass the backend's check, or if it's built on a stale version
   *  (meta.version — see REQ-268). */
  async createFromYaml(path: string): Promise<CreatedFunction> {
    const fs = await import("node:fs/promises");
    const text = await fs.readFile(path, "utf8");
    return this.createFromYamlText(text);
  }

  /** Same as createFromYaml(), for YAML already in memory (e.g. after
   *  editing what getYaml() returned) rather than freshly read from disk.
   *
   *  A push always mints a new version on the backend — versions are
   *  immutable history, never overwritten in place (REQ-268's stale-push
   *  guard exists precisely because of that). What this method adds on
   *  top: if the function already has a version and the document you're
   *  pushing is IDENTICAL to it (ignoring `meta`, which the platform
   *  re-stamps fresh on every pull — see getYaml()), it returns that
   *  existing version instead of minting a no-op duplicate. Re-importing
   *  an unedited file is then a no-op; a genuine edit still creates a new
   *  version exactly as before. */
  async createFromYamlText(text: string): Promise<CreatedFunction> {
    let yaml: { load: (s: string) => unknown };
    try {
      // @ts-ignore — optional dependency, not in this package's own deps
      yaml = await import("js-yaml");
    } catch {
      throw new PromptFileError(
        "parsing a .yaml file needs js-yaml (`npm install js-yaml`) — " +
        "or call createFromYamlText with pre-parsed JSON text instead");
    }
    let doc: unknown;
    try {
      doc = yaml.load(text);
    } catch (e) {
      throw new PromptFileError(`not valid YAML — ${(e as Error).message}`);
    }
    if (!isPlainObject(doc) || typeof doc.name !== "string" || !doc.name.trim()) {
      throw new PromptFileError("the file must be a YAML mapping with at least a `name` field.");
    }
    const slug = slugify(doc.name);
    // no lookup-by-slug endpoint exists — resolve against the (small,
    // per-space) function list rather than adding a new backend route for
    // this SDK convenience alone.
    const existing = (await this.list()).find((f) => f.slug === slug);
    if (existing) {
      const versions = await this.versions(existing.id);
      if (versions.length) {
        const latest = versions.reduce((a, b) => (b.number > a.number ? b : a));
        const currentText = await this.h.requestText("GET", `/api/versions/${latest.id}/yaml`);
        const currentDoc = yaml.load(currentText);
        if (isPlainObject(currentDoc) &&
            canonicalJson(withoutMeta(doc)) === canonicalJson(withoutMeta(currentDoc))) {
          return { fn: existing, version: latest };
        }
      }
    }
    // Bypasses create()'s validation deliberately (same as the Python
    // client) — the YAML/JSON body is the real definition; input/output
    // schema come from IT, not from a second, redundant object here.
    const fn = existing ?? await this.h.request<S["FunctionOut"]>(
      "POST", "/api/functions", { slug, name: doc.name });
    const raw = await this.h.requestText(
      "POST", `/api/functions/${fn.id}/versions/yaml`, text, "text/yaml");
    return { fn, version: JSON.parse(raw) as S["VersionOut"] };
  }

  /** Pull a function down as YAML — its latest version, regenerated fresh
   *  from what the platform holds (REQ-264: regenerate, not preserve-
   *  verbatim). `meta.version` is stamped on the way out, so pushing it back
   *  unmodified round-trips cleanly (REQ-268). */
  async getYaml(fnId: number): Promise<string> {
    const versions = await this.versions(fnId);
    if (!versions.length) throw new FnaiError(404, `function ${fnId} has no versions yet`);
    const latest = versions.reduce((a, b) => (b.number > a.number ? b : a));
    return this.h.requestText("GET", `/api/versions/${latest.id}/yaml`);
  }
}

function slugify(name: string): string {
  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return slug || "function";
}

/** Same document, minus `meta` — the one block that's SUPPOSED to differ
 *  between two pulls of the same unchanged content (the platform re-stamps
 *  meta.version fresh every time — REQ-264). Used to tell a genuine edit
 *  apart from a no-op re-import. */
function withoutMeta(doc: Record<string, unknown>): Record<string, unknown> {
  const { meta: _meta, ...rest } = doc;
  return rest;
}

/** JSON.stringify with every object's keys sorted, recursively — so two
 *  documents that are the same content but different key order (a
 *  hand-edited file vs. the platform's regenerated one) compare equal. */
function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (isPlainObject(value)) {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

class Versions {
  constructor(private h: Http) {}

  get(vId: number): Promise<S["VersionOut"]> {
    requireId(vId, "vId");
    return this.h.request("GET", `/api/versions/${vId}`);
  }

  update(vId: number, fields: S["VersionUpdate"]): Promise<S["VersionOut"]> {
    requireId(vId, "vId");
    if (Object.keys(fields as unknown as Json).length === 0) {
      throw new Error("update() needs at least one field to change.");
    }
    return this.h.request("PUT", `/api/versions/${vId}`, fields as unknown as Json);
  }

  /** REQ-275: change one field of a draft without resending the whole
   *  snapshot. `patch` follows JSON Merge Patch (RFC 7396) — a key merges
   *  recursively, `null` deletes it, anything else (including a whole list)
   *  replaces wholesale. */
  patch(vId: number, patch: Json, message = ""): Promise<S["VersionOut"]> {
    requireId(vId, "vId");
    requireRecord(patch, "patch");
    return this.h.request("PATCH", `/api/versions/${vId}`, { patch, message } as unknown as Json);
  }

  artifact(vId: number): Promise<Json> {
    requireId(vId, "vId");
    return this.h.request("GET", `/api/versions/${vId}/artifact`);
  }

  playground(vId: number, opts: { input: Json; model?: string; temperature?: number }):
      Promise<S["PlaygroundOut"]> {
    requireId(vId, "vId");
    requireRecord(opts.input, "input", { allowEmpty: true });
    requireField(opts.model ?? "mock-small", "model");
    const body: Json = { input: opts.input, model: opts.model ?? "mock-small" };
    if (opts.temperature !== undefined) body.temperature = opts.temperature;
    return this.h.request("POST", `/api/versions/${vId}/playground/run`, body);
  }

  gate(vId: number): Promise<S["PlanOut"]> {
    requireId(vId, "vId");
    return this.h.request("POST", `/api/versions/${vId}/gate`, {});
  }

  plan(vId: number): Promise<S["PlanOut"]> {
    requireId(vId, "vId");
    return this.h.request("GET", `/api/versions/${vId}/plan`);
  }
}

class Plans {
  constructor(private h: Http) {}

  list(fnId: number): Promise<S["PlanBriefOut"][]> {
    requireId(fnId, "fnId");
    return this.h.request("GET", `/api/functions/${fnId}/plans`);
  }

  create(fnId: number, opts: { name: string }): Promise<S["PlanCreatedOut"]> {
    requireId(fnId, "fnId");
    requireField(opts.name, "name");
    return this.h.request("POST", `/api/functions/${fnId}/plans`, { name: opts.name });
  }

  update(planId: number, fields: Json): Promise<S["PlanCreatedOut"]> {
    requireId(planId, "planId");
    if (Object.keys(fields).length === 0) {
      throw new Error("update() needs at least one field to change.");
    }
    return this.h.request("PATCH", `/api/plans/${planId}`, fields);
  }

  accept(planId: number, body: Json = {}): Promise<S["AcceptPlanOut"]> {
    requireId(planId, "planId");
    return this.h.request("POST", `/api/plans/${planId}/accept`, body);
  }

  clone(planId: number, body: Json = {}): Promise<S["PlanCreatedOut"]> {
    requireId(planId, "planId");
    return this.h.request("POST", `/api/plans/${planId}/clone`, body);
  }

  delete(planId: number): Promise<S["DeletePlanOut"]> {
    requireId(planId, "planId");
    return this.h.request("DELETE", `/api/plans/${planId}`);
  }

  scores(planId: number): Promise<S["ScoreOut"][]> {
    requireId(planId, "planId");
    return this.h.request("GET", `/api/plans/${planId}/scores`);
  }

  addScore(planId: number, body: Json): Promise<S["ScoreOut"]> {
    requireId(planId, "planId");
    if (Object.keys(body).length === 0) {
      throw new Error("addScore() needs at least the score fields to add.");
    }
    return this.h.request("POST", `/api/plans/${planId}/scores`, body);
  }

  removeScore(planId: number, scoreId: number): Promise<S["RemovePlanScoreOut"]> {
    requireId(planId, "planId");
    requireId(scoreId, "scoreId");
    return this.h.request("DELETE", `/api/plans/${planId}/scores/${scoreId}`);
  }

  catalogue(): Promise<S["CatalogueEntryOut"][]> {
    return this.h.request("GET", "/api/catalogue");
  }
}

class Experiments {
  constructor(private h: Http) {}

  list(fnId: number): Promise<S["ExperimentOut"][]> {
    requireId(fnId, "fnId");
    return this.h.request("GET", `/api/functions/${fnId}/experiments`);
  }

  get(expId: number): Promise<S["ExperimentOut"]> {
    requireId(expId, "expId");
    return this.h.request("GET", `/api/experiments/${expId}`);
  }

  create(fnId: number, pins: S["ExperimentCreate"]): Promise<S["ExperimentOut"]> {
    requireId(fnId, "fnId");
    const p = pins as unknown as Json;
    // ExperimentCreate (backend/app/schemas.py) requires version_ids — a
    // planId-only or singleInput-only pin still needs SOME version to run
    // against, so this is the one field worth enforcing here rather than
    // letting an empty pins object reach the backend as a 422.
    if (!Array.isArray(p.version_ids) || p.version_ids.length === 0) {
      throw new Error("create() needs version_ids: [...] — at least one version to run.");
    }
    return this.h.request("POST", `/api/functions/${fnId}/experiments`, p);
  }

  update(expId: number, fields: S["ExperimentPatch"]): Promise<S["ExperimentOut"]> {
    requireId(expId, "expId");
    if (Object.keys(fields as unknown as Json).length === 0) {
      throw new Error("update() needs at least one field to change.");
    }
    return this.h.request("PATCH", `/api/experiments/${expId}`, fields as unknown as Json);
  }

  /** Run a draft experiment. The result carries the scoring details and, by default, a `findings`
   *  list alongside them — what the UI shows together. Pass `includeFindings: false` for the bare
   *  experiment. */
  async run(expId: number, opts: { includeFindings?: boolean } = {}):
      Promise<S["ExperimentOut"] & { findings?: S["FindingOut"][] }> {
    requireId(expId, "expId");
    const out = await this.h.request<Json>("POST", `/api/experiments/${expId}/run`, {});
    if (opts.includeFindings !== false) out.findings = await this.findings(expId);
    return out as S["ExperimentOut"] & { findings?: S["FindingOut"][] };
  }

  /** Compare finished experiments side by side — what the Comparisons screen shows. An experiment
   *  pins exactly one version, so to compare v1 with v2 run one experiment on each (same dataset,
   *  same Score Set) and pass both ids. Returns {experiments, rows}; each row is {metric, values
   *  (one per experiment, in order), best (index of the winner, or null)}. */
  async compare(expIds: number[]): Promise<{ experiments: Json[]; rows: ReturnType<typeof comparisonRow>[] }> {
    if (!Array.isArray(expIds) || expIds.length < 2) throw new Error("compare() needs at least two experiment ids.");
    const exps: Json[] = [];
    for (const id of expIds) {
      requireId(id, "expId");
      const e = (await this.get(id)) as unknown as Json;
      if (e.status !== "completed") throw new Error(`experiment ${id} is ${JSON.stringify(e.status)} — only completed experiments can be compared.`);
      exps.push(e);
    }
    const models = exps.map((e) => trustOf(e.results as Json));
    const costPerRun = (e: Json) => {
      const n = (e.results as Json | null)?.n_rows as number | undefined;
      return n ? ((e.cost as number) ?? 0) / n : ((e.cost as number) ?? 0);
    };
    const p95 = (e: Json) => {
      const v = (((e.results as Json | null)?.latency as Json | undefined)?.p95) as number | undefined;
      return v === undefined ? null : v / 1000;
    };
    const label = (e: Json) => {
      const pv = Object.values((((e.results as Json | null)?.per_version) as Record<string, Json>) ?? {});
      const nums = pv.map((v) => v.number as number).filter(Boolean);
      return nums.map((n) => `v${n}`).join(" vs ") || (e.name as string);
    };
    const rows = [
      comparisonRow("Trust score (/100)", models.map((m) => m.trust)),
      comparisonRow("Pass rate %", models.map((m) => m.passPct)),
      comparisonRow("Cost per run $", exps.map(costPerRun), false),
      comparisonRow("p95 latency s", exps.map(p95), false),
      ...DIMENSIONS.map(([key, name]) => comparisonRow(`${name} %`, models.map((m) => (m.dims[key] === null || m.dims[key] === undefined ? null : (m.dims[key] as number) * 100)))),
      ...[...new Set(models.flatMap((m) => Object.keys(m.checks)))].sort()
        .map((check) => comparisonRow(`check: ${check} %`, models.map((m) => m.checks[check] ?? null))),
    ];
    return { experiments: exps.map((e) => ({ id: e.id, name: e.name, version: label(e), cost: e.cost })), rows };
  }

  clone(expId: number): Promise<S["ExperimentOut"]> {
    requireId(expId, "expId");
    return this.h.request("POST", `/api/experiments/${expId}/clone`, {});
  }

  delete(expId: number): Promise<void> {
    requireId(expId, "expId");
    return this.h.request("DELETE", `/api/experiments/${expId}`);
  }

  findings(expId: number): Promise<S["FindingOut"][]> {
    requireId(expId, "expId");
    return this.h.request("GET", `/api/experiments/${expId}/findings`);
  }
}

class Scores {
  constructor(private h: Http) {}

  list(fnId: number): Promise<S["ScoreOut"][]> {
    requireId(fnId, "fnId");
    return this.h.request("GET", `/api/functions/${fnId}/scores`);
  }

  create(fnId: number, body: S["ScoreCreate"]): Promise<S["ScoreOut"]> {
    requireId(fnId, "fnId");
    const b = body as unknown as Json;
    // ScoreCreate: name and config are both required with no default — tier
    // defaults to "det" server-side, so it's the only one left optional here.
    requireField(b.name, "name");
    requireRecord(b.config, "config", { allowEmpty: true });
    return this.h.request("POST", `/api/functions/${fnId}/scores`, b);
  }

  results(fnId: number, filters: Record<string, string | number | undefined> = {}):
      Promise<S["ScoreResultOut"][]> {
    requireId(fnId, "fnId");
    return this.h.request("GET", `/api/functions/${fnId}/score-results`, undefined, filters);
  }

  setGate(scoreId: number, gate: boolean): Promise<S["ScoreOut"]> {
    requireId(scoreId, "scoreId");
    if (typeof gate !== "boolean") {
      throw new Error(`gate must be a boolean, got ${typeof gate}.`);
    }
    return this.h.request("POST", `/api/scores/${scoreId}/gate`, { gate });
  }
}

/** eval.plans / eval.experiments / eval.scores — the evaluation loop. */
class Eval {
  plans: Plans;
  experiments: Experiments;
  scores: Scores;

  constructor(h: Http) {
    this.plans = new Plans(h);
    this.experiments = new Experiments(h);
    this.scores = new Scores(h);
  }
}

class Datasets {
  constructor(private h: Http) {}

  list(fnId: number, includeArchived = false): Promise<S["DatasetOut"][]> {
    requireId(fnId, "fnId");
    return this.h.request("GET", `/api/functions/${fnId}/datasets`, undefined,
                          includeArchived ? { include_archived: "true" } : undefined);
  }

  create(fnId: number, opts: { name: string; rows?: Json[] }): Promise<S["DatasetOut"]> {
    requireId(fnId, "fnId");
    requireField(opts.name, "name");
    if (opts.rows !== undefined) {
      if (!Array.isArray(opts.rows) || !opts.rows.every(isPlainRecord)) {
        throw new Error("rows must be an array of objects.");
      }
      opts.rows.forEach((r, i) => {
        if (!("input" in r)) throw new Error(`rows[${i}] is missing required key 'input'.`);
      });
    }
    return this.h.request("POST", `/api/functions/${fnId}/datasets`,
                          { name: opts.name, rows: opts.rows ?? [] });
  }

  update(dsId: number, fields: S["DatasetPatch"]): Promise<S["DatasetOut"]> {
    requireId(dsId, "dsId");
    if (Object.keys(fields as unknown as Json).length === 0) {
      throw new Error("update() needs at least one field to change.");
    }
    return this.h.request("PATCH", `/api/datasets/${dsId}`, fields as unknown as Json);
  }

  delete(dsId: number): Promise<void> {
    requireId(dsId, "dsId");
    return this.h.request("DELETE", `/api/datasets/${dsId}`);
  }

  rows(dsId: number): Promise<S["RowOut"][]> {
    requireId(dsId, "dsId");
    return this.h.request("GET", `/api/datasets/${dsId}/rows`);
  }

  addRow(dsId: number, row: S["RowIn"]): Promise<S["RowOut"]> {
    requireId(dsId, "dsId");
    // RowIn (backend/app/schemas.py): input is required with no default;
    // expected/tags/source all have defaults, so only this one is worth
    // enforcing before a request goes out.
    requireRecord((row as unknown as Json).input, "input", { allowEmpty: true });
    return this.h.request("POST", `/api/datasets/${dsId}/rows`, row as unknown as Json);
  }

  /** Bulk-load rows from a local .csv, .json (array of flat objects) or .xlsx/.xlsm file into an
   *  existing dataset — what the UI's "Upload rows" does. `mapping` is {file column: input
   *  field}; the optional `expectedMapping` is {file column: expected-output field}. Columns are
   *  never guessed. Returns {added, skipped, errors}. */
  async upload(dsId: number, path: string, opts: { mapping: Record<string, string>;
               expectedMapping?: Record<string, string> }):
      Promise<{ added: number; skipped: number; errors: Json[] }> {
    requireId(dsId, "dsId");
    requireField(path, "path");
    requireRecord(opts.mapping, "mapping");
    if (!/\.(csv|json|xlsx|xlsm)$/i.test(path)) throw new Error("unsupported file type — use .csv, .json, or .xlsx");
    const fs = await import("node:fs/promises");
    const nodePathMod = await import("node:path");
    let buf: Buffer;
    try { buf = await fs.readFile(path); } catch { throw new Error(`no such file: ${JSON.stringify(path)}`); }
    const form = new FormData();
    form.append("file", new Blob([new Uint8Array(buf)]), nodePathMod.basename(path));
    form.append("mapping", JSON.stringify(opts.mapping));
    form.append("expected_mapping", JSON.stringify(opts.expectedMapping ?? {}));
    return this.h.requestForm("POST", `/api/datasets/${dsId}/upload`, form);
  }

  snapshot(dsId: number): Promise<S["SnapshotOut"]> {
    requireId(dsId, "dsId");
    return this.h.request("POST", `/api/datasets/${dsId}/snapshot`, {});
  }

  snapshots(dsId: number): Promise<S["SnapshotHistoryOut"][]> {
    requireId(dsId, "dsId");
    return this.h.request("GET", `/api/datasets/${dsId}/snapshots`);
  }
}

class Deployments {
  constructor(private h: Http) {}

  /** Deploy a version. The result also carries `endpoint` and `curl` — a ready-to-run call against
   *  the public runtime (put a minted fnk_* invoke key in place of the placeholder). Pass
   *  `confirm: true` to deploy over a breaking contract change. `model` (and `temperature`) is the
   *  model production calls run on: a version saved by the builder, YAML or the MCP tools names none,
   *  so it needs one here (or a default model on the space), or the deploy is refused. */
  async deploy(fnId: number, versionId: number,
               opts: { confirm?: boolean; model?: string; temperature?: number } = {}):
      Promise<S["DeploymentOut"] & { endpoint?: string; curl?: string }> {
    requireId(fnId, "fnId");
    requireId(versionId, "versionId");
    const body: Json = { version_id: versionId };
    if (opts.confirm) body.confirm = true;
    if (opts.model) body.model = opts.model;
    if (opts.temperature !== undefined) body.temperature = opts.temperature;
    const out = await this.h.request<Json>("POST", `/api/functions/${fnId}/deploy`, body);
    try { Object.assign(out, await this.curl(fnId)); } catch { /* the deploy worked; the snippet is a convenience */ }
    return out as S["DeploymentOut"] & { endpoint?: string; curl?: string };
  }

  /** {endpoint, curl} for calling this function's public runtime URL. Pass `apiKey` (an fnk_* key)
   *  to embed it, else a placeholder is used. */
  async curl(fnId: number, apiKey?: string): Promise<{ endpoint: string; curl: string }> {
    requireId(fnId, "fnId");
    const fn = await this.h.request<Json>("GET", `/api/functions/${fnId}`);
    const endpoint = `${this.h.baseUrl}/v1/run/${fn.slug}`;
    let schema = (fn.input_schema as Json) ?? {};
    if (!Object.keys((schema.properties as Json) ?? {}).length) {   // built with submit(): the schema is on the version
      const versions = await this.h.request<Json[]>("GET", `/api/functions/${fnId}/versions`);
      if (versions.length) {
        schema = ((versions.reduce((a, b) => ((b.number as number) > (a.number as number) ? b : a))).input_schema as Json) ?? {};
      }
    }
    const sample = JSON.stringify(Object.fromEntries(Object.keys((schema.properties as Json) ?? {}).map((k) => [k, "..."])));
    return { endpoint, curl: `curl -X POST ${endpoint} \\\n  -H "Authorization: Bearer ${apiKey ?? "fnk_YOUR_KEY"}" \\\n` +
      `  -H "Content-Type: application/json" \\\n  -d '{"input": ${sample}}'` };
  }

  current(fnId: number): Promise<S["DeploymentOut"]> {
    requireId(fnId, "fnId");
    return this.h.request("GET", `/api/functions/${fnId}/deployment`);
  }

  history(fnId: number): Promise<S["DeploymentOut"][]> {
    requireId(fnId, "fnId");
    return this.h.request("GET", `/api/functions/${fnId}/deployments`);
  }

  rollback(fnId: number, versionId?: number): Promise<S["DeploymentOut"]> {
    requireId(fnId, "fnId");
    if (versionId !== undefined) requireId(versionId, "versionId");
    return this.h.request("POST", `/api/functions/${fnId}/rollback`,
                          versionId !== undefined ? { version_id: versionId } : {});
  }
}

/** Register and manage customer tools (a REST endpoint, an MCP server, or sandboxed Python) — what
 *  the UI's tool registration does. Attach one to a function with FunctionDraft.addTool(name). */
class Tools {
  constructor(private h: Http) {}

  list(): Promise<Json[]> {
    return this.h.request("GET", "/api/tools");
  }

  get(toolId: number): Promise<Json> {
    requireId(toolId, "toolId");
    return this.h.request("GET", `/api/tools/${toolId}`);
  }

  /** targetType: "http" (url, method), "mcp" (url, mcpToolName) or "python" (code defining
   *  run(args)). Optional: inputSchema or openapiSpec (+ openapiOperation), effect, servicing,
   *  configSchema, authKind ("static" | "oauth2_client_credentials") + credential. Fields use the
   *  API's own names (snake_case), as everywhere else in this client. */
  create(body: { name: string; description: string; target_type: "http" | "mcp" | "python" } & Json): Promise<Json> {
    requireField(body.name, "name");
    requireField(body.description, "description");
    const t = body.target_type;
    if (!["http", "mcp", "python"].includes(t)) throw new Error(`target_type must be 'http', 'mcp' or 'python', got ${JSON.stringify(t)}.`);
    if ((t === "http" || t === "mcp") && !body.url) throw new Error(`a ${t} tool needs url: ...`);
    if (t === "python") {
      if (!body.code) throw new Error("a python tool needs code: ... defining run(args).");
      if (!body.input_schema) throw new Error("a python tool needs input_schema: ... (the arguments run(args) takes).");
    } else if (!body.input_schema && !body.openapi_spec) {
      throw new Error(`a ${t} tool needs input_schema: ... or openapi_spec: ... so the agent knows what arguments to send.`);
    }
    return this.h.request("POST", "/api/tools", body);
  }

  /** Try a tool with a real call before saving it. Nothing is stored. */
  test(body: { target_type: "http" | "mcp" | "python"; sample_args?: Json } & Json): Promise<Json> {
    if (!["http", "mcp", "python"].includes(body.target_type)) {
      throw new Error(`target_type must be 'http', 'mcp' or 'python', got ${JSON.stringify(body.target_type)}.`);
    }
    return this.h.request("POST", "/api/tools/test", { sample_args: {}, ...body });
  }

  update(toolId: number, fields: Json): Promise<Json> {
    requireId(toolId, "toolId");
    if (Object.keys(fields).length === 0) throw new Error("update() needs at least one field to change.");
    return this.h.request("PATCH", `/api/tools/${toolId}`, fields);
  }

  usage(toolId: number): Promise<Json> {
    requireId(toolId, "toolId");
    return this.h.request("GET", `/api/tools/${toolId}/usage`);
  }
}

/** Space management. `create()` validates against a fresh GET /api/me every
 *  call — the same exact-name match get_scope's X-Space lookup uses (see
 *  backend/app/auth_usermanagement/security/scope.py) — so a typo'd
 *  "create" that was meant to be a "switch" fails loudly instead of quietly
 *  leaving two same-named spaces. */
class Spaces {
  constructor(private h: Http) {}

  async create(opts: { name: string }): Promise<{ id: string; name: string }> {
    requireField(opts.name, "name");
    const me = await this.h.request<{ spaces: { id: string; name: string }[] }>("GET", "/api/me");
    const existing = me.spaces.find((sp) => sp.name === opts.name);
    if (existing) {
      throw new Error(`a space named ${JSON.stringify(opts.name)} already exists ` +
        `(id=${existing.id}) — use Client.useSpace(${JSON.stringify(opts.name)}) ` +
        `to switch to it instead of creating another.`);
    }
    return this.h.request("POST", "/api/spaces", { name: opts.name });
  }
}

/** BYOK provider credentials (org/space scope) — model calls run on these. */
class ProviderKeys {
  constructor(private h: Http) {}

  list(): Promise<S["KeyOut"][]> {
    return this.h.request("GET", "/api/provider-keys");
  }

  add(body: S["KeyCreate"]): Promise<S["KeyOut"]> {
    const b = body as unknown as Json;
    requireField(b.provider, "provider");
    requireRecord(b.secret, "secret");
    return this.h.request("POST", "/api/provider-keys", b);
  }

  update(keyId: number, fields: S["KeyPatch"]): Promise<S["KeyOut"]> {
    requireId(keyId, "keyId");
    if (Object.keys(fields as unknown as Json).length === 0) {
      throw new Error("update() needs at least one field to change.");
    }
    return this.h.request("PATCH", `/api/provider-keys/${keyId}`, fields as unknown as Json);
  }

  test(keyId: number): Promise<S["KeyTestOut"]> {
    requireId(keyId, "keyId");
    return this.h.request("POST", `/api/provider-keys/${keyId}/test`, {});
  }

  rotate(keyId: number, secret: Json): Promise<S["KeyOut"]> {
    requireId(keyId, "keyId");
    requireRecord(secret, "secret");
    return this.h.request("POST", `/api/provider-keys/${keyId}/rotate`, { secret });
  }

  delete(keyId: number): Promise<void> {
    requireId(keyId, "keyId");
    return this.h.request("DELETE", `/api/provider-keys/${keyId}`);
  }
}

/** Per-function fnk_* keys for the public runtime — plaintext returned once. */
class InvokeKeys {
  constructor(private h: Http) {}

  list(fnId: number): Promise<S["InvokeKeyOut"][]> {
    requireId(fnId, "fnId");
    return this.h.request("GET", `/api/functions/${fnId}/invoke-keys`);
  }

  mint(fnId: number, name = ""): Promise<S["InvokeKeyMintedOut"]> {
    requireId(fnId, "fnId");
    return this.h.request("POST", `/api/functions/${fnId}/invoke-keys`, { name });
  }

  revoke(keyId: number): Promise<void> {
    requireId(keyId, "keyId");
    return this.h.request("DELETE", `/api/invoke-keys/${keyId}`);
  }
}

/** The models you can name as `model` when deploying or running an experiment. */
class Models {
  constructor(private h: Http) {}

  /** Every model the platform can route: name, provider, price per 1k tokens, and `usable` (true when this
   *  space has what a call needs: the mock always, a real model once its provider has an active key).
   *  `reason` says what is missing when it is not. */
  list(): Promise<S["ModelOut"][]> {
    return this.h.request("GET", "/api/models");
  }
}

class Keys {
  provider: ProviderKeys;
  invoke: InvokeKeys;

  constructor(h: Http) {
    this.provider = new ProviderKeys(h);
    this.invoke = new InvokeKeys(h);
  }
}

class Traces {
  constructor(private h: Http) {}

  list(opts: { functionId?: number; source?: string; limit?: number } = {}):
      Promise<S["TraceSummaryOut"][]> {
    if (opts.functionId !== undefined) requireId(opts.functionId, "functionId");
    return this.h.request("GET", "/api/traces", undefined, {
      function_id: opts.functionId, source: opts.source, limit: opts.limit ?? 50,
    });
  }

  get(traceId: string): Promise<S["TraceDetailOut"]> {
    requireField(traceId, "traceId");
    return this.h.request("GET", `/api/traces/${traceId}`);
  }

  /** The flywheel: production trace → regression dataset row. */
  flag(traceId: string): Promise<S["TraceFlagOut"]> {
    requireField(traceId, "traceId");
    return this.h.request("POST", `/api/traces/${traceId}/flag`, {});
  }
}

class AuthTokens {
  constructor(private h: Http) {}

  list(): Promise<S["TokenOut"][]> {
    return this.h.request("GET", "/api/auth/tokens");
  }

  /** Plaintext token is in THIS response only. */
  mint(name: string): Promise<S["TokenMintedOut"]> {
    requireField(name, "name");
    return this.h.request("POST", "/api/auth/tokens", { name });
  }

  revoke(tokenId: string): Promise<void> {
    requireField(tokenId, "tokenId");
    return this.h.request("DELETE", `/api/auth/tokens/${tokenId}`);
  }
}

class Auth {
  tokens: AuthTokens;

  constructor(h: Http) {
    this.tokens = new AuthTokens(h);
  }
}

// ── local draft files ───────────────────────────────────────────────────────
// Node's file tools are loaded only when needed, so the package still imports in a browser.
type NodeFs = typeof import("node:fs");
type NodePath = typeof import("node:path");
let nodeFs: NodeFs | null = null;
let nodePath: NodePath | null = null;
let nodeHome = "";
async function loadNode(): Promise<void> {
  if (nodeFs) return;
  try {
    nodeFs = await import("node:fs");
    nodePath = await import("node:path");
    nodeHome = (await import("node:os")).homedir();
  } catch { /* not running in Node: draft files are simply off */ }
}

/** Where draft files go when no exact path is given: the folder the user set, else
 *  $FNAI_DRAFTS_DIR, else .fnai/drafts next to where the script runs. */
function draftsDir(folder?: string): string {
  const p = nodePath!;
  const base = folder ?? process.env.FNAI_DRAFTS_DIR ?? p.join(process.cwd(), ".fnai", "drafts");
  return p.resolve(base.replace(/^~(?=$|[\\/])/, nodeHome));
}

/** Where a copy of every saved version goes: the folder the user set, else $FNAI_VERSIONS_DIR, else
 *  .fnai/versions beside .fnai/drafts (inside the drafts folder if that is not called "drafts"). */
function versionsDir(versionsFolder?: string, draftsFolder?: string): string {
  const p = nodePath!;
  const base = versionsFolder ?? process.env.FNAI_VERSIONS_DIR;
  if (base) return p.resolve(base.replace(/^~(?=$|[\\/])/, nodeHome));
  const drafts = draftsDir(draftsFolder);
  return p.basename(drafts) === "drafts" ? p.join(p.dirname(drafts), "versions") : p.join(drafts, "versions");
}

/** The draft file for a function. saveTo wins (a .yaml/.yml path is the file itself, anything
 *  else is a folder), then the drafts folder, then the default. saveTo: false turns it off. */
function resolveDraftPath(saveTo: string | false | undefined, draftsFolder: string | undefined,
                          slug: string): string | null {
  if (!nodeFs || !nodePath || saveTo === false) return null;
  const p = nodePath;
  if (saveTo) {
    const expanded = p.resolve(saveTo.replace(/^~(?=$|[\\/])/, nodeHome));
    return /\.ya?ml$/i.test(expanded) ? expanded : p.join(expanded, `${slug}.yaml`);
  }
  return p.join(draftsDir(draftsFolder), `${slug}.yaml`);
}

/** Write to a temporary file beside the real one, then swap it in, so a crash halfway never
 *  leaves a half-written file. */
function writeAtomic(path: string, text: string): void {
  const fs = nodeFs!;
  fs.mkdirSync(nodePath!.dirname(path), { recursive: true });
  const tmp = `${path}.tmp`;
  fs.writeFileSync(tmp, text, "utf8");
  fs.renameSync(tmp, path);
}

export interface DraftOptions {
  /** Where this function's draft file goes: a file path (.yaml/.yml) or a folder. `false` turns it off. */
  saveTo?: string | false;
  /** A folder for draft files. The Client option of the same name sets this for you. */
  draftsFolder?: string;
}

/** JSON with sorted keys, so two equal values compare equal as text. */
function stableJson(v: unknown): string {
  return JSON.stringify(v, (_k, x) => (x && typeof x === "object" && !Array.isArray(x)
    ? Object.fromEntries(Object.entries(x).sort(([a], [b]) => (a < b ? -1 : 1))) : x));
}

export interface LoadOptions extends DraftOptions {
  /** Start from this version number instead of the latest. A frozen (deployed) version is never changed:
   *  saving creates a NEW version based on it. A draft is edited in place when you save. */
  version?: number;
  /** Keep the function (same name and slug) but start it EMPTY: only what you add goes into the new
   *  version. A fresh draft always saves as a NEW version and never overwrites the one it grew from;
   *  submit() lists what was dropped, added or changed first (`draft.changes()` gives the same list). */
  fresh?: boolean;
}

export interface ToolOptions {
  /** Cap on uses per run. */
  maxCalls?: number;
  /** Lock the tool until a condition holds, e.g. `human_approval.returned.approved == true`. */
  requires?: string;
  /** May only tighten the tool's registered effect. */
  effect?: "read_only" | "idempotent" | "side_effecting";
  /** A version-pinned reference, e.g. `summarizer@3`. */
  ref?: string;
  /** This function's own settings for the tool. */
  config?: Json;
}

const INSPECT = Symbol.for("nodejs.util.inspect.custom");

/** The YAML printing shared by the draft and the function record. */
abstract class YamlView {
  protected abstract yamlParts(): YamlParts;
  protected abstract missingLists(): { required: string[]; recommended: string[] };

  /** The function as YAML in the definition format (function-dsl.md). */
  toYaml(): string { return renderYaml(this.yamlParts()); }

  /** What the function still needs: `required` (the server needs it) and `recommended`. */
  missing(): { required: string[]; recommended: string[] } { return this.missingLists(); }

  /** The YAML, then what is missing. `String(fn)` and `console.log(fn)` both show it. */
  toString(): string { return this.printNote() + describe(this.toYaml(), this.missingLists()); }

  /** A line above the printed function, when it has something to say. */
  protected printNote(): string { return ""; }

  [INSPECT](): string { return this.toString(); }
}

/** A function as the server returns it (`functions.get` / `functions.create`). The fields
 *  (`id`, `slug`, `name`, …) are on the object as before; printing it shows the definition
 *  as YAML and what is missing. Once the function has a version, the prompt, steps and tools
 *  shown come from the latest one. */
export class FunctionRecord extends YamlView {
  version: Json | null = null;
  [key: string]: unknown;

  constructor(data: Json, version: Json | null = null) {
    super();
    Object.assign(this, data);
    Object.defineProperty(this, "version", { value: version, enumerable: false, writable: true });
  }

  private schemas(): [Json, Json] {
    const v = this.version ?? {};
    return [((v.input_schema as Json) ?? (this.input_schema as Json) ?? {}),
            ((v.output_schema as Json) ?? (this.output_schema as Json) ?? {})];
  }

  private impl(): Json {
    const snap = (this.version?.snapshot as Json) ?? {};
    return isPlainRecord(snap.implementation) ? snap.implementation : {};
  }

  protected yamlParts(): YamlParts {
    const [ins, outs] = this.schemas();
    const impl = this.impl();
    const snap = (this.version?.snapshot as Json) ?? {};
    return {
      name: (this.name as string) || (this.slug as string) || "",
      description: this.description as string | undefined,
      input: schemaFields(ins), output: schemaFields(outs),
      mode: ((impl.execution as Json)?.mode as string) ?? "single_shot",
      flow: (impl.flow as string[]) ?? [],
      rubric: (snap.rubric as string[]) ?? [],
      memory: (snap.memory as Json) ?? null,
      extraTop: extrasOf(snap).top, extraImpl: extrasOf(snap).impl,
      sections: this.version ? ((impl.sections as Json) ?? {}) : null,
      tools: (impl.tools as Json[]) ?? [],
    };
  }

  protected missingLists(): { required: string[]; recommended: string[] } {
    const [ins, outs] = this.schemas();
    if (!this.version) {
      const m = missingLists(schemaFields(ins), schemaFields(outs), ["-"],
        { role: "-", examples: ["-"], constraints: ["-"] });
      m.recommended.push("no version saved yet — build one with client.functions.new(...) or client.loadDraft(id)");
      return m;
    }
    const impl = this.impl();
    return missingLists(schemaFields(ins), schemaFields(outs), (impl.flow as string[]) ?? [],
      isPlainRecord(impl.sections) ? impl.sections : {});
  }
}

/** The function being built (FUNCTION_CLASS_DESIGN.md, approved 2026-09-24).
 *
 *  Add the parts one call at a time — inputs, outputs, a prompt, definitions, constraints, edge
 *  cases, examples, steps, tools, a rubric, memory. Each call is checked against the definition
 *  rules (function-dsl.md) as you make it: a mistake throws DefinitionError saying what to fix.
 *
 *  Nothing reaches the server until submit(). While you build, a YAML draft file on your computer
 *  is kept up to date after every change (default `.fnai/drafts/<slug>.yaml`), so a stopped
 *  script loses nothing: client.loadYaml(...) reads it back. submit() deletes it.
 *  `String(fn)` shows the function as YAML and what is still missing. */
export class FunctionDraft extends YamlView {
  /** The function's id on the server. `undefined` until submit() creates it. */
  id: number | undefined;
  slug?: string;
  name?: string;
  description = "";
  lastVersion: S["VersionOut"] | null = null;
  private saveToCfg: string | false | undefined;
  private draftsCfg: string | undefined;

  private h: Http;
  private inputSchema: Json;
  private outputSchema: Json;
  private sections: Record<string, unknown> =
    { role: null, definitions: [], constraints: [], edge_cases: [], examples: [] };
  private tools: Json[] = [];
  private flow: string[] = [];
  private rubric: string[] = [];
  private memory: Json | null = null;
  private mode: "single_shot" | "agent" | null = null;
  private loading = false;
  private warned = false;
  // which version a save goes to (see load({ version })):
  //   neither set -> the server's rule: update the latest draft, or start a new version
  //   target      -> edit that draft in place
  //   base        -> a frozen version this draft grew from: a save creates a NEW version
  private target: Json | null = null;
  private base: Json | null = null;
  // parts of a loaded version the builder has no call for, kept so a save does not drop them
  private extraTop: Json = {};                                   // needs, ...
  private extraImpl: Json = {};                                  // preprocessors, ...
  private fieldExtras: Record<"input" | "output", Record<string, { flags: Json; full: Json | null }>> = { input: {}, output: {} };
  private fieldOrder: Record<"input" | "output", string[]> = { input: [], output: [] };
  private fresh = false;                          // load({ fresh: true }): a save is always a NEW version
  private baseline: Record<string, unknown> | null = null;   // the loaded version's outline, to list changes() against

  private constructor(h: Http, fn: Json) {
    super();
    this.h = h;
    this.id = fn.id as number | undefined;
    this.slug = fn.slug as string | undefined;
    this.name = fn.name as string | undefined;
    this.description = (fn.description as string) ?? "";
    this.inputSchema = { ...((fn.input_schema as Json) ?? {}) };
    this.outputSchema = { ...((fn.output_schema as Json) ?? {}) };
  }

  /** Start a new function on your computer. Nothing is created on the server until submit(): the
   *  one thing done here is a read-only check that the name is free (archived functions keep
   *  their name, so they count). `id` is undefined until then. */
  static async create(h: Http, opts: { name: string; slug?: string; description?: string } & DraftOptions):
      Promise<FunctionDraft> {
    requireField(opts.name, "name");
    await loadNode();
    const slug = opts.slug ?? slugify(opts.name);
    const all = await h.request<Json[]>("GET", "/api/functions", undefined, { include_archived: "true" });
    const taken = (all ?? []).find((f) => f.slug === slug);
    if (taken) {
      throw new Error(`a function with slug ${JSON.stringify(slug)} already exists${taken.archived ? " (archived)" : ""} ` +
        `(id=${taken.id}) — pick another name (or slug: ...), or carry on with that one using client.loadDraft(${taken.id}).`);
    }
    const fn: Json = { slug, name: opts.name, description: opts.description ?? "" };
    const draft = new FunctionDraft(h, fn);
    draft.setDraftPath(opts);
    draft.autosave();
    return draft;
  }

  /** Resume an existing function to keep building on it — e.g. adding a field after it's already
   *  deployed. By default you get the LATEST version's content. Pass `version: N` to start from
   *  version N: a frozen (deployed) version is never changed (saving creates a NEW version based on it,
   *  and never overwrites another draft); a draft is edited in place when you save. */
  static async load(h: Http, fnRef: number | string, opts: LoadOptions = {}): Promise<FunctionDraft> {
    const fnId = await resolveFunctionId(h, fnRef);
    const version = opts.version;
    if (version !== undefined && !(Number.isInteger(version) && version >= 1)) {
      throw new Error(`version must be a version number such as 1, got ${JSON.stringify(version)}.`);
    }
    await loadNode();
    const fn = await h.request<Json>("GET", `/api/functions/${fnId}`);
    const draft = new FunctionDraft(h, fn);
    draft.setDraftPath(opts);
    const versions = (await h.request<S["VersionOut"][]>("GET", `/api/functions/${fnId}/versions`)) ?? [];
    let chosen: S["VersionOut"] | undefined;
    if (version === undefined) {
      if (versions.length) chosen = versions.reduce((a, b) => (b.number > a.number ? b : a));
    } else {
      chosen = versions.find((v) => v.number === version);
      if (!chosen) {
        const have = versions.map((v) => v.number).sort((a, b) => a - b).join(", ") || "none yet";
        throw new FnaiError(404, `no version ${version} for ${JSON.stringify(fn.slug)} (versions: ${have})`);
      }
      draft.pointAt(chosen as unknown as Json);
    }
    if (chosen) {
      draft.applyVersion(chosen as unknown as Json);
      draft.baseline = draft.outline();
      if (opts.fresh) draft.startEmpty();
    }
    return draft;
  }

  /** Forget the loaded content but keep the function itself and the version it grew from. */
  private startEmpty(): void {
    this.inputSchema = {};
    this.outputSchema = {};
    this.sections = { role: null, definitions: [], constraints: [], edge_cases: [], examples: [] };
    this.tools = []; this.flow = []; this.rubric = []; this.memory = null; this.mode = null;
    this.extraTop = {}; this.extraImpl = {};
    this.fieldExtras = { input: {}, output: {} };
    this.fieldOrder = { input: [], output: [] };
    this.target = null; this.base = null;
    this.fresh = true;
    this.autosave();
  }

  /** The function as { label: value }, one entry per part, for listing changes. */
  private outline(): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const side of ["input", "output"] as const) {
      for (const [n, spec] of Object.entries(this.dslFields(side))) out[`${side} ${n}`] = spec;
    }
    if (this.sections.role) out["prompt"] = this.sections.role;
    for (const kind of ["definitions", "constraints", "edge_cases"]) {
      for (const text of (this.sections[kind] as string[]) ?? []) out[`${kind} ${text}`] = true;
    }
    for (const ex of (this.sections.examples as Json[]) ?? []) out["example " + stableJson(ex)] = true;
    for (const text of this.flow) out[`step ${text}`] = true;
    for (const t of this.tools) out[`tool ${t.name ?? t.server ?? stableJson(t)}`] = t;
    for (const text of this.rubric) out[`rubric ${text}`] = true;
    if (this.memory) out["memory"] = this.memory;
    Object.assign(out, this.extraTop, this.extraImpl);
    return out;
  }

  /** What differs from the version you loaded: "+ input company" (added), "- input rating" (dropped),
   *  "~ input name" (changed). Empty when nothing differs or nothing was loaded. */
  changes(): string[] {
    if (!this.baseline) return [];
    const now = this.outline(), was = this.baseline;
    return [
      ...Object.keys(was).filter((k) => !(k in now)).map((k) => `- ${k}`),
      ...Object.keys(now).filter((k) => !(k in was)).map((k) => `+ ${k}`),
      ...Object.keys(now).filter((k) => k in was && stableJson(now[k]) !== stableJson(was[k])).map((k) => `~ ${k}`),
    ];
  }

  /** Where the next save goes: a draft is edited in place; a frozen version becomes the base of a new one. */
  private pointAt(version: Json): void {
    if (version.status === "draft") { this.target = version; this.base = null; }
    else { this.base = version; this.target = null; }
  }

  /** Stage a saved version's content, so addX calls extend it. */
  private applyVersion(lv: Json): void {
    const snapshot = (lv.snapshot as Json | undefined) ?? {};
    if (Object.keys(snapshot).length && !isPlainRecord(snapshot.implementation) && (snapshot.sections || snapshot.impl)) {
      throw new DefinitionError([`v${lv.number} was saved in the older editor format, which the builder can't edit ` +
        `without losing content. Edit it in the app, or build a new version with the builder.`]);
    }
    this.lastVersion = lv as unknown as S["VersionOut"];
    // a function built with submit() keeps its schemas on the version
    if (isPlainRecord(lv.input_schema) && Object.keys(lv.input_schema).length) this.inputSchema = JSON.parse(JSON.stringify(lv.input_schema));
    if (isPlainRecord(lv.output_schema) && Object.keys(lv.output_schema).length) this.outputSchema = JSON.parse(JSON.stringify(lv.output_schema));
    const impl = (snapshot.implementation as Json) ?? {};
    const sections = (impl.sections as Json) ?? {};
    for (const key of ["role", "definitions", "constraints", "edge_cases", "examples"]) {
      if (key in sections) this.sections[key] = sections[key];
    }
    this.tools = [...((impl.tools as Json[]) ?? [])];
    this.flow = [...((impl.flow as string[]) ?? [])];
    this.rubric = [...((snapshot.rubric as string[]) ?? [])];
    this.memory = isPlainRecord(snapshot.memory) ? { ...snapshot.memory } : null;
    const extras = extrasOf(snapshot);
    this.extraTop = extras.top;
    this.extraImpl = extras.impl;
    for (const side of ["input", "output"] as const) {
      this.fieldExtras[side] = {};
      this.fieldOrder[side] = [];
      const dsl = snapshot[side];
      if (!isPlainRecord(dsl)) continue;
      this.fieldOrder[side] = Object.keys(dsl);
      for (const [name, spec] of Object.entries(dsl)) {
        if (!isPlainRecord(spec)) continue;
        if (spec.derived) {          // filled by a preprocessor: never in the stored JSON schema
          this.fieldExtras[side][name] = { flags: {}, full: Object.fromEntries(Object.entries(spec).filter(([, x]) => x !== false && x !== null)) };
        } else if (spec.sensitive) {
          this.fieldExtras[side][name] = { flags: { sensitive: true }, full: null };
        }
      }
    }
    // only an explicit "agent" is kept; single_shot is the default and flips to agent once a tool is added
    if (((impl.execution as Json)?.mode) === "agent") this.mode = "agent";
  }

  /** The fields in the definition's form: the stored schema plus what it cannot hold (derived fields, the
   *  sensitive flag), in their original order. */
  private dslFields(side: "input" | "output"): Record<string, Json> {
    const fields = schemaFields(side === "input" ? this.inputSchema : this.outputSchema);
    for (const [name, ex] of Object.entries(this.fieldExtras[side])) {
      if (name in fields) Object.assign(fields[name]!, ex.flags);
      else if (ex.full) fields[name] = { ...ex.full };
    }
    const order = this.fieldOrder[side].filter((n) => n in fields);
    return Object.fromEntries([...order, ...Object.keys(fields).filter((n) => !order.includes(n))].map((n) => [n, fields[n]!]));
  }

  /** Rebuild a draft from a parsed YAML document (used by Client.loadYaml). The document is the
   *  whole truth: it replaces what the function held. */
  static async fromDocument(h: Http, fn: Json, doc: Json, opts: DraftOptions, source: string):
      Promise<FunctionDraft> {
    const draft = new FunctionDraft(h, fn);
    draft.inputSchema = {};
    draft.outputSchema = {};
    draft.description = (doc.description as string) ?? draft.description;
    draft.setDraftPath(opts);
    draft.loading = true;
    try {
      for (const side of ["input", "output"] as const) {
        const fields = (doc[side] as Record<string, Json>) ?? {};
        draft.fieldOrder[side] = Object.keys(fields);
        for (const [n, raw] of Object.entries(fields)) {
          const spec = { ...(raw ?? {}) };
          if (side === "input" && spec.derived) {            // filled by a preprocessor: not a caller input
            draft.fieldExtras[side][n] = { flags: {}, full: spec };
            continue;
          }
          if (side === "input") draft.addInput(n, spec); else draft.addOutput(n, spec);
        }
      }
      const impl = (doc.implementation as Json) ?? {};
      const extras = { top: Object.fromEntries(Object.entries(doc).filter(([k]) => !MANAGED_TOP.includes(k))),
                       impl: Object.fromEntries(Object.entries(impl).filter(([k]) => !MANAGED_IMPL.includes(k))) };
      draft.extraTop = extras.top;
      draft.extraImpl = extras.impl;
      if (((impl.execution as Json)?.mode) === "agent") draft.setMode("agent");
      for (const step of (impl.flow as string[]) ?? []) draft.addStep(step);
      const sections = (impl.sections as Json) ?? {};
      if (sections.role) draft.addPrompt(sections.role as string);
      for (const kind of ["definitions", "constraints", "edge_cases"] as const) {
        for (const text of (sections[kind] as string[]) ?? []) draft.addSection(kind, text);
      }
      for (const ex of (sections.examples as Json[]) ?? []) {
        draft.addExample({ polarity: ex.polarity as "positive" | "negative", input: ex.input as Json,
                           output: ex.output as Json, badValue: ex.bad_value, note: ex.note as string });
      }
      for (const t of (impl.tools as Json[]) ?? []) {
        if ("server" in t) { draft.tools.push({ ...t }); continue; }          // expanded by the platform; kept as written
        draft.addTool(t.name as string, { maxCalls: t.max_calls as number, requires: t.requires as string,
                                          effect: t.effect as ToolOptions["effect"], ref: t.ref as string,
                                          config: (t.config as Json) ?? undefined });
      }
      for (const sentence of (doc.rubric as string[]) ?? []) draft.addRubric(sentence);
      if (doc.memory) draft.setMemory(doc.memory as { per: string; remember?: string[]; never?: string[] });
    } finally {
      draft.loading = false;
    }
    // a draft made with loadDraft({ version }) remembers which version it grew from, so a stopped
    // script can't turn into an accidental overwrite of another draft
    const meta = (doc.meta as Json) ?? {};
    const number = (meta.editing ?? meta.based_on ?? meta.version) as number | undefined;
    if (draft.id !== undefined && typeof number === "number") {
      const versions = (await h.request<Json[]>("GET", `/api/functions/${draft.id}/versions`)) ?? [];
      const found = versions.find((v) => v.number === number);
      if (!found) throw new FnaiError(404, `${source}: it was based on v${number}, which no longer exists on the server.`);
      draft.pointAt(found);          // a draft that has been deployed since is now frozen: a new version
    }
    draft.autosave();
    return draft;
  }

  // ── the local draft file ──────────────────────────────────────────────────
  private setDraftPath(opts: DraftOptions): void {
    this.saveToCfg = opts.saveTo;
    this.draftsCfg = opts.draftsFolder;
  }

  /** The working draft file, or null when files are off. It is named for what you are editing, so two
   *  drafts of one function never share a file: `<slug>.yaml` (a new function or the latest),
   *  `<slug>.v3.yaml` (editing draft v3), `<slug>.from-v1.yaml` (a new version being built from v1).
   *  An exact `saveTo` file is used as given. */
  get draftPath(): string | null {
    const slug = this.slug ?? slugify(this.name ?? "function");
    const base = resolveDraftPath(this.saveToCfg, this.draftsCfg, slug);
    if (base === null) return null;
    if (this.saveToCfg && /\.ya?ml$/i.test(base)) return base;                 // an exact file
    const suffix = this.target ? `.v${this.target.number}` : this.base ? `.from-v${this.base.number}` : "";
    return suffix ? base.replace(/\.yaml$/, `${suffix}.yaml`) : base;
  }

  /** Where the copy of saved version `number` goes, or null when copies are off. */
  private versionRecordPath(number: number): string | null {
    if (this.saveToCfg === false || !nodePath || this.h.keepVersions === false) return null;
    return nodePath.join(versionsDir(this.h.versionsFolder, this.draftsCfg), this.slug ?? slugify(this.name ?? "function"), `v${number}.yaml`);
  }

  private fileYaml(): string {
    let meta = `meta:\n  function: ${yamlScalar(this.slug)}\n`;
    if (this.base) meta += `  based_on: ${this.base.number}\n`;
    if (this.target) meta += `  editing: ${this.target.number}\n`;
    return meta + this.toYaml();
  }

  /** Keep the draft file in step with the function. A problem writing it warns once and never
   *  stops the script. */
  private autosave(): void {
    if (!this.draftPath || this.loading) return;
    try {
      writeAtomic(this.draftPath, this.fileYaml());
    } catch (e) {
      if (!this.warned) {
        this.warned = true;
        console.warn(`fnai: couldn't write the draft file ${this.draftPath}: ${(e as Error).message}. ` +
          `Your work is still in memory.`);
      }
    }
  }

  /** After a successful save the server has the real copy. The working draft file becomes that version's
   *  copy (versions/<slug>/v<N>.yaml) instead of being deleted, so you keep a readable copy of every version
   *  you save. With copies off (`keepVersions: false`) it is just removed. */
  private finishSave(working: string | null, version: { number: number }): void {
    if (!nodeFs || !working) return;
    const record = this.versionRecordPath(version.number);
    if (record) {
      try {
        writeAtomic(record, `meta:\n  function: ${yamlScalar(this.slug)}\n  version: ${version.number}\n` + this.toYaml());
      } catch (e) {
        console.warn(`fnai: couldn't write the copy of v${version.number} at ${record}: ${(e as Error).message}`);
      }
    }
    try { nodeFs.rmSync(working, { force: true }); } catch { /* nothing to clean up */ }
  }

  /** Write the function as YAML to a path you choose, any time. A path ending in .yaml/.yml is
   *  the file; anything else is a folder, and the file is named after the function. Folders are
   *  created. Returns the path written. Unlike the automatic draft file, this copy stays after
   *  submit(). */
  saveYaml(path: string): string {
    if (!nodeFs || !nodePath) throw new Error("saveYaml needs Node.js");
    const p = nodePath;
    const expanded = path.replace(/^~(?=$|[\\/])/, nodeHome);
    const target = /\.ya?ml$/i.test(expanded) ? expanded : p.join(expanded, `${this.slug}.yaml`);
    try {
      writeAtomic(target, this.fileYaml());
    } catch (e) {
      throw new Error(`couldn't write ${target}: ${(e as Error).message}`);
    }
    return p.resolve(target);
  }

  // ── adding the parts ──────────────────────────────────────────────────────
  private addField(side: "input" | "output", fieldName: string, spec: Json): FunctionDraft {
    requireField(fieldName, "fieldName");
    const full: Json = { type: "string", required: false, ...spec };
    const problems = checkField(side, fieldName, full);
    const schema = side === "input" ? this.inputSchema : this.outputSchema;
    if (fieldName in ((schema.properties as Json) ?? {}) || fieldName in this.fieldExtras[side]) problems.push(`${side} '${fieldName}' is already defined`);
    if (problems.length) throw new DefinitionError(problems);
    schema.type ??= "object";
    ((schema.properties as Json) ??= {})[fieldName] = projectField(full);
    const req = ((schema.required as string[]) ?? []).filter((n) => n !== fieldName);
    if (full.required) req.push(fieldName);
    if (req.length) schema.required = req; else delete schema.required;
    if (full.sensitive === true) this.fieldExtras[side][fieldName] = { flags: { sensitive: true }, full: null };   // the stored schema can't hold it
    this.fieldOrder[side] = [...this.fieldOrder[side].filter((n) => n !== fieldName), fieldName];
    this.autosave();
    return this;
  }

  /** Attach one input field. Checked now against the definition rules — a mistake throws
   *  DefinitionError here, not later. Just remembered for now, not saved yet. */
  addInput(fieldName: string, spec: { type?: string; required?: boolean } & Json = {}): FunctionDraft {
    return this.addField("input", fieldName, spec);
  }

  /** Attach one output field. Checked now, same as addInput. Just remembered for now. */
  addOutput(fieldName: string, spec: { type?: string; required?: boolean } & Json = {}): FunctionDraft {
    return this.addField("output", fieldName, spec);
  }

  private removeField(side: "input" | "output", fieldName: string): FunctionDraft {
    requireField(fieldName, "fieldName");
    const schema = side === "input" ? this.inputSchema : this.outputSchema;
    const props = (schema.properties as Json) ?? {};
    if (!(fieldName in props) && !(fieldName in this.fieldExtras[side])) {
      throw new DefinitionError([`${side} '${fieldName}' is not defined, nothing to remove`]);
    }
    delete props[fieldName];
    const req = ((schema.required as string[]) ?? []).filter((n) => n !== fieldName);
    if (req.length) schema.required = req; else delete schema.required;
    delete this.fieldExtras[side][fieldName];
    this.fieldOrder[side] = this.fieldOrder[side].filter((n) => n !== fieldName);
    this.autosave();
    return this;
  }

  /** Take one input field out of this draft. Throws DefinitionError if there is no such field. Just
   *  remembered for now; an example that still uses the field is reported when you submit(). */
  removeInput(fieldName: string): FunctionDraft { return this.removeField("input", fieldName); }

  /** Take one output field out of this draft. Same rules as removeInput. */
  removeOutput(fieldName: string): FunctionDraft { return this.removeField("output", fieldName); }

  /** Take one step out of the flow. Pass its number (1 = the first step, as print lists them) or its exact text. */
  removeStep(step: number | string): FunctionDraft {
    if (typeof step === "number") {
      if (!Number.isInteger(step) || step < 1 || step > this.flow.length) {
        throw new DefinitionError([`step ${step} does not exist (the flow has ${this.flow.length} step(s))`]);
      }
      this.flow.splice(step - 1, 1);
    } else if (typeof step === "string" && this.flow.includes(step)) {
      this.flow.splice(this.flow.indexOf(step), 1);
    } else {
      throw new DefinitionError([`no step matches ${JSON.stringify(step)}`]);
    }
    this.autosave();
    return this;
  }

  /** Set the prompt: who the function is and what it does (the role section). Replaces any
   *  earlier prompt. Just remembered for now. */
  addPrompt(text: string): FunctionDraft {
    return this.addSection("role", text);
  }

  /** Attach a piece of prompt text. `kind` is "role" (replaces — a function has exactly one
   *  role), or "definitions" / "constraints" / "edge_cases" (appends — many of each).
   *  Just remembered for now. */
  addSection(kind: "role" | "definitions" | "constraints" | "edge_cases", content: string): FunctionDraft {
    requireField(content, "content");
    if (kind === "role") {
      this.sections.role = content;
    } else if (kind === "definitions" || kind === "constraints" || kind === "edge_cases") {
      (this.sections[kind] as string[]).push(content);
    } else {
      throw new Error(`kind must be "role", "definitions", "constraints", or "edge_cases", ` +
        `got ${JSON.stringify(kind)}.`);
    }
    this.autosave();
    return this;
  }

  /** Attach one worked example. A positive example needs input and output; a negative one needs
   *  output (or badValue) and a note. Checked now. Just remembered for now. */
  addExample(opts: { polarity?: "positive" | "negative"; input?: Json; output?: Json;
                     badValue?: unknown; note?: string } = {}): FunctionDraft {
    const polarity = opts.polarity ?? "positive";
    const problems = exampleProblems(polarity, opts.input, opts.output, opts.badValue, opts.note);
    if (problems.length) throw new DefinitionError(problems);
    const example: Json = { polarity };
    if (opts.input !== undefined) example.input = opts.input;
    if (opts.output !== undefined) example.output = opts.output;
    if (opts.badValue !== undefined) example.bad_value = opts.badValue;
    if (opts.note !== undefined) example.note = opts.note;
    (this.sections.examples as Json[]).push(example);
    this.autosave();
    return this;
  }

  /** Add one step the function follows, in order (the definition's `flow`). At least one is
   *  required. Just remembered for now. */
  addStep(text: string): FunctionDraft {
    requireField(text, "text");
    this.flow.push(text);
    this.autosave();
    return this;
  }

  /** Add one sentence describing what a good answer looks like. Use it when the output is free
   *  text, which a rule like "a number from 1 to 5" cannot judge. Just remembered for now. */
  addRubric(text: string): FunctionDraft {
    if (typeof text !== "string" || !text.trim()) throw new DefinitionError(["rubric: the sentence can't be empty"]);
    if (this.rubric.includes(text)) throw new DefinitionError([`rubric: this sentence is already added: ${JSON.stringify(text)}`]);
    this.rubric.push(text);
    this.autosave();
    return this;
  }

  /** Let the function remember things between runs. `per` is who shares one memory: "caller",
   *  "space" or "organisation". `remember` and `never` are optional lists of sentences saying
   *  what is worth keeping and what to keep out (guidance for the function, not a hard control).
   *  A second call replaces the first. Just remembered for now. */
  setMemory(opts: { per: string; remember?: string[]; never?: string[] }): FunctionDraft {
    const problems: string[] = [];
    if (!MEMORY_SCOPES.includes(opts.per)) {
      problems.push(`memory: per must be 'caller', 'space' or 'organisation', got ${JSON.stringify(opts.per)}`);
    }
    for (const label of ["remember", "never"] as const) {
      const items = opts[label];
      if (items !== undefined && !(Array.isArray(items) && items.every((i) => typeof i === "string" && i.trim()))) {
        problems.push(`memory: ${label} must be a list of non-empty sentences`);
      }
    }
    if (problems.length) throw new DefinitionError(problems);
    const mem: Json = { per: opts.per };
    if (opts.remember?.length) mem.remember = [...opts.remember];
    if (opts.never?.length) mem.never = [...opts.never];
    this.memory = mem;
    this.autosave();
    return this;
  }

  /** "single_shot" (one pass) or "agent" (loops, can use tools). Left alone, it is "agent"
   *  once a tool is added and "single_shot" otherwise. */
  setMode(mode: "single_shot" | "agent"): FunctionDraft {
    if (mode !== "single_shot" && mode !== "agent") {
      throw new Error(`mode must be "single_shot" or "agent", got ${JSON.stringify(mode)}.`);
    }
    this.mode = mode;
    this.autosave();
    return this;
  }

  /** Attach one tool it's allowed to use, with this function's own limits: `maxCalls` caps uses
   *  per run, `requires` locks the tool until a condition holds, `effect` may only tighten the
   *  tool's registered effect, `config` is this function's own settings for it. Checked now.
   *  Just remembered for now. */
  addTool(name: string, opts: ToolOptions = {}): FunctionDraft {
    requireField(name, "name");
    const problems: string[] = [];
    if (this.tools.some((t) => t.name === name)) problems.push(`tool '${name}' is already added`);
    if (opts.maxCalls !== undefined && !(Number.isInteger(opts.maxCalls) && opts.maxCalls >= 1)) {
      problems.push(`tool '${name}': maxCalls must be a whole number, 1 or more`);
    }
    if (opts.effect !== undefined && !EFFECTS.includes(opts.effect)) {
      problems.push(`tool '${name}': effect must be one of ${EFFECTS.join(", ")}, got ${JSON.stringify(opts.effect)}`);
    }
    if (opts.requires !== undefined) problems.push(...guardProblems(name, opts.requires));
    if (problems.length) throw new DefinitionError(problems);
    const entry: Json = { name };
    if (opts.config && Object.keys(opts.config).length) entry.config = opts.config;
    if (opts.maxCalls !== undefined) entry.max_calls = opts.maxCalls;
    if (opts.requires !== undefined) entry.requires = opts.requires;
    if (opts.effect !== undefined) entry.effect = opts.effect;
    if (opts.ref !== undefined) entry.ref = opts.ref;
    this.tools.push(entry);
    this.autosave();
    return this;
  }

  /** Test a sample input against this function's input rules without calling the server.
   *  Returns the problems found ([] means it passes). */
  checkInput(sample: Json): string[] {
    requireRecord(sample, "sample", { allowEmpty: true });
    return validateSample(this.inputSchema, sample);
  }

  // ── printing ──────────────────────────────────────────────────────────────
  protected printNote(): string {
    if (this.base) return `# based on v${this.base.number} — saving creates a new version\n`;
    if (this.target) return `# editing draft v${this.target.number} — saving updates it\n`;
    return "";
  }

  private effectiveMode(): string {
    return this.mode ?? (this.tools.length ? "agent" : "single_shot");
  }

  protected yamlParts(): YamlParts {
    const sections: Json = {};
    for (const [k, v] of Object.entries(this.sections)) {
      if (Array.isArray(v) ? v.length > 0 : v != null) sections[k] = v;
    }
    return {
      name: this.name || this.slug || "", description: this.description,
      input: this.dslFields("input"), output: this.dslFields("output"),
      mode: this.effectiveMode(), flow: this.flow, rubric: this.rubric, memory: this.memory,
      sections, tools: this.tools, extraTop: this.extraTop, extraImpl: this.extraImpl,
    };
  }

  protected missingLists(): { required: string[]; recommended: string[] } {
    const sections: Json = {};
    for (const [k, v] of Object.entries(this.sections)) {
      if (Array.isArray(v) ? v.length > 0 : v != null) sections[k] = v;
    }
    return missingLists(this.dslFields("input"), this.dslFields("output"), this.flow, sections);
  }

  // ── saving ────────────────────────────────────────────────────────────────
  /** The function as a v2 definition document: input and output in the definition's field form,
   *  plus implementation (mode, flow, sections, tools) — what the server validates and compiles. */
  private snapshot(): Json {
    const sections: Json = {};
    for (const [k, v] of Object.entries(this.sections)) {
      if (Array.isArray(v) ? v.length > 0 : v != null) sections[k] = v;
    }
    const snap: Json = {
      name: this.name,
      input: this.dslFields("input"),
      output: this.dslFields("output"),
      implementation: { execution: { mode: this.effectiveMode() }, ...this.extraImpl, flow: [...this.flow], sections, tools: this.tools },
    };
    if (this.description) snap.description = this.description;
    if (this.rubric.length) snap.rubric = [...this.rubric];
    if (this.memory) snap.memory = { ...this.memory };
    Object.assign(snap, this.extraTop);
    return snap;
  }

  /** Names in addTool() that don't match a tool this space can use. One GET at submit time;
   *  skipped if the lookup itself fails (the server rejects an unknown tool on submit anyway). */
  private async toolProblems(): Promise<string[]> {
    const names = this.tools.filter((t) => t.name && !t.ref).map((t) => t.name as string);
    if (!names.length) return [];
    let known: string[];
    try {
      const list = await this.h.request<Json[]>("GET", "/api/tools");
      known = [...new Set(list.filter((t) => [undefined, "active", "deprecated"].includes(t.status as string | undefined))
                              .map((t) => t.name as string))].sort();
    } catch { return []; }
    return names.filter((n) => !known.includes(n)).map((n) => {
      const near = closestMatch(n, known);
      return `tool '${n}' not found${near ? ` — did you mean '${near}'?` : ""} ` +
        `(available: ${known.join(", ") || "none"}; register your own with client.tools.create)`;
    });
  }

  private async structureProblems(): Promise<string[]> {
    const problems: string[] = [];
    if (!Object.keys((this.outputSchema.properties as Json) ?? {}).length) problems.push("no output fields — addOutput(...)");
    if (!this.flow.length) problems.push("no steps — addStep(...)");
    const hasServer = this.tools.some((t) => "server" in t);    // a server entry expands to tools we can't see here
    const declaredTools = hasServer ? undefined : new Set(this.tools.map((t) => t.name as string).filter(Boolean));
    const declaredInputs = new Set([...Object.keys((this.inputSchema.properties as Json) ?? {}), ...Object.keys(this.fieldExtras.input)]);
    for (const t of this.tools) {
      if (t.requires && t.name) problems.push(...guardProblems(t.name as string, t.requires as string, declaredTools, declaredInputs));
    }
    problems.push(...(await this.toolProblems()));
    const ins = declaredInputs;
    const outs = new Set(Object.keys((this.outputSchema.properties as Json) ?? {}));
    (this.sections.examples as Json[]).forEach((ex, i) => {
      for (const key of Object.keys((ex.input as Json) ?? {})) {
        if (ins.size && !ins.has(key)) problems.push(`example ${i + 1}: input field '${key}' is not declared (addInput)`);
      }
      for (const key of Object.keys((ex.output as Json) ?? {})) {
        if (outs.size && !outs.has(key)) problems.push(`example ${i + 1}: output field '${key}' is not declared (addOutput)`);
      }
    });
    return problems;
  }

  /** First save: create the function on the server. Remembered, so a retry after a failed save
   *  carries on with the same function instead of creating it twice. */
  private async ensureCreated(): Promise<void> {
    if (this.id !== undefined) return;
    try {
      const fn = await this.h.request<Json>("POST", "/api/functions", {
        slug: this.slug, name: this.name, description: this.description,
        input_schema: this.inputSchema, output_schema: this.outputSchema,
      });
      this.id = fn.id as number;
    } catch (e) {
      if (e instanceof FnaiError && e.status === 409) {
        throw new FnaiError(409, `a function with slug ${JSON.stringify(this.slug)} already exists in this organization ` +
          `(archived functions keep their name). Nothing was saved; your work is still in memory and in the draft ` +
          `file. Start again with a different slug: client.functions.new(name, { slug: "..." }).`);
      }
      throw e;
    }
  }

  /** This is Save. Everything remembered since the last save gets committed now. By default it updates
   *  the latest draft in place, or starts a new version if the latest is already deployed and frozen —
   *  the one rule that decision follows, used by the UI and every SDK caller alike
   *  (POST /api/functions/{id}/submit). A draft loaded with `loadDraft(..., { version: N })` saves to that
   *  version instead (a frozen one always becomes a NEW version).
   *
   *  `{ newVersion: true }` always creates a NEW version, even if the one you started from is a draft:
   *  that version is left exactly as it was, and the new one records it as its parent. Nothing is
   *  deployed. Later plain saves update the new draft in place.
   *
   *  Before sending, checks the whole function and throws one DefinitionError listing every
   *  problem. On success the local draft file becomes that version's copy (`.fnai/versions/<slug>/v<N>.yaml`). */
  async submit(message = "", opts: { newVersion?: boolean } = {}): Promise<S["VersionOut"]> {
    let newVersion = opts.newVersion ?? false;
    if (this.fresh) {
      if (opts.newVersion === false) {
        throw new Error("this draft was started with fresh: true, so it can only be saved as a NEW version " +
          "(it would overwrite the one it grew from). Drop newVersion: false.");
      }
      newVersion = true;
    }
    const problems = await this.structureProblems();
    if (problems.length) throw new DefinitionError(problems);
    if (this.fresh) {
      const shown = this.changes();
      console.log(shown.length ? "Changes from the version you started from:" : "No changes from the version you started from.");
      for (const line of shown) console.log("  " + line);
    }
    await this.ensureCreated();
    const working = this.draftPath;      // the file you were working in, before a save changes what you are editing
    const body = { snapshot: this.snapshot(), message, input_schema: this.inputSchema, output_schema: this.outputSchema };
    let version: S["VersionOut"];
    if (newVersion) {
      // branch: always a NEW version; the one it grew from is left untouched
      const parent = this.target ?? this.base ?? (this.lastVersion as unknown as Json | null);
      version = await this.h.request<S["VersionOut"]>("POST", `/api/functions/${this.id}/versions`,
        parent ? { ...body, parent_id: parent.id } : body);
      this.target = version as unknown as Json;      // it is a draft now: later saves update it
      this.base = null;
    } else if (this.target) {
      // a draft chosen with load({ version }): edit it in place
      try {
        version = await this.h.request<S["VersionOut"]>("PUT", `/api/versions/${this.target.id}`, body);
      } catch (e) {
        if (e instanceof FnaiError && e.status === 409) {
          throw new FnaiError(409, `v${this.target.number} has been deployed since you loaded it, so it can no longer be ` +
            `edited. Your work is still in memory and in the draft file. Load it again to base a new version on it: ` +
            `loadDraft(..., { version: ${this.target.number} }).`);
        }
        throw e;
      }
    } else if (this.base) {
      // based on a frozen version: always a NEW version, never an overwrite
      version = await this.h.request<S["VersionOut"]>("POST", `/api/functions/${this.id}/versions`,
        { ...body, parent_id: this.base.id });
      this.target = version as unknown as Json;      // it is a draft now: later saves update it
      this.base = null;
    } else {
      version = await this.h.request<S["VersionOut"]>("POST", `/api/functions/${this.id}/submit`, body);
    }
    this.lastVersion = version;
    this.fresh = false;                   // saved: later plain saves update the new draft
    this.baseline = this.outline();
    this.finishSave(working, version);    // the draft file becomes this version's copy
    return version;
  }

  /** Make a version live. Deploys the version from the most recent submit() — call submit()
   *  first if there are unsaved addX changes. Pass `model` to choose what production calls run on;
   *  without one the space's default model is used, and with neither the deploy is refused. */
  async deploy(note = "", opts: { model?: string; temperature?: number } = {}): Promise<S["DeploymentOut"]> {
    if (this.lastVersion === null) {
      throw new Error("nothing to deploy yet — call submit() first.");
    }
    const body: Json = { version_id: this.lastVersion.id, note };
    if (opts.model) body.model = opts.model;
    if (opts.temperature !== undefined) body.temperature = opts.temperature;
    return this.h.request("POST", `/api/functions/${this.id}/deploy`, body);
  }
}

export interface ClientOptions {
  token: string;
  baseUrl?: string;
  timeoutMs?: number;
  /** Where draft files go (default `.fnai/drafts` next to where the script runs). */
  draftsFolder?: string;
  /** Where a copy of every saved version goes (default `.fnai/versions`). */
  versionsFolder?: string;
  /** false: no copies of saved versions; the draft file is just removed on save. Default true. */
  keepVersions?: boolean;
}

/** Namespaced management client. Auth: personal access token (fnp_*). */
export class Client {
  private http: Http;
  private baseUrl: string;
  private timeoutMs: number;
  private draftsFolder?: string;

  functions: Functions;
  spaces: Spaces;
  versions: Versions;
  eval: Eval;
  datasets: Datasets;
  deployments: Deployments;
  tools: Tools;
  keys: Keys;
  models: Models;
  traces: Traces;
  auth: Auth;

  constructor(opts: ClientOptions) {
    this.baseUrl = opts.baseUrl ?? "http://localhost:8000";
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.http = new Http(this.baseUrl, opts.token, this.timeoutMs);
    this.draftsFolder = opts.draftsFolder;
    this.http.versionsFolder = opts.versionsFolder;
    this.http.keepVersions = opts.keepVersions ?? true;
    this.functions = new Functions(this.http, opts.draftsFolder);
    this.spaces = new Spaces(this.http);
    this.versions = new Versions(this.http);
    this.eval = new Eval(this.http);
    this.datasets = new Datasets(this.http);
    this.deployments = new Deployments(this.http);
    this.tools = new Tools(this.http);
    this.keys = new Keys(this.http);
    this.models = new Models(this.http);
    this.traces = new Traces(this.http);
    this.auth = new Auth(this.http);
  }

  me(): Promise<S["MeOut"]> {
    return this.http.request("GET", "/api/me");
  }

  /** Start building a function through the shared create/addX/submit/deploy
   *  flow (FUNCTION_CLASS_DESIGN.md) — the same actions the canvas UI
   *  performs. See FunctionDraft for the full method list. */
  draft(opts: { name: string; slug?: string; description?: string } & DraftOptions): Promise<FunctionDraft> {
    return FunctionDraft.create(this.http, { draftsFolder: this.draftsFolder, ...opts });
  }

  /** Resume an existing function through that same flow — e.g. to
   *  addOutput() a new field after it's already deployed. Pass its id or its slug: client.loadDraft("my_function").
   *  By default you get the latest version; `{ version: N }` starts from version N. `{ fresh: true }` keeps the
   *  function but starts it empty (always saved as a NEW version; submit() lists what was dropped first). */
  loadDraft(fnRef: number | string, opts: LoadOptions = {}): Promise<FunctionDraft> {
    return FunctionDraft.load(this.http, fnRef, { draftsFolder: this.draftsFolder, ...opts });
  }

  /** Write EVERY saved version of a function as YAML: v1.yaml, v2.yaml, ... Read from the server, so it is
   *  complete and exact, including versions made in the app or by a teammate. Pass the function's id or slug.
   *  Default folder: `.fnai/versions/<slug>`. Returns the paths written (oldest first). The server is the
   *  source of truth: run it again to refresh the copies. */
  async exportYaml(fnRef: number | string, folder?: string): Promise<string[]> {
    await loadNode();
    if (!nodeFs || !nodePath) throw new Error("exportYaml needs Node.js");
    const fnId = await resolveFunctionId(this.http, fnRef);
    const fn = await this.http.request<Json>("GET", `/api/functions/${fnId}`);
    const versions = ((await this.http.request<Json[]>("GET", `/api/functions/${fnId}/versions`)) ?? [])
      .sort((a, b) => (a.number as number) - (b.number as number));
    if (!versions.length) throw new FnaiError(404, `function ${JSON.stringify(fn.slug)} has no versions yet`);
    const target = folder ? nodePath.resolve(folder.replace(/^~(?=$|[\\/])/, nodeHome))
      : nodePath.join(versionsDir(this.http.versionsFolder, this.draftsFolder), fn.slug as string);
    const paths: string[] = [];
    for (const v of versions) {
      const text = await this.http.requestText("GET", `/api/versions/${v.id}/yaml`);
      const path = nodePath.join(target, `v${v.number}.yaml`);
      writeAtomic(path, text);
      paths.push(path);
    }
    return paths;
  }

  /** Read a function back from a YAML file (a draft file, or a copy made with saveYaml) and carry
   *  on building it. Pass the file's path, or just the function's name to look in the drafts
   *  folder. The file is the whole truth: it replaces what the draft held. If the function already
   *  exists on the server (matched by the file's meta.function) you continue it; otherwise it is
   *  created. Needs js-yaml (npm install js-yaml). */
  async loadYaml(pathOrName: string, opts: { saveTo?: string | false } = {}): Promise<FunctionDraft> {
    await loadNode();
    if (!nodeFs || !nodePath) throw new Error("loadYaml needs Node.js");
    let yaml: { load: (text: string) => unknown };
    try {
      yaml = (await import("js-yaml")) as unknown as { load: (text: string) => unknown };
    } catch {
      throw new Error("Loading a YAML file needs js-yaml: npm install js-yaml (saving YAML does not need it).");
    }
    let file = nodePath.resolve(pathOrName.replace(/^~(?=$|[\\/])/, nodeHome));
    if (!nodeFs.existsSync(file) || !nodeFs.statSync(file).isFile()) {
      const folder = draftsDir(this.draftsFolder);
      const slug = slugify(pathOrName);
      // a draft is named for what it edits: <slug>.yaml, <slug>.v3.yaml, <slug>.from-v1.yaml
      const own = new RegExp(`^${slug.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(\\.(v|from-v)\\d+)?\\.yaml$`);
      const names = nodeFs.existsSync(folder) ? nodeFs.readdirSync(folder).filter((n) => own.test(n)).sort() : [];
      const matches = names.includes(`${slug}.yaml`) ? [`${slug}.yaml`] : names;
      if (!matches.length) throw new Error(`no YAML file at ${file} and no draft named ${JSON.stringify(pathOrName)} in ${folder}`);
      if (matches.length > 1) throw new Error(`${matches.length} drafts match ${JSON.stringify(pathOrName)} in ${folder}: ${matches.join(", ")} — pass the file's path`);
      file = nodePath.join(folder, matches[0]!);
    }
    const doc = yaml.load(nodeFs.readFileSync(file, "utf8")) as Json | null;
    if (!isPlainRecord(doc) || !doc.name) throw new DefinitionError([`${file}: the file must be YAML with at least a \`name\``]);
    const slug = ((doc.meta as Json | undefined)?.function as string | undefined) || slugify(doc.name as string);
    const existing = (await this.functions.list()).find((f) => f.slug === slug);
    // a function that is not on the server yet is created at submit()
    const fn = (existing ?? { slug, name: doc.name, description: (doc.description as string) ?? "" }) as unknown as Json;
    return FunctionDraft.fromDocument(this.http, fn, doc, { saveTo: opts.saveTo, draftsFolder: this.draftsFolder }, file);
  }

  /** Scope every later space-scoped call to one space by name (the
   *  `X-Space` header get_scope reads — see
   *  backend/app/auth_usermanagement/security/scope.py), instead of
   *  whatever this token's default space resolves to. `name` is a space's
   *  `name` from me().spaces, not its id.
   *
   *  Opt-in: if you never call this, nothing changes — no X-Space header is
   *  ever sent, same as before this method existed, and the backend
   *  resolves its own default space exactly as it always has.
   *
   *  Re-validates `name` against a FRESH GET /api/me every call — raising
   *  SpaceNotFoundError rather than accepting a name nobody checked. Pass
   *  null to clear the override. */
  useSpace(name: string | null): Promise<void> {
    return this.http.useSpace(name);
  }

  /** The space name useSpace() last set, or null if nothing has been
   *  selected (the default state — see useSpace()). */
  get currentSpace(): string | null {
    return this.http.space;
  }

  /** Cross over to the runtime surface: a Function handle on this base URL. */
  function(slug: string, apiKey: string): FnaiFunction {
    return new FnaiFunction(slug, { apiKey, baseUrl: this.baseUrl,
                                    timeoutMs: this.timeoutMs });
  }
}

// ── the v2 DSL starter file (REQ-276) — a ready-made, valid example, so the
// first thing a new user edits is a real function, not a blank page. ──────

export const STARTER_YAML = `name: Summarize Support Ticket
description: Produces a one-paragraph summary and urgency score for a support ticket.

input:
  ticket_text: { type: string, required: true }

output:
  summary: { type: string, required: true }
  urgency: { type: integer, required: true, minimum: 1, maximum: 5 }

implementation:
  execution:
    mode: single_shot
  flow:
    - "Summarize the ticket in one paragraph, plain language."
    - "Score urgency from the ticket's severity."
  sections:
    role: You are a support-ticket triage assistant.
`;

/**
 * A ready-made, structurally valid starting example (REQ-276) — zero setup,
 * nothing to look up first. Writes it to `path` (default function.yaml in
 * the current directory, never overwriting an existing file by accident) and
 * returns the path actually written.
 *
 *     const path = await writeStarterYaml();
 *     const { fn, version } = await client.functions.createFromYaml(path);   // works as-is
 */
export async function writeStarterYaml(path = "function.yaml"): Promise<string> {
  const fs = await import("node:fs/promises");
  let exists = true;
  try {
    await fs.access(path);
  } catch {
    exists = false;   // fs.access rejects exactly when the file is absent
  }
  if (exists) {
    throw new Error(`${path} already exists — pick a different path, or edit it directly`);
  }
  await fs.writeFile(path, STARTER_YAML, "utf8");
  return path;
}

// ── legacy prompt-file import (pre-v2) — same .yaml/.yml/.json shape the
// canvas UI's Import button ALSO still reads for a legacy (non-v2) draft
// (frontend/src/pages/canvas/yamlImport.ts). Superseded by createFromYaml()
// above for anything new — kept working, unchanged, for existing callers,
// the same "leave the legacy path alone" choice the backend and canvas both
// made (see REQ-351's note). Deliberately two separate steps:
// parsePromptFile() only reads a file and validates it — no network call,
// nothing created — createFunctionFromDoc() is the one that actually talks
// to the API. ────────────────────────────────────────────────────────────

export class PromptFileError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PromptFileError";
  }
}

const ITEM_KEYS = ["definitions", "instructions", "examples",
                   "negative_examples", "edge_cases", "non_goals"] as const;
const LABEL: Record<string, string> = {
  definitions: "Definitions", instructions: "Instructions", examples: "Examples",
  negative_examples: "Negative examples", edge_cases: "Edge cases", non_goals: "Non-goals",
};
const KNOWN_KEYS = new Set<string>(["role", "freeform", "inputs", "outputs", ...ITEM_KEYS]);
const FIELD_TYPES = new Set(["string", "number", "integer", "boolean", "array", "object"]);

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function parseFieldMap(raw: unknown, label: string): Json {
  if (!isPlainObject(raw)) throw new PromptFileError(`${label} must be a mapping of field name to type.`);
  const properties: Json = {};
  const required: string[] = [];
  for (const [name, spec] of Object.entries(raw)) {
    let type: unknown, description: unknown, isRequired = true;
    if (typeof spec === "string") {
      type = spec;
    } else if (isPlainObject(spec)) {
      type = spec.type;
      description = spec.description;
      if (spec.required !== undefined) isRequired = !!spec.required;
    } else {
      throw new PromptFileError(`${label}.${name} must be a type name or {type, description, required}.`);
    }
    if (typeof type !== "string" || !FIELD_TYPES.has(type)) {
      throw new PromptFileError(`${label}.${name}: type must be one of ${[...FIELD_TYPES].join(", ")}.`);
    }
    if (description !== undefined && typeof description !== "string") {
      throw new PromptFileError(`${label}.${name}.description must be text.`);
    }
    properties[name] = description ? { type, description } : { type };
    if (isRequired) required.push(name);
  }
  return { type: "object", required, properties };
}

export interface PromptDoc {
  sections: Json[];
  input_schema?: Json;
  output_schema?: Json;
}

/**
 * Parse a canvas-format prompt file into `{sections, input_schema?, output_schema?}`.
 *
 * Read-and-validate only — makes no network calls and creates nothing.
 * Throws PromptFileError naming exactly what's missing/wrong if the file
 * doesn't satisfy the format (role is required; inputs:/outputs:, if
 * present, must be a field-name -> type mapping; unknown top-level keys are
 * rejected rather than silently dropped).
 *
 * .json parses with no extra package. .yaml/.yml needs `js-yaml` installed
 * separately — this SDK has zero required runtime dependencies, so that's an
 * opt-in you add yourself (`npm install js-yaml`) only if you use YAML files.
 */
export async function parsePromptFile(path: string): Promise<PromptDoc> {
  const fs = await import("node:fs/promises");
  let text: string;
  try {
    text = await fs.readFile(path, "utf8");
  } catch {
    throw new PromptFileError(`file not found: ${path}`);
  }

  let doc: unknown;
  if (path.toLowerCase().endsWith(".json")) {
    try {
      doc = JSON.parse(text);
    } catch (e) {
      throw new PromptFileError(`not valid JSON — ${(e as Error).message}`);
    }
  } else {
    let yaml: { load: (s: string) => unknown };
    try {
      // @ts-ignore — optional dependency, not in this package's own deps
      yaml = await import("js-yaml");
    } catch {
      throw new PromptFileError(
        "parsing a .yaml file needs js-yaml (`npm install js-yaml`) — " +
        "or save the file as .json instead, which needs no extra package");
    }
    try {
      doc = yaml.load(text);
    } catch (e) {
      throw new PromptFileError(`not valid YAML — ${(e as Error).message}`);
    }
  }

  if (!isPlainObject(doc)) {
    throw new PromptFileError("the file must be an object/mapping (key: value pairs) " +
      "at the top level, not a list or plain text.");
  }

  const unknown = Object.keys(doc).filter((k) => !KNOWN_KEYS.has(k));
  if (unknown.length) {
    throw new PromptFileError(`unknown section${unknown.length > 1 ? "s" : ""}: ${unknown.join(", ")}. ` +
      `expected: role, ${ITEM_KEYS.join(", ")}, freeform, inputs, outputs.`);
  }

  const role = doc.role;
  if (typeof role !== "string" || !role.trim()) {
    throw new PromptFileError("role is required and must be non-empty text.");
  }

  const sections: Json[] = [{ type: "role", content: role }];

  for (const key of ITEM_KEYS) {
    const raw = doc[key];
    if (raw === undefined) continue;
    if (!Array.isArray(raw) || raw.some((it) => typeof it !== "string" || !it.trim())) {
      throw new PromptFileError(`${key} must be a list of non-empty text lines.`);
    }
    if (raw.length) sections.push({ type: key, title: LABEL[key], items: raw });
  }

  const rawFree = doc.freeform;
  if (rawFree !== undefined) {
    if (!Array.isArray(rawFree)) {
      throw new PromptFileError("freeform must be a list of {title, content} blocks.");
    }
    for (const block of rawFree) {
      if (!isPlainObject(block) || typeof block.content !== "string" || !block.content.trim()) {
        throw new PromptFileError("each freeform block needs a non-empty content field (title is optional).");
      }
      const title = block.title;
      sections.push({
        type: "freeform",
        title: typeof title === "string" && title.trim() ? title : "Freeform",
        content: block.content,
      });
    }
  }

  const result: PromptDoc = { sections };
  if (doc.inputs !== undefined) result.input_schema = parseFieldMap(doc.inputs, "inputs");
  if (doc.outputs !== undefined) result.output_schema = parseFieldMap(doc.outputs, "outputs");
  return result;
}

export interface CreatedFunction {
  fn: S["FunctionOut"];
  version: S["VersionOut"];
}

async function createFunctionAndVersion(client: Client, opts: {
  slug: string; name: string; sections: Json[];
  inputSchema?: Json; outputSchema?: Json;
  model?: string; temperature?: number; extra?: Json;
}): Promise<CreatedFunction> {
  const fn = await client.functions.create({
    slug: opts.slug, name: opts.name,
    input_schema: opts.inputSchema ?? { type: "object", properties: {} },
    output_schema: opts.outputSchema ?? { type: "object", properties: {} },
    ...(opts.extra ?? {}),
  } as unknown as S["FunctionCreate"]);
  const version = await client.functions.createVersion(fn.id, {
    snapshot: {
      impl: "prompt", tools: [], sections: opts.sections,
      model_binding: { model: opts.model ?? "mock-small", temperature: opts.temperature ?? 0.2 },
    },
  } as unknown as S["VersionCreate"]);
  return { fn, version };
}

/**
 * Create a function + its first version from data already parsed by
 * parsePromptFile(). This is the step that makes network calls — it never
 * reads a file itself, so callers can inspect or edit `doc` in between the
 * two. `doc` supplies the prompt's sections and, if the file declared
 * inputs:/outputs:, their schemas (an empty object schema is used for
 * whichever one the file omitted). `slug`/`name` are yours to choose, not
 * derived from the file.
 *
 * For creating a function from sections/schemas you already have in code —
 * not loaded from a file — use createFunction() instead.
 */
export function createFunctionFromDoc(client: Client, opts: {
  slug: string; name: string; doc: PromptDoc;
  model?: string; temperature?: number; extra?: Json;
}): Promise<CreatedFunction> {
  return createFunctionAndVersion(client, {
    slug: opts.slug, name: opts.name, sections: opts.doc.sections,
    inputSchema: opts.doc.input_schema, outputSchema: opts.doc.output_schema,
    model: opts.model, temperature: opts.temperature, extra: opts.extra,
  });
}

/**
 * Create a function + its first version from sections/schemas you supply
 * directly — the manual counterpart to createFunctionFromDoc(), for when you
 * aren't loading a prompt from a file. inputSchema/outputSchema each default
 * to an empty object schema if omitted.
 */
export function createFunction(client: Client, opts: {
  slug: string; name: string; sections: Json[];
  inputSchema?: Json; outputSchema?: Json;
  model?: string; temperature?: number; extra?: Json;
}): Promise<CreatedFunction> {
  return createFunctionAndVersion(client, opts);
}
