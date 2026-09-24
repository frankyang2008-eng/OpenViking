import type { OVConfig } from "./config.js";
import type { OvHttpRequestOptions } from "./shared/ov-http.mjs";
import { createOvHttp } from "./shared/ov-http.mjs";

// --- OV API Response Shapes ---
// All OV responses wrap in: { status: "ok"|"error", result: T, error?: {...}, ... }
// This client normalizes to { ok, result } internally.

export interface OVSearchResult {
  uri: string;
  context_type: string;   // "memory" | "resource" | "skill"
  score: number;
  abstract: string;
  overview: string | null;
  level: number;          // 0=L0, 1=L1, 2=L2
  category: string;
  match_reason: string;
}

export interface OVDirEntry {
  uri: string;
  name: string;
  isDir: boolean;
  size: number;
  mode: number;
  modTime: string;
  abstract: string;
}

export interface OVStatInfo {
  name: string;
  size: number;
  mode: number;
  modTime: string;
  isDir: boolean;
  isLocked: boolean;
  uri?: string;
  count?: number;         // directories only
}

export interface OVSessionMeta {
  session_id: string;
  message_count: number;
  total_message_count?: number;
  commit_count: number;
  pending_tokens?: number;
  memories_extracted?: Record<string, number>;
  last_commit_at?: string;
}

export interface OVSessionContext {
  latest_archive_overview: string | null;
  pre_archive_abstracts: any[];
  messages: any[];
  estimatedTokens: number;
  stats: {
    totalArchives: number;
    includedArchives: number;
    droppedArchives: number;
    failedArchives: number;
    activeTokens: number;
    archiveTokens: number;
  };
}

export interface OVCommitResult {
  task_id?: string;
  archive_uri?: string;
  trace_id?: string;
}

export interface OVCommitResponse {
  result: OVCommitResult | null;
  traceId?: string;
  error?: any;
  status?: number;
}

export interface OVResponse<T> {
  ok: boolean;
  result: T | null;
  error?: any;
  status?: number;
  traceId?: string;
}

export class OVClient {
  private http: ReturnType<typeof createOvHttp>;
  connected: boolean = false;

  /** Read-only access to config (for value access across modules). */
  readonly cfg: OVConfig;

  constructor(config: OVConfig) {
    this.cfg = config;
    this.http = createOvHttp(
      { ...config, baseUrl: config.endpoint.replace(/\/+$/, "") },
      { defaultTimeoutMs: 10000, resolveActorPeerId: () => config.peerId },
    );
  }

  /** Core fetch wrapper. Returns { ok, result } after parsing OV's { status, result } envelope. */
  async fetchJSON<T>(
    path: string,
    init?: RequestInit,
    options?: OvHttpRequestOptions,
  ): Promise<OVResponse<T>> {
    const res = await this.http(path, init, options);
    // Network-level failure (DNS / refused / abort) comes back as status 0: the
    // server is unreachable. Flip connected off so gating tools stop taking the
    // live path; health() flips it back on once the server recovers. HTTP
    // 4xx/5xx carry a real status, meaning the server IS up, so they don't touch
    // connected.
    if (!res.ok && res.status === 0) this.connected = false;
    return res;
  }

  // ========== Health ==========

  async health(): Promise<boolean> {
    const res = await this.fetchJSON<any>("/health", undefined, { timeoutMs: 5000 });
    this.connected = res.ok;
    return res.ok;
  }

  // ========== Sessions ==========

  /** POST /api/v1/sessions — create or reuse session */
  async createSession(sessionId: string): Promise<boolean> {
    const res = await this.fetchJSON<any>("/api/v1/sessions", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId }),
    });
    return res.ok;
  }

  /** GET /api/v1/sessions/{id} — session metadata */
  async getSession(sessionId: string, autoCreate = false): Promise<OVSessionMeta | null> {
    const q = autoCreate ? "?auto_create=true" : "";
    const res = await this.fetchJSON<OVSessionMeta>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}${q}`,
      undefined, { timeoutMs: 5000 },
    );
    return res.ok ? res.result : null;
  }

  /** GET /api/v1/sessions/{id}/context — assembled context with archive overview */
  async getSessionContext(sessionId: string, tokenBudget = 128000): Promise<OVSessionContext | null> {
    const res = await this.fetchJSON<OVSessionContext>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/context?token_budget=${tokenBudget}`,
      undefined, { timeoutMs: 10000 },
    );
    return res.ok ? res.result : null;
  }

  /** POST /api/v1/sessions/{id}/messages — add a message with parts */
  async addMessageParts(sessionId: string, role: string, parts: any[]): Promise<boolean> {
    const res = await this.fetchJSON<any>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
      { method: "POST", body: JSON.stringify({ role, parts }) },
      { timeoutMs: 10000 },
    );
    return res.ok;
  }

  /** POST /api/v1/sessions/{id}/commit — commit session for archiving + extraction */
  async commitSessionResponse(
    sessionId: string,
    keepRecentCount = this.cfg.commitKeepRecentCount,
  ): Promise<OVCommitResponse> {
    const res = await this.fetchJSON<OVCommitResult>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/commit`,
      { method: "POST", body: JSON.stringify({ keep_recent_count: keepRecentCount }) },
      { timeoutMs: 30000 },
    );
    if (res.ok && res.result && !res.result.trace_id && res.traceId) {
      res.result.trace_id = res.traceId;
    }
    return {
      result: res.ok ? res.result : null,
      traceId: res.traceId,
      error: res.error,
      status: res.status,
    };
  }

  async commitSession(
    sessionId: string,
    keepRecentCount = this.cfg.commitKeepRecentCount,
  ): Promise<OVCommitResult | null> {
    return (await this.commitSessionResponse(sessionId, keepRecentCount)).result;
  }

  /** DELETE /api/v1/sessions/{id} */
  async deleteSession(sessionId: string): Promise<boolean> {
    const res = await this.fetchJSON<any>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
      { timeoutMs: 10000 },
    );
    return res.ok;
  }
}

