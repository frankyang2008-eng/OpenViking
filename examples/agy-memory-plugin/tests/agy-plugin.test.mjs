// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0

import assert from "node:assert/strict";
import test from "node:test";
import {
  extractAgyAssistantText,
  extractAgyUserText,
  getLatestAgyUserPrompt,
  parseAgyTranscript,
} from "../scripts/agy-transcript.mjs";
import { evaluateAgyUriGuard } from "../scripts/uri-guard.mjs";
import { sanitizeCapturedText } from "../../memory-plugin-shared/lib/capture-utils.mjs";

test("extractAgyUserText extracts content enclosed in <USER_REQUEST>", () => {
  const raw = `<USER_REQUEST>\nFix authentication error in middleware\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\ntime: now\n</ADDITIONAL_METADATA>`;
  const result = extractAgyUserText(raw);
  assert.equal(result, "Fix authentication error in middleware");
});

test("extractAgyUserText strips metadata when no <USER_REQUEST> tags present", () => {
  const raw = `Please refactor this module.\n<ADDITIONAL_METADATA>\nBrowser open: false\n</ADDITIONAL_METADATA>`;
  const result = extractAgyUserText(raw);
  assert.equal(result, "Please refactor this module.");
});

test("extractAgyAssistantText retrieves assistant response or tool summary", () => {
  const stepWithContent = {
    step_index: 3,
    source: "MODEL",
    type: "PLANNER_RESPONSE",
    content: "I have analyzed the database schema.",
  };
  assert.equal(extractAgyAssistantText(stepWithContent), "I have analyzed the database schema.");

  const stepWithTools = {
    step_index: 4,
    source: "MODEL",
    type: "PLANNER_RESPONSE",
    content: "",
    tool_calls: [{ name: "run_command" }, { name: "view_file" }],
  };
  assert.equal(extractAgyAssistantText(stepWithTools), "[Executed tools: run_command, view_file]");
});

test("parseAgyTranscript parses multi-step transcript correctly", () => {
  const jsonl = [
    JSON.stringify({
      step_index: 0,
      source: "USER_EXPLICIT",
      type: "USER_INPUT",
      content: "<USER_REQUEST>\nHow does VikingFS work?\n</USER_REQUEST>",
    }),
    JSON.stringify({
      step_index: 1,
      source: "SYSTEM",
      type: "CONVERSATION_HISTORY",
      content: "Summary...",
    }),
    JSON.stringify({
      step_index: 2,
      source: "MODEL",
      type: "PLANNER_RESPONSE",
      content: "VikingFS is an AGFS-based filesystem abstraction.",
    }),
    JSON.stringify({
      step_index: 3,
      source: "USER_EXPLICIT",
      type: "USER_INPUT",
      content: "Show me the code.",
    }),
  ].join("\n");

  const turns = parseAgyTranscript(jsonl);
  assert.equal(turns.length, 3);
  assert.equal(turns[0].role, "user");
  assert.equal(turns[0].content, "How does VikingFS work?");
  assert.equal(turns[0].stepIndex, 0);

  assert.equal(turns[1].role, "assistant");
  assert.equal(turns[1].content, "VikingFS is an AGFS-based filesystem abstraction.");
  assert.equal(turns[1].stepIndex, 2);

  assert.equal(turns[2].role, "user");
  assert.equal(turns[2].content, "Show me the code.");
  assert.equal(turns[2].stepIndex, 3);
});

test("getLatestAgyUserPrompt returns the most recent user prompt", () => {
  const jsonl = [
    JSON.stringify({
      step_index: 0,
      source: "USER_EXPLICIT",
      type: "USER_INPUT",
      content: "Initial query",
    }),
    JSON.stringify({
      step_index: 1,
      source: "MODEL",
      type: "PLANNER_RESPONSE",
      content: "Initial answer",
    }),
    JSON.stringify({
      step_index: 2,
      source: "USER_EXPLICIT",
      type: "USER_INPUT",
      content: "<USER_REQUEST>\nLatest query details\n</USER_REQUEST>",
    }),
  ].join("\n");

  const latest = getLatestAgyUserPrompt(jsonl);
  assert.equal(latest, "Latest query details");
});

