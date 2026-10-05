#!/usr/bin/env node
// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0

import { readFileSync, realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { evaluateUriGuard, evaluateUriNotice } from "../../memory-plugin-shared/lib/uri-guard.mjs";

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
  if (name === "viewfile") {
    return "read";
  }
  if (name === "replacefilecontent" || name === "multireplacefilecontent") {
    return "edit";
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
  const decision = evaluateUriGuard(mappedToolName, args);
  if (decision) {
    return {
      decision: "deny",
      reason: decision.reason || "Viking URIs (viking://) cannot be read via local file tools. Use OpenViking MCP tools instead.",
    };
  }

  // Shell tools are never denied: a viking:// URI in a command line is as often
  // intentional data (an `ov` argument, an HTTP payload) as a mistaken path.
  // The command still runs; the notice tells the model which MCP tool to use.
  const notice = evaluateUriNotice(mappedToolName, args);
  if (notice) {
    return {
      decision: "allow",
      reason: notice.reason,
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
