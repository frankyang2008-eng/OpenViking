#!/usr/bin/env node
// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0

import { readFile } from "node:fs/promises";
import {
  addAgentMessages,
  commitAgentSession,
  createAgentLogger,
  deriveAgentSessionId,
  makeAgentFetchJSON,
  readHookInput,
  readHookState,
  resolveAgentCwd,
  resolveNativeSessionId,
  shouldBypassAgent,
  stableHash,
  withAgentHookLock,
  writeHookState,
} from "../../memory-plugin-shared/lib/agent-hook-runtime.mjs";
import { maybeDetach } from "../../memory-plugin-shared/lib/async-writer.mjs";
import { sanitizeCapturedText } from "../../memory-plugin-shared/lib/capture-utils.mjs";
import { parseAgyTranscript } from "./agy-transcript.mjs";
import { CLIENT_ID, SESSION_PREFIX, loadAgyConfig } from "./config.mjs";

const cfg = loadAgyConfig();
const { log, logError } = createAgentLogger(CLIENT_ID, "Stop", cfg);

function output(result = {}) {
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

async function main() {
  // If async write path is enabled, detach immediate background worker
  // and respond to AGY IDE instantly to avoid blocking the agent loop.
  if (await maybeDetach(cfg, { approve: () => output({}) })) {
    return;
  }

  const input = await readHookInput();
  if (!cfg.enabled || shouldBypassAgent(cfg, input)) {
    output({});
    return;
  }

  const nativeSessionId = resolveNativeSessionId(input);
  const sessionId = deriveAgentSessionId(SESSION_PREFIX, input);
  const cwd = resolveAgentCwd(input);
  const { fetchJSON } = makeAgentFetchJSON(cfg, cwd);

  await withAgentHookLock(CLIENT_ID, nativeSessionId, async () => {
    let state = await readHookState(CLIENT_ID, nativeSessionId);
    const transcriptPath = input.transcriptPath || input.transcript_path;

    let captured = 0;
    if (cfg.autoCapture && transcriptPath) {
      try {
        const rawTranscript = await readFile(transcriptPath, "utf8");
        const turns = parseAgyTranscript(rawTranscript);

        const capturedHashes = new Set(Array.isArray(state.capturedHashes) ? state.capturedHashes : []);
        const toSend = [];

        for (const [index, turn] of turns.entries()) {
          const sanitized = sanitizeCapturedText(turn.content);
          if (!sanitized) continue;

          // Stable hash includes step index / turn index to prevent re-capturing
          const stepKey = turn.stepIndex != null ? turn.stepIndex : index;
          const hash = stableHash(stepKey, turn.role, sanitized);
          if (capturedHashes.has(hash)) continue;

          toSend.push({
            hash,
            turn: {
              role: turn.role,
              content: sanitized,
            },
          });
        }

        if (toSend.length > 0) {
          const result = await addAgentMessages(
            fetchJSON,
            sessionId,
            toSend.map((item) => item.turn)
          );
          captured = result.sent + result.queued;
          for (const item of toSend.slice(0, captured)) {
            capturedHashes.add(item.hash);
          }

          state = {
            ...state,
            capturedHashes: [...capturedHashes].slice(-1000),
            capturedSinceCommit: Number(state.capturedSinceCommit || 0) + captured,
          };
        }
      } catch (err) {
        logError("captureTranscript", err);
      }
    }

    // Determine if commit should be triggered
    const terminationReason = input.terminationReason || input.termination_reason || "";
    const isStopTerminal = ["user_cancelled", "session_end", "max_steps_exceeded"].includes(terminationReason);
    const commitThresholdReached = (state.capturedSinceCommit || 0) >= cfg.commitTurnThreshold;

    if (commitThresholdReached || isStopTerminal) {
      log("triggeringCommit", { sessionId, capturedSinceCommit: state.capturedSinceCommit, terminationReason });
      const result = await commitAgentSession(fetchJSON, sessionId, log).catch((err) => {
        logError("commitSession", err);
        return { ok: false };
      });
      if (result?.ok) {
        state.capturedSinceCommit = 0;
        state.lastCommittedAt = Date.now();
      }
    }

    await writeHookState(CLIENT_ID, nativeSessionId, state);
  });

  output({});
}

main().catch((err) => {
  logError("uncaughtStop", err);
  output({});
});
