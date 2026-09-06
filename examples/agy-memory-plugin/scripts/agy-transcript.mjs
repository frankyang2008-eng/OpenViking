// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0

/**
 * Antigravity (AGY) Transcript Parser.
 *
 * Extracts user queries and turns from AGY's transcript.jsonl format.
 */

const USER_REQUEST_RE = /<USER_REQUEST>([\s\S]*?)<\/USER_REQUEST>/i;
const METADATA_RE = /<(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|system_context)[\s\S]*?<\/(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|system_context)>/gi;

export function extractAgyUserText(rawContent) {
  if (typeof rawContent !== "string") return "";
  const match = rawContent.match(USER_REQUEST_RE);
  if (match && match[1]) {
    return match[1].trim();
  }
  // If no <USER_REQUEST> wrapper, strip out known metadata blocks and return the remainder
  return rawContent.replace(METADATA_RE, "").trim();
}

export function extractAgyAssistantText(step) {
  if (!step) return "";
  if (typeof step.content === "string" && step.content.trim()) {
    return step.content.trim();
  }
  // If content is empty or null, check tool calls summary or response text
  if (Array.isArray(step.tool_calls) && step.tool_calls.length > 0) {
    const tools = step.tool_calls.map((t) => t?.name || "tool").join(", ");
    return `[Executed tools: ${tools}]`;
  }
  return "";
}

export function parseAgyTranscript(raw) {
  const turns = [];
  if (!raw) return turns;

  const lines = typeof raw === "string" ? raw.split("\n") : [];
  for (const line of lines) {
    if (!line.trim()) continue;
    let step;
    try {
      step = JSON.parse(line);
    } catch {
      continue;
    }

    if (!step || typeof step !== "object") continue;

    if (step.type === "USER_INPUT" && (step.source === "USER_EXPLICIT" || step.source === "USER")) {
      const text = extractAgyUserText(step.content);
      if (text) {
        turns.push({ role: "user", content: text, stepIndex: step.step_index });
      }
    } else if (step.source === "MODEL" && step.type === "PLANNER_RESPONSE") {
      const text = extractAgyAssistantText(step);
      if (text) {
        turns.push({ role: "assistant", content: text, stepIndex: step.step_index });
      }
    }
  }

  return turns;
}

export function getLatestAgyUserPrompt(raw) {
  if (!raw) return "";
  const lines = typeof raw === "string" ? raw.split("\n") : [];
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (!line) continue;
    let step;
    try {
      step = JSON.parse(line);
    } catch {
      continue;
    }
    if (step?.type === "USER_INPUT" && (step.source === "USER_EXPLICIT" || step.source === "USER")) {
      return extractAgyUserText(step.content);
    }
  }
  return "";
}
