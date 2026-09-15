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

type S = components["schemas"];

export const VERSION = "0.3.0";
const DEFAULT_TIMEOUT_MS = 90_000; // server caps execution at 60s; transport headroom

export class FnaiError extends Error {
  constructor(public status: number, public detail: string) {
    super(`HTTP ${status}: ${detail}`);
    this.name = "FnaiError";
  }
}

type Json = Record<string, unknown>;

class Http {
  constructor(private base: string, private token: string,
              private timeoutMs: number) {
    this.base = base.replace(/\/+$/, "");
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
        headers: {
          Authorization: `Bearer ${this.token}`,
          "Content-Type": "application/json",
          "User-Agent": `fnai-ts/${VERSION}`,
        },
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
        if (parsed && parsed.detail !== undefined) detail = String(parsed.detail);
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
        headers: {
          Authorization: `Bearer ${this.token}`,
          "Content-Type": contentType,
          "User-Agent": `fnai-ts/${VERSION}`,
        },
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
        if (parsed && parsed.detail !== undefined) detail = String(parsed.detail);
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

  /** Invoke the active deployment; `input` is validated server-side. */
  async run(input: Json): Promise<RunResult> {
    const body = await this.http.request<S["RunOut"]>(
      "POST", `/v1/run/${this.slug}`, { input });
    return new RunResult((body.output ?? null) as Json | null,
                         body.status ?? "failed", body.meta,
                         body.error ?? null);
  }
}

export { FnaiFunction as Function };

// ── management surface (fnp_* personal access tokens) ───────────────────────

class Functions {
  constructor(private h: Http) {}

  list(): Promise<S["FunctionOut"][]> {
    return this.h.request("GET", "/api/functions");
  }

  get(fnId: number): Promise<S["FunctionOut"]> {
    return this.h.request("GET", `/api/functions/${fnId}`);
  }

  create(body: S["FunctionCreate"]): Promise<S["FunctionOut"]> {
    return this.h.request("POST", "/api/functions", body as unknown as Json);
  }

  update(fnId: number, fields: S["FunctionPatch"]): Promise<S["FunctionOut"]> {
    return this.h.request("PATCH", `/api/functions/${fnId}`, fields as unknown as Json);
  }

  archive(fnId: number): Promise<void> {
    return this.h.request("DELETE", `/api/functions/${fnId}`);
  }

  versions(fnId: number): Promise<S["VersionOut"][]> {
    return this.h.request("GET", `/api/functions/${fnId}/versions`);
  }

  createVersion(fnId: number, body: S["VersionCreate"]): Promise<S["VersionOut"]> {
    return this.h.request("POST", `/api/functions/${fnId}/versions`, body as unknown as Json);
  }

  health(fnId: number, windowHours = 24): Promise<S["HealthOut"]> {
    return this.h.request("GET", `/api/functions/${fnId}/health`,
                          undefined, { window_hours: windowHours });
  }

  /** `fn types` — caller-side types for the function's deployed contract. */
  types(fnId: number, lang: "typescript" | "python" = "typescript"):
      Promise<S["TypesOut"]> {
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
   *  editing what getYaml() returned) rather than freshly read from disk. */
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
    const fn = existing ?? await this.create(
      { slug, name: doc.name } as unknown as S["FunctionCreate"]);
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

class Versions {
  constructor(private h: Http) {}

  get(vId: number): Promise<S["VersionOut"]> {
    return this.h.request("GET", `/api/versions/${vId}`);
  }

  update(vId: number, fields: S["VersionUpdate"]): Promise<S["VersionOut"]> {
    return this.h.request("PUT", `/api/versions/${vId}`, fields as unknown as Json);
  }

  artifact(vId: number): Promise<Json> {
    return this.h.request("GET", `/api/versions/${vId}/artifact`);
  }

  playground(vId: number, opts: { input: Json; model?: string; temperature?: number }):
      Promise<S["PlaygroundOut"]> {
    const body: Json = { input: opts.input, model: opts.model ?? "mock-small" };
    if (opts.temperature !== undefined) body.temperature = opts.temperature;
    return this.h.request("POST", `/api/versions/${vId}/playground/run`, body);
  }

  gate(vId: number): Promise<S["PlanOut"]> {
    return this.h.request("POST", `/api/versions/${vId}/gate`, {});
  }

  plan(vId: number): Promise<S["PlanOut"]> {
    return this.h.request("GET", `/api/versions/${vId}/plan`);
  }
}

class Plans {
  constructor(private h: Http) {}

  list(fnId: number): Promise<S["PlanBriefOut"][]> {
    return this.h.request("GET", `/api/functions/${fnId}/plans`);
  }

