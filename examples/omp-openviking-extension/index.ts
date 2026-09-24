/**
 * omp OpenViking Extension
 *
 * OpenViking extension maintained for omp (oh-my-pi, pi fork). Kept separate
 * from examples/pi-coding-agent-extension so omp compatibility patches do not
 * fight upstream pi-targeted fixes: omp's SessionManager predates pi's
 * buildContextEntries() and types before_agent_start systemPrompt as string[].
 *
 * Syncs conversation turns to OV, recalls relevant memories on each prompt,
 * and commits sessions for long-term memory extraction.
 *
 * Design informed by: OpenClaw (synchronous recall), Claude Code plugin
 * (most mature, production-hardened), Hermes (anti-pattern: stale prefetch).
 */
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { loadConfigFromModuleUrl, type OVConfig } from "./config.js";
import { OVClient } from "./client.js";
import { RecallManager } from "./recall.js";
import { RecallLedger } from "./shared/recall-ledger.mjs";
import { SyncManager } from "./sync.js";
import { buildProfileBlock } from "./shared/profile-inject.mjs";
import { createLogger } from "./shared/debug-log.mjs";
import { isBypassed } from "./shared/session-model.mjs";
import { collectInertKnobs } from "./lib/omp-config.mjs";
import { agentDirForModuleUrl, readMcpServerState } from "./lib/mcp-server-state.mjs";
import { guardVikingUriToolCall, noticeVikingUriToolResult } from "./lib/uri-guard-adapter.mjs";
import { createTakeoverManager } from "./takeover.js";

