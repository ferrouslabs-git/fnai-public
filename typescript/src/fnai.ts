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
 *     await c.versions.submit(v.id); await c.versions.approve(v.id);
 *     await c.deployments.deploy(fn.id, v.id);
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

export const VERSION = "0.2.0";
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

  submit(vId: number): Promise<S["VersionOut"]> {
    return this.h.request("POST", `/api/versions/${vId}/submit`, {});
  }

  approve(vId: number, comment = ""): Promise<S["VersionOut"]> {
    return this.h.request("POST", `/api/versions/${vId}/approve`, { comment });
  }

  reject(vId: number, comment = ""): Promise<S["VersionOut"]> {
    return this.h.request("POST", `/api/versions/${vId}/reject`, { comment });
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