  create(fnId: number, opts: { name: string }): Promise<S["PlanCreatedOut"]> {
    return this.h.request("POST", `/api/functions/${fnId}/plans`, { name: opts.name });
  }

  update(planId: number, fields: Json): Promise<S["PlanCreatedOut"]> {
    return this.h.request("PATCH", `/api/plans/${planId}`, fields);
  }

  accept(planId: number, body: Json = {}): Promise<S["AcceptPlanOut"]> {
    return this.h.request("POST", `/api/plans/${planId}/accept`, body);
  }

  clone(planId: number, body: Json = {}): Promise<S["PlanCreatedOut"]> {
    return this.h.request("POST", `/api/plans/${planId}/clone`, body);
  }

  delete(planId: number): Promise<S["DeletePlanOut"]> {
    return this.h.request("DELETE", `/api/plans/${planId}`);
  }

  scores(planId: number): Promise<S["ScoreOut"][]> {
    return this.h.request("GET", `/api/plans/${planId}/scores`);
  }

  addScore(planId: number, body: Json): Promise<S["ScoreOut"]> {
    return this.h.request("POST", `/api/plans/${planId}/scores`, body);
  }

  removeScore(planId: number, scoreId: number): Promise<S["RemovePlanScoreOut"]> {
    return this.h.request("DELETE", `/api/plans/${planId}/scores/${scoreId}`);
  }

  catalogue(): Promise<S["CatalogueEntryOut"][]> {
    return this.h.request("GET", "/api/catalogue");
  }
}

class Experiments {
  constructor(private h: Http) {}

  list(fnId: number): Promise<S["ExperimentOut"][]> {
    return this.h.request("GET", `/api/functions/${fnId}/experiments`);
  }

  get(expId: number): Promise<S["ExperimentOut"]> {
    return this.h.request("GET", `/api/experiments/${expId}`);
  }

  create(fnId: number, pins: S["ExperimentCreate"]): Promise<S["ExperimentOut"]> {
    return this.h.request("POST", `/api/functions/${fnId}/experiments`, pins as unknown as Json);
  }

  update(expId: number, fields: S["ExperimentPatch"]): Promise<S["ExperimentOut"]> {
    return this.h.request("PATCH", `/api/experiments/${expId}`, fields as unknown as Json);
  }

  run(expId: number): Promise<S["ExperimentOut"]> {
    return this.h.request("POST", `/api/experiments/${expId}/run`, {});
  }

  clone(expId: number): Promise<S["ExperimentOut"]> {
    return this.h.request("POST", `/api/experiments/${expId}/clone`, {});
  }

  delete(expId: number): Promise<void> {
    return this.h.request("DELETE", `/api/experiments/${expId}`);
  }

  findings(expId: number): Promise<S["FindingOut"][]> {
    return this.h.request("GET", `/api/experiments/${expId}/findings`);
  }
}

class Scores {
  constructor(private h: Http) {}

  list(fnId: number): Promise<S["ScoreOut"][]> {
    return this.h.request("GET", `/api/functions/${fnId}/scores`);
  }

  create(fnId: number, body: S["ScoreCreate"]): Promise<S["ScoreOut"]> {
    return this.h.request("POST", `/api/functions/${fnId}/scores`, body as unknown as Json);
  }

  results(fnId: number, filters: Record<string, string | number | undefined> = {}):
      Promise<S["ScoreResultOut"][]> {
    return this.h.request("GET", `/api/functions/${fnId}/score-results`, undefined, filters);
  }

  setGate(scoreId: number, gate: boolean): Promise<S["ScoreOut"]> {
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
    return this.h.request("GET", `/api/functions/${fnId}/datasets`, undefined,
                          includeArchived ? { include_archived: "true" } : undefined);
  }

  create(fnId: number, opts: { name: string; rows?: Json[] }): Promise<S["DatasetOut"]> {
    return this.h.request("POST", `/api/functions/${fnId}/datasets`,
                          { name: opts.name, rows: opts.rows ?? [] });
  }

  update(dsId: number, fields: S["DatasetPatch"]): Promise<S["DatasetOut"]> {
    return this.h.request("PATCH", `/api/datasets/${dsId}`, fields as unknown as Json);
  }

  delete(dsId: number): Promise<void> {
    return this.h.request("DELETE", `/api/datasets/${dsId}`);
  }

  rows(dsId: number): Promise<S["RowOut"][]> {
    return this.h.request("GET", `/api/datasets/${dsId}/rows`);
  }

  addRow(dsId: number, row: S["RowIn"]): Promise<S["RowOut"]> {
    return this.h.request("POST", `/api/datasets/${dsId}/rows`, row as unknown as Json);
  }