export default async function (pi: ExtensionAPI) {
  // --- Load config ---
  const config = loadConfigFromModuleUrl(import.meta.url);
  if (!config.enabled) return;

  // Env overrides

  // --- Initialize modules ---
  const client = new OVClient(config);
  const sync = new SyncManager(client, config);
  const recall = new RecallManager(
    client,
    config,
    () => sync.sessionId,
    // The ledger keeps request prefixes byte-stable for provider prompt
    // caches (#4137); it is per pi session and opened once the id is known.
    config.recallLedger ? new RecallLedger() : null,
  );
  const logger = createLogger("omp", {
    debug: Boolean(config.debugLogPath),
    debugLogPath: config.debugLogPath,
  });
  const takeover = createTakeoverManager({
    pi,
    client,
    sync,
    config,
    log: (message: string) => logger.log("takeover", message),
  });

  // Session state
  let connected = false;
  let bypassed = false;
  let profileBlock = "";
  let archiveOverview = "";
  let mcpToolsHint = "";
  let compacted = false;
  let started = false;
  let inertKnobsNotified = false;
  let startPromise: Promise<void> | null = null;

  // ================================================================
  // Event Handlers
  // ================================================================

  const start = async (ctx: any): Promise<void> => {
    if (started) return;
    if (startPromise) return startPromise;

    startPromise = (async () => {
      // A knob the shared schema declares but nothing here reads looks exactly
      // like one that worked: an operator setting `recallPreferAbstract` on a
      // harness whose plugin reads it gets a warning here instead of silence.
      if (!inertKnobsNotified) {
        inertKnobsNotified = true;
        const inert = collectInertKnobs(config);
        if (inert.length && config.logLevel !== "silent") {
          ctx.ui.notify(`OpenViking: settings with no omp consumer: ${inert.join(", ")}`, "warning");
        }
      }

      // Bypass check. `bypassSession` is the switch and `bypassSessionPatterns`
      // the globs; the shared matcher owns both, so this extension no longer
      // reads its own list first. Patterns are globs (`path/**`), not bare
      // directory prefixes.
      if (isBypassed(config, { cwd: process.cwd() })) {
        bypassed = true;
        started = true;
        return;
      }

      // Health check
      connected = await client.health();
      if (!connected) {
        // Not gated on logLevel: with the tool catalogue living behind the MCP
        // proxy, a down server means all 16 tools are missing, and the design
        // (§4.4) requires that to be said out loud rather than silently absent.
        ctx.ui.notify(
          "OpenViking: server not reachable — the mcp__openviking_* tools will fail until it is back",
          "warning",
        );
        return;
      }

      // Ensure OV session
      const piSessionId = ctx.sessionManager.getSessionId();
      recall.openLedger(piSessionId);
      const ok = await sync.ensureSession(piSessionId);
      if (!ok) {
        if (config.logLevel !== "silent") {
          ctx.ui.notify("OpenViking: failed to create session", "error");
        }
        return;
      }
      await sync.replayPending();

      // Profile injection
      profileBlock = await buildSessionProfileBlock(client, config);

      const branch = typeof ctx.sessionManager.getBranch === "function"
        ? ctx.sessionManager.getBranch()
        : [];
      if (config.takeoverEnabled) {
        takeover.restore(branch);
        sync.restoreWatermark(takeover.state.syncedEntryCount);
      } else if (sync.sessionId) {
        // Resume rehydration — fetch archive overview if session was previously committed.
        archiveOverview = await fetchArchiveOverview(client, sync.sessionId, config);
      }

      // The server's own catalogue reaches the model through omp's MCP client,
      // so there is nothing to register here — but nothing in omp's prompt lists
      // MCP tools (`tools.xdevDocs` defaults to "builtins", which keeps them
      // on-demand), so a session that never runs `read xd://` never learns they
      // exist. One line says where they are. It is only true when the entry is
      // actually in mcp.json, when the user has not disabled the server, and
      // when the extension's own `mcpEnabled` switch is on.
      const mcp = readMcpServerState(agentDirForModuleUrl(import.meta.url));
      mcpToolsHint = config.mcpEnabled !== false && mcp.enabled
        ? "OpenViking tools are available as MCP tools named `mcp__openviking_*` (find, search, read, list, tree, remember, write, edit, add_resource, add_skill, list_watches, cancel_watch, grep, glob, forget, health). They load on demand — `read xd://` lists them. Use them for `viking://` URIs instead of local file tools."
        : "";
      updateStatus(ctx, connected, 0, sync.sessionId, config, takeover.state);

      started = true;
      if (config.logLevel === "info") {
        ctx.ui.notify(`OpenViking connected (${piSessionId.slice(0, 8)}...)`, "info");
      }
    })().finally(() => {
      startPromise = null;
    });

    return startPromise;
  };

  // --- session_start ---
  pi.on("session_start", async (_event, ctx) => {
    // Fire-and-forget (ported from upstream #4506): the OV chain (health
    // check, session ensure, profile build) costs ~2s against a remote
    // server; blocking session_start on it delays every omp startup.
    // start() is memoized via startPromise, so before_agent_start awaits
    // the same in-flight chain before the first provider request — the
    // first turn still gets profile + recall.
    void start(ctx).catch((error) => {
      logger.logError("session_start", error);
    });
  });

  // --- before_agent_start ---
  pi.on("before_agent_start", async (event, ctx) => {
    // session_start doesn't fire for pi -c continuations.
    await start(ctx);

    if (!connected || bypassed) return;

    // Queue recall for the context hook. Pi renders the user message before
    // that hook, so recall latency does not delay the message appearing.
    recall.queueSearch(event.prompt);

    // Compose system prompt additions
    const parts: string[] = [];
    if (profileBlock) parts.push(profileBlock);
    if (!config.takeoverEnabled && archiveOverview && (compacted || archiveOverview.trim())) {
      parts.push(archiveOverview);
    }
    if (mcpToolsHint) parts.push(mcpToolsHint);

    const additions = parts.join("\n\n");
    if (!additions) return;

    // omp types `systemPrompt` as string[] (pi: string). Join arrays before
    // concatenating — `array + string` would comma-flatten the base prompt.
    const basePrompt = Array.isArray(event.systemPrompt)
      ? event.systemPrompt.join("\n\n")
      : event.systemPrompt;
    return {
      systemPrompt: basePrompt + "\n\n" + additions,
    };
  });

  // --- context ---
  pi.on("context", async (event, ctx) => {
    if (!connected || bypassed) return;

    // Keep recall synchronous with the provider request so the current prompt
    // still receives current-query memory, without blocking user-message UI.
    await recall.searchPending();

    // The context hook omits persisted entry ids, but its user messages are a
    // deep copy of the active SessionManager context. Associate those objects
    // with stable ids before takeover may filter the array; retained messages
    // keep object identity through that transform.
    const sm = ctx.sessionManager as {
      buildContextEntries?: () => Array<{ type: string; id: string; message?: { role: string } }>;
      getBranch?: () => Array<{ type: string; id: string; message?: { role: string } }>;
    };
    // pi exposes buildContextEntries; omp (fork) predates it — getBranch is the
    // compatible fallback (ledger keys carry a content hash, so a stale id only
    // costs a cache miss, never context pollution).
    const contextEntries =
      typeof sm.buildContextEntries === "function"
        ? sm.buildContextEntries()
        : typeof sm.getBranch === "function"
          ? sm.getBranch()
          : [];
    const userEntryIds = contextEntries
      .filter(entry => entry?.type === "message" && entry.message?.role === "user")
      .map(entry => entry.id);
    const messageIds = new WeakMap<object, string>();
    let userIndex = 0;
    for (const message of event.messages) {
      if (message?.role !== "user") continue;
      const entryId = userEntryIds[userIndex++];
      if (entryId && typeof message === "object") {
        messageIds.set(message, entryId);
      }
    }

    const afterTakeover = config.takeoverEnabled
      ? takeover.transformContext(event.messages)
      : event.messages;
    const messages = recall.injectRecall(
      afterTakeover,
      (message) => messageIds.get(message) ?? null,
    );
    return { messages };
  });

  // --- tool_call ---
  pi.on("tool_call", async (event, _ctx) => {
    const decision = guardVikingUriToolCall(event);
    if (!decision) return;
    return decision;
  });

  // --- tool_result ---
  pi.on("tool_result", async (event) => {
    const notice = noticeVikingUriToolResult(event);
    if (!notice) return;
    return notice;
  });

  // --- turn_end ---
  pi.on("turn_end", async (_event, ctx) => {
    if (!connected || bypassed || !config.syncTurns) return;

    const branch = ctx.sessionManager.getBranch();
    const result = await sync.syncBranch(branch);
    logger.log("turn_end", { added: result.added, tokens: result.tokens });
    await takeover.onTurnSynced(result.tokens);
    updateStatus(ctx, connected, result.added, sync.sessionId, config, takeover.state);
  });

  // --- session_before_compact ---
  pi.on("session_before_compact", async (event, _ctx) => {
    if (!connected || bypassed) return;

    if (config.takeoverEnabled) {
      const prep = (event as any)?.preparation ?? {};
      return await takeover.handleBeforeCompact({
        firstKeptEntryId: prep.firstKeptEntryId,
        tokensBefore: prep.tokensBefore ?? 0,
      });
    }

    const archiveId = await sync.commit();
    compacted = true;

    // Cache archive overview for rehydration after compaction
    if (archiveId && sync.sessionId) {
      archiveOverview = await fetchArchiveOverview(
        client, sync.sessionId, config,
      );
    }
    // Return nothing → pi proceeds with default compaction
  });

  // --- session_shutdown ---
  pi.on("session_shutdown", async (_event, _ctx) => {
    if (!connected || bypassed) return;

    await sync.shutdown();
    if (config.takeoverEnabled) {
      await takeover.shutdown();
    } else {
      await sync.commit();
    }
  });

  // --- agent_end ---
  pi.on("agent_end", async (_event, _ctx) => {
    recall.invalidate();
  });

  // ================================================================
  // Commands
  // ================================================================

  pi.registerCommand("viking", {
    description: "OpenViking status and manual operations. Use 'commit' to force a sync.",
    handler: async (args, ctx) => {
      if (!connected) {
        ctx.ui.notify("OpenViking: not connected", "warning");
        return;
      }

      if (args?.trim() === "commit") {
        await sync.shutdown();
        const commitResult = config.takeoverEnabled ? null : await sync.commit();
        const ok = config.takeoverEnabled
          ? await takeover.commitAndAdvance()
          : commitResult !== null;
        if (ok) {
          ctx.ui.notify(
            "OpenViking: committed successfully" +
              (commitResult?.trace_id ? ` (trace_id=${commitResult.trace_id})` : ""),
            "info",
          );
        } else {
          ctx.ui.notify("OpenViking: commit failed", "error");
        }
        return;
      }

      // Status
      const sid = sync.sessionId ?? "none";
      const t = takeover.state;
      const takeoverInfo = config.takeoverEnabled
        ? ` | takeover: ${t.coveredUserTurns}/${t.lastSeenUserTurns} turns archived, ~${t.pendingTokens} tokens pending`
        : "";
      ctx.ui.notify(
        `OpenViking: ${connected ? "connected" : "disconnected"} | session: ${sid.slice(0, 12)}...${takeoverInfo}`,
        "info",
      );
    },
  });
}

