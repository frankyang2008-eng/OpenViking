#!/usr/bin/env node
// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0

import { readFile } from "node:fs/promises";
import {
  buildAgentProfile,
  createAgentLogger,
  deriveAgentSessionId,
  makeAgentFetchJSON,
  readHookInput,
  readHookState,
  recallForPrompt,
  replayAgentPending,
  resolveAgentCwd,
  resolveNativeSessionId,
  shouldBypassAgent,
  stableHash,
  withAgentHookLock,
  writeHookState,
} from "../../memory-plugin-shared/lib/agent-hook-runtime.mjs";
import { getLatestAgyUserPrompt } from "./agy-transcript.mjs";
import { CLIENT_ID, SESSION_PREFIX, loadAgyConfig } from "./config.mjs";

const cfg = loadAgyConfig();
const { log, logError } = createAgentLogger(CLIENT_ID, "PreInvocation", cfg);

function output(result = { injectSteps: [] }) {
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

const TRIVIAL_QUERY_RE = /^(ok|yes|no|thanks|thank you|done|proceed|continue|好|好的|收到|收到，继续|继续|行|对|是的|1)$/i;

async function executeWithTimeout(promise, ms = 1200) {
  let timer;
  const timeoutPromise = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`Timeout after ${ms}ms`)), ms);
  });
  try {
    return await Promise.race([promise, timeoutPromise]);
  } finally {
    clearTimeout(timer);
  }
}

async function main() {
  const input = await readHookInput();
  if (!cfg.enabled || shouldBypassAgent(cfg, input)) {
    output({ injectSteps: [] });
    return;
  }

  const nativeSessionId = resolveNativeSessionId(input);
  const sessionId = deriveAgentSessionId(SESSION_PREFIX, input);
  const cwd = resolveAgentCwd(input);
  const { fetchJSON } = makeAgentFetchJSON(cfg, cwd);

  const injectSteps = [];

  await executeWithTimeout(
    withAgentHookLock(CLIENT_ID, nativeSessionId, async () => {
      let state = await readHookState(CLIENT_ID, nativeSessionId);

      const invocationNum = Number(input.invocationNum ?? input.invocation_num ?? 1);
      const isFirstInvocation = invocationNum === 1 || !state.lastSessionStartAt;

      // 1. Session Start / Profile Injection (First turn)
      let profileText = "";
      if (isFirstInvocation) {
        state = { ...state, lastSessionStartAt: Date.now() };
        await replayAgentPending(fetchJSON, log).catch((err) => logError("replayPending", err));
        const profile = await buildAgentProfile(fetchJSON, cfg, cwd).catch((err) => {
          logError("profile", err);
          return null;
        });
        if (profile) {
          profileText = `<openviking-context source="profile">\n${profile}\n</openviking-context>`;
        }
      }

      // 2. Extract latest user query from transcript
      const transcriptPath = input.transcriptPath || input.transcript_path;
      let prompt = "";
      if (transcriptPath) {
        try {
          const rawTranscript = await readFile(transcriptPath, "utf8");
          prompt = getLatestAgyUserPrompt(rawTranscript);
        } catch (err) {
          logError("readTranscript", err);
        }
      }

      // Fallback: check prompt in input payload
      if (!prompt && typeof input.prompt === "string") {
        prompt = input.prompt.trim();
      }

      // 3. Recall evaluation
      let recallText = "";
      if (cfg.autoRecall && prompt && prompt.length >= 3 && !TRIVIAL_QUERY_RE.test(prompt)) {
        const promptHash = stableHash(prompt);
        if (state.promptHash !== promptHash || !state.recallBlock) {
          const recallBlock = await recallForPrompt(fetchJSON, cfg, prompt, cwd, log, { sessionId }).catch((err) => {
            logError("recall", err);
            return null;
          });
          state = { ...state, promptHash, recallBlock };
        }
        if (state.recallBlock) {
          recallText = state.recallBlock;
        }
      }

      await writeHookState(CLIENT_ID, nativeSessionId, state);

      // 4. Assemble Ephemeral Message
      const blocks = [profileText, recallText].filter(Boolean);
      if (blocks.length > 0) {
        injectSteps.push({
          ephemeralMessage: blocks.join("\n\n"),
        });
      }
    }),
    1200
  ).catch((err) => {
    logError("PreInvocationTimeoutOrError", err);
  });

  output({ injectSteps });
}

main().catch((err) => {
  logError("uncaughtPreInvocation", err);
  output({ injectSteps: [] });
});
