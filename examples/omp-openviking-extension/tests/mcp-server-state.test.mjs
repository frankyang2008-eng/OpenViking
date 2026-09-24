import test from "node:test"
import assert from "node:assert/strict"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { readMcpServerState, resolveAgentDir } from "../lib/mcp-server-state.mjs"

async function withMcpJson(content, fn) {
  const dir = await mkdtemp(join(tmpdir(), "ov-omp-mcp-"))
  try {
    if (content !== null) await writeFile(join(dir, "mcp.json"), content)
    return await fn(dir)
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
}

test("agent dir is derived from the extension's install location", () => {
  assert.equal(resolveAgentDir("/home/u/.omp/agent/extensions/openviking"), "/home/u/.omp/agent")
})

test("an installed entry reports the server as available", async () => {
  await withMcpJson(JSON.stringify({
    mcpServers: { openviking: { command: "node", args: ["/x/mcp-proxy.mjs"] } },
  }), (dir) => {
    assert.deepEqual(readMcpServerState(dir), { present: true, enabled: true, disabled: false })
  })
})

test("a missing mcp.json means no tools, not an error", async () => {
  await withMcpJson(null, (dir) => {
    assert.deepEqual(readMcpServerState(dir), { present: false, enabled: false, disabled: false })
  })
})

test("an entry with enabled false is present but not available", async () => {
  await withMcpJson(JSON.stringify({
    mcpServers: { openviking: { command: "node", args: [], enabled: false } },
  }), (dir) => {
    assert.equal(readMcpServerState(dir).enabled, false)
  })
})

test("the disabledServers denylist wins over the entry", async () => {
  await withMcpJson(JSON.stringify({
    disabledServers: ["openviking"],
    mcpServers: { openviking: { command: "node", args: [] } },
  }), (dir) => {
    const state = readMcpServerState(dir)
    assert.equal(state.enabled, false)
    assert.equal(state.disabled, true)
  })
})

test("another server in the file is not mistaken for ours", async () => {
  await withMcpJson(JSON.stringify({
    mcpServers: { "codebase-memory-mcp": { command: "x", args: [] } },
  }), (dir) => {
    assert.equal(readMcpServerState(dir).present, false)
  })
})