// ================================================================
// Helper Functions
// ================================================================

/** Build the <openviking-context> profile block. */
async function buildSessionProfileBlock(
  client: OVClient, config: OVConfig,
): Promise<string> {
  try {
    const profile = await buildProfileBlock(
      (path: string, init?: any, _options?: any) => client.fetchJSON(path, init, 10000),
      config.profileTokenBudget,
      config.peerId,
      config,
    );
    if (!profile?.block) return "";
    return [
      '<openviking-context source="session-start">',
      profile.block,
      "</openviking-context>",
    ].join("\n");
  } catch {
    return "";
  }
}

/** Fetch archive overview for rehydration using the session context API. */
async function fetchArchiveOverview(
  client: OVClient, sessionId: string, config: OVConfig,
): Promise<string> {
  try {
    const ctx = await client.getSessionContext(sessionId, config.resumeContextBudget);
    if (!ctx || !ctx.latest_archive_overview) return "";

    return [
      '<openviking-context source="session-archive">',
      "<session-archive>",
      ctx.latest_archive_overview,
      "</session-archive>",
      "</openviking-context>",
    ].join("\n");
  } catch {
    return "";
  }
}

function updateStatus(
  ctx: any,
  connected: boolean,
  added: number,
  sessionId: string | null,
  config: OVConfig,
  takeoverState?: { pendingTokens?: number; coveredUserTurns?: number },
): void {
  const setter = ctx?.ui?.setStatus;
  if (typeof setter !== "function") return;
  const threshold = config.takeoverEnabled
    ? config.takeoverTokenThreshold
    : config.commitTokenThreshold;
  const pending = config.takeoverEnabled && takeoverState
    ? ` · ctx ${takeoverState.coveredUserTurns ?? 0} · ~${takeoverState.pendingTokens ?? 0}/${threshold}`
    : ` · ✎ ${threshold}`;
  const status = `${connected ? "OV ✓" : "OV ✗"} · ↩${added}${pending} · ${sessionId ? sessionId.slice(0, 12) : "none"}`;
  try {
    setter("openviking", status);
  } catch {
    // Best effort; pi API shape may vary across fast-moving versions.
  }
}