test("evaluateAgyUriGuard denies viking:// URIs in view_file and allows local paths", () => {
  const deniedCall = {
    toolCall: {
      name: "view_file",
      args: {
        AbsolutePath: "viking://user/memories/preferences.md",
      },
    },
  };
  const deniedResult = evaluateAgyUriGuard(deniedCall);
  assert.equal(deniedResult.decision, "deny");
  assert.match(deniedResult.reason, /viking:\/\//);

  const allowedCall = {
    toolCall: {
      name: "view_file",
      args: {
        AbsolutePath: "/Users/dev/project/src/index.ts",
      },
    },
  };
  const allowedResult = evaluateAgyUriGuard(allowedCall);
  assert.equal(allowedResult.decision, "allow");
});

test("evaluateAgyUriGuard lets run_command carrying a viking URI through with a notice", () => {
  const shellCall = {
    toolCall: {
      name: "run_command",
      args: {
        CommandLine: "cat viking://user/skills/build.md",
      },
    },
  };
  const result = evaluateAgyUriGuard(shellCall);
  assert.equal(result.decision, "allow");
  assert.match(result.reason, /URI guard/);
  assert.match(result.reason, /ignore this notice/);
});

test("evaluateAgyUriGuard denies viking:// in replace_file_content with the edit hint", () => {
  const writeCall = {
    toolCall: {
      name: "replace_file_content",
      args: {
        AbsolutePath: "viking://user/skills/build.md",
        OldString: "old",
        NewString: "new",
      },
    },
  };
  const result = evaluateAgyUriGuard(writeCall);
  assert.equal(result.decision, "deny");
  assert.match(result.reason, /OpenViking MCP edit/);
});

test("evaluateAgyUriGuard denies viking:// in multi_replace_file_content with the edit hint", () => {
  const editCall = {
    toolCall: {
      name: "multi_replace_file_content",
      args: {
        TargetFile: "viking://user/memories/preferences.md",
        ReplacementChunks: [],
      },
    },
  };
  const result = evaluateAgyUriGuard(editCall);
  assert.equal(result.decision, "deny");
  assert.match(result.reason, /OpenViking MCP edit/);
});

test("evaluateAgyUriGuard denies viking:// in write_to_file with the write or add_skill hint", () => {
  const writeCall = {
    toolCall: {
      name: "write_to_file",
      args: {
        TargetFile: "viking://user/skills/my-skill/SKILL.md",
        CodeContent: "# My Skill",
      },
    },
  };
  const result = evaluateAgyUriGuard(writeCall);
  assert.equal(result.decision, "deny");
  assert.match(result.reason, /OpenViking MCP (?:write|add_skill)/);
});

test("evaluateAgyUriGuard denies viking:// in grep_search with the grep hint", () => {
  const grepCall = {
    toolCall: {
      name: "grep_search",
      args: {
        SearchPath: "viking://user/memories",
        Query: "something",
      },
    },
  };
  const result = evaluateAgyUriGuard(grepCall);
  assert.equal(result.decision, "deny");
  assert.match(result.reason, /OpenViking MCP (?:grep|search)/);
});

test("sanitizeCapturedText completely strips <openviking-context> blocks", () => {
  const dirtyText = `Here is my answer.\n\n<openviking-context source="recall">\n* [viking://user/skills/git](viking://user/skills/git): Git workflow\n</openviking-context>\n\nProceeding with task.`;
  const clean = sanitizeCapturedText(dirtyText);
  assert.doesNotMatch(clean, /<openviking-context/);
  assert.doesNotMatch(clean, /viking:\/\/user\/skills\/git/);
  assert.match(clean, /Here is my answer\./);
  assert.match(clean, /Proceeding with task\./);
});

test("pre-invocation.mjs responds with valid AGY injectSteps format even when server offline", async () => {
  const { execFileSync } = await import("node:child_process");
  const scriptPath = new URL("../scripts/pre-invocation.mjs", import.meta.url).pathname;

  const inputPayload = JSON.stringify({
    conversationId: "test-conv-12345",
    workspacePaths: ["/tmp"],
    invocationNum: 1,
    prompt: "How to run the tests?",
  });

  const stdout = execFileSync(process.execPath, [scriptPath], {
    input: inputPayload,
    encoding: "utf8",
    env: {
      ...process.env,
      OPENVIKING_URL: "http://127.0.0.1:9999", // Intentional offline port
      OPENVIKING_TIMEOUT_MS: "500",
    },
  });

  const response = JSON.parse(stdout.trim());
  assert.ok(Array.isArray(response.injectSteps));
});

test("stop.mjs responds with empty JSON format and does not crash when server offline", async () => {
  const { execFileSync } = await import("node:child_process");
  const scriptPath = new URL("../scripts/stop.mjs", import.meta.url).pathname;

  const inputPayload = JSON.stringify({
    conversationId: "test-conv-12345",
    workspacePaths: ["/tmp"],
    terminationReason: "model_stop",
  });

  const stdout = execFileSync(process.execPath, [scriptPath], {
    input: inputPayload,
    encoding: "utf8",
    env: {
      ...process.env,
      OPENVIKING_URL: "http://127.0.0.1:9999", // Intentional offline port
      OPENVIKING_WRITE_PATH_ASYNC: "false", // Synchronous for test predictability
      OPENVIKING_TIMEOUT_MS: "500",
    },
  });

  const response = JSON.parse(stdout.trim());
  assert.deepEqual(response, {});
});


