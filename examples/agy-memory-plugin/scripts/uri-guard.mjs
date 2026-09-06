#!/usr/bin/env node
// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0

import { readFileSync, realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { evaluateAgentUriGuard } from "../../memory-plugin-shared/lib/agent-uri-guard.mjs";

function readInput() {
  try {
    const raw = readFileSync(0, "utf8").trim();
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function mapAgyToolName(rawName) {
  const name = String(rawName || "").toLowerCase().replace(/[_-]/g, "");
  if (name === "viewfile" || name === "replacefilecontent" || name === "multireplacefilecontent") {
    return "read";
  }
  if (name === "runcommand") {
    return "runcommand";
  }
  if (name === "grepsearch") {
    return "grep";
  }
  return name;
}

export function evaluateAgyUriGuard(payload = {}) {
  const toolCall = payload.toolCall || payload.tool_call || {};
  const rawToolName = toolCall.name || "";
  const mappedToolName = mapAgyToolName(rawToolName);
  const args = toolCall.args || {};

  // Check if args contains a viking:// URI
  const decision = evaluateAgentUriGuard(mappedToolName, args);
  if (decision) {
    return {
      decision: "deny",
      reason: decision.reason || "Viking URIs (viking://) cannot be read via local file tools. Use OpenViking MCP tools instead.",
    };
  }

  return {
    decision: "allow",
  };
}

const isEntrypoint =
  process.argv[1] &&
  realpathSync(fileURLToPath(import.meta.url)) === realpathSync(process.argv[1]);

if (isEntrypoint) {
  const input = readInput();
  const output = evaluateAgyUriGuard(input);
  process.stdout.write(`${JSON.stringify(output)}\n`);
}