  snapshot(dsId: number): Promise<S["SnapshotOut"]> {
    return this.h.request("POST", `/api/datasets/${dsId}/snapshot`, {});
  }

  snapshots(dsId: number): Promise<S["SnapshotHistoryOut"][]> {
    return this.h.request("GET", `/api/datasets/${dsId}/snapshots`);
  }
}

class Deployments {
  constructor(private h: Http) {}

  deploy(fnId: number, versionId: number): Promise<S["DeploymentOut"]> {
    return this.h.request("POST", `/api/functions/${fnId}/deploy`,
                          { version_id: versionId });
  }

  current(fnId: number): Promise<S["DeploymentOut"]> {
    return this.h.request("GET", `/api/functions/${fnId}/deployment`);
  }

  history(fnId: number): Promise<S["DeploymentOut"][]> {
    return this.h.request("GET", `/api/functions/${fnId}/deployments`);
  }

  rollback(fnId: number, versionId?: number): Promise<S["DeploymentOut"]> {
    return this.h.request("POST", `/api/functions/${fnId}/rollback`,
                          versionId !== undefined ? { version_id: versionId } : {});
  }
}

/** BYOK provider credentials (org/space scope) — model calls run on these. */
class ProviderKeys {
  constructor(private h: Http) {}

  list(): Promise<S["KeyOut"][]> {
    return this.h.request("GET", "/api/provider-keys");
  }

  add(body: S["KeyCreate"]): Promise<S["KeyOut"]> {
    return this.h.request("POST", "/api/provider-keys", body as unknown as Json);
  }

  update(keyId: number, fields: S["KeyPatch"]): Promise<S["KeyOut"]> {
    return this.h.request("PATCH", `/api/provider-keys/${keyId}`, fields as unknown as Json);
  }

  test(keyId: number): Promise<S["KeyTestOut"]> {
    return this.h.request("POST", `/api/provider-keys/${keyId}/test`, {});
  }

  rotate(keyId: number, secret: Json): Promise<S["KeyOut"]> {
    return this.h.request("POST", `/api/provider-keys/${keyId}/rotate`, { secret });
  }

  delete(keyId: number): Promise<void> {
    return this.h.request("DELETE", `/api/provider-keys/${keyId}`);
  }
}

/** Per-function fnk_* keys for the public runtime — plaintext returned once. */
class InvokeKeys {
  constructor(private h: Http) {}

  list(fnId: number): Promise<S["InvokeKeyOut"][]> {
    return this.h.request("GET", `/api/functions/${fnId}/invoke-keys`);
  }

  mint(fnId: number, name = ""): Promise<S["InvokeKeyMintedOut"]> {
    return this.h.request("POST", `/api/functions/${fnId}/invoke-keys`, { name });
  }

  revoke(keyId: number): Promise<void> {
    return this.h.request("DELETE", `/api/invoke-keys/${keyId}`);
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
    return this.h.request("GET", "/api/traces", undefined, {
      function_id: opts.functionId, source: opts.source, limit: opts.limit ?? 50,
    });
  }

  get(traceId: string): Promise<S["TraceDetailOut"]> {
    return this.h.request("GET", `/api/traces/${traceId}`);
  }

  /** The flywheel: production trace → regression dataset row. */
  flag(traceId: string): Promise<S["TraceFlagOut"]> {
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
    return this.h.request("POST", "/api/auth/tokens", { name });
  }

  revoke(tokenId: string): Promise<void> {
    return this.h.request("DELETE", `/api/auth/tokens/${tokenId}`);
  }
}

class Auth {
  tokens: AuthTokens;

  constructor(h: Http) {
    this.tokens = new AuthTokens(h);
  }
}

export interface ClientOptions {
  token: string;
  baseUrl?: string;
  timeoutMs?: number;
}

/** Namespaced management client. Auth: personal access token (fnp_*). */
export class Client {
  private http: Http;
  private baseUrl: string;
  private timeoutMs: number;

  functions: Functions;
  versions: Versions;
  eval: Eval;
  datasets: Datasets;
  deployments: Deployments;
  keys: Keys;
  traces: Traces;
  auth: Auth;

  constructor(opts: ClientOptions) {
    this.baseUrl = opts.baseUrl ?? "http://localhost:8000";
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.http = new Http(this.baseUrl, opts.token, this.timeoutMs);
    this.functions = new Functions(this.http);
    this.versions = new Versions(this.http);
    this.eval = new Eval(this.http);
    this.datasets = new Datasets(this.http);
    this.deployments = new Deployments(this.http);
    this.keys = new Keys(this.http);
    this.traces = new Traces(this.http);
    this.auth = new Auth(this.http);
  }

  me(): Promise<S["MeOut"]> {
    return this.http.request("GET", "/api/me");
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
