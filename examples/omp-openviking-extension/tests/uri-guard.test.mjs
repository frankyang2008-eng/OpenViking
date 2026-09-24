import test from "node:test"
import assert from "node:assert/strict"
import { guardVikingUriToolCall, noticeVikingUriToolResult } from "../lib/uri-guard-adapter.mjs"

test("guard blocks builtin file tools on viking URIs", () => {
  const decision = guardVikingUriToolCall({
    type: "tool_call",
    toolName: "read",
    input: { filePath: "viking://resources/project/file.md" },
  })

  assert.equal(decision?.block, true)
  assert.match(decision?.reason ?? "", /viking:\/\/ URIs are OpenViking virtual paths/)
  assert.match(decision?.reason ?? "", /Use mcp__openviking_read instead/)
  assert.match(decision?.reason ?? "", /mcp__openviking_read\(uris=\["viking:\/\/resources\/project\/file\.md"\]\)/)
})

test("guard blocks an edit whose filePath is a viking URI", () => {
  // omp's edit carries `filePath`, not `path`: narrowing the guard input to
  // `path` alone would let this one through.
  const decision = guardVikingUriToolCall({
    toolName: "edit",
    input: { filePath: "viking://user/default/memories/prefs.md", edits: [{ oldText: "a", newText: "b" }] },
  })

  assert.equal(decision?.block, true)
  assert.match(decision?.reason ?? "", /Use mcp__openviking_edit instead/)
})

test("guard leaves a local edit alone when only its replacement text mentions a viking URI", () => {
  const decision = guardVikingUriToolCall({
    toolName: "edit",
    input: {
      filePath: "/tmp/notes.md",
      edits: [{ oldText: "TODO", newText: "see viking://user/default/memories/prefs.md" }],
    },
  })

  assert.equal(decision, null)
})

test("guard points a write or edit at a skill to add_skill, not the refused tool", () => {
  const write = guardVikingUriToolCall({
    toolName: "write",
    input: { filePath: "viking://~/skills/pr-review/SKILL.md", content: "# skill" },
  })
  assert.equal(write?.block, true)
  assert.match(write?.reason ?? "", /Use mcp__openviking_add_skill instead/)

  const edit = guardVikingUriToolCall({
    toolName: "edit",
    input: { filePath: "viking://agent/skills/pr-review/SKILL.md" },
  })
  assert.equal(edit?.block, true)
  assert.match(edit?.reason ?? "", /mcp__openviking_add_skill\(/)
  // A shared skill goes back to the shared root; without target_uri, add_skill
  // makes a private copy.
  assert.match(edit?.reason ?? "", /target_uri="viking:\/\/agent\/skills"/)
})

test("guard allows normal local paths and the OpenViking MCP tools", () => {
  assert.equal(guardVikingUriToolCall({ toolName: "read", input: { filePath: "/tmp/file.md" } }), null)
  assert.equal(
    guardVikingUriToolCall({ toolName: "mcp__openviking_read", input: { uris: ["viking://resources/file.md"] } }),
    null,
  )
})

test("a bash command with a viking URI runs, then the model gets a notice", () => {
  // Unlike the file tools, this is not blocked: a viking:// URI in a command
  // line is as often data (an `ov` argument, a grep pattern) as a path.
  const call = { toolName: "bash", input: { command: "cat viking://resources/project/file.md" } }
  assert.equal(guardVikingUriToolCall(call), null)

  const result = noticeVikingUriToolResult({ ...call, content: [{ type: "text", text: "ORIGINAL OUTPUT" }] })
  assert.equal(result?.content.length, 2)
  assert.deepEqual(result?.content[0], { type: "text", text: "ORIGINAL OUTPUT" })
  assert.match(result?.content[1].text ?? "", /URI guard: this shell command contains the viking:\/\/ URI/)
  assert.match(result?.content[1].text ?? "", /use mcp__openviking_read or mcp__openviking_search instead/)
})

test("no notice for tools the guard does not own", () => {
  assert.equal(
    noticeVikingUriToolResult({ toolName: "bash", input: { command: "ls /tmp" }, content: [] }),
    null,
  )
  assert.equal(noticeVikingUriToolResult({ toolName: "read", input: { filePath: "viking://x" }, content: [] }), null)
})
