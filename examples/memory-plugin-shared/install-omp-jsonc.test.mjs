/**
 * omp reads MCP servers from `mcp.json` and keeps that file next to the
 * extensions directory.
 *
 * The entry this installer adds has to satisfy omp's own schema — a stdio
 * server in a top-level `mcpServers` map — while sharing the file with servers
 * the user added by hand, and it has to survive a reinstall without losing
 * decisions the user made about it. Removal is the mirror: it takes back the
 * entry this installer wrote and nothing else.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { removeOmpMcpEntry, stripJsonc, updateOmpMcpConfig } from "./lib/install/jsonc-edit.mjs";

const EXT_DIR = "/home/u/.omp/agent/extensions/openviking";
const PROXY = `${EXT_DIR}/servers/mcp-proxy.mjs`;
const CLI = fileURLToPath(new URL("./lib/install/jsonc-edit.mjs", import.meta.url));

async function withConfig(raw, fn) {
  const dir = await mkdtemp(join(tmpdir(), "ov-mcp-"));
  const file = join(dir, "mcp.json");
  if (raw !== null) await writeFile(file, raw);
  try {
    return await fn(file);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

test("the entry is a stdio server omp's schema accepts", () => {
  const next = parse(updateOmpMcpConfig("{}\n", { mcpProxy: PROXY }));
  assert.deepEqual(next.mcpServers.openviking, {
    type: "stdio",
    command: "node",
    args: [PROXY],
    enabled: true,
  });
});

test("no timeout is invented: omp's own 30s default stands", () => {
  const next = parse(updateOmpMcpConfig("{}\n", { mcpProxy: PROXY }));
  assert.equal("timeout" in next.mcpServers.openviking, false);
});

test("servers the user added are untouched, comments and all", () => {
  const raw = `{
  // npx, because it is already on PATH in this shell
  "mcpServers": {
    "codebase-memory-mcp": { "type": "stdio", "command": "npx", "args": ["-y", "cbm"] },
  },
  "disabledServers": ["something-else"]
}
`;
  const next = updateOmpMcpConfig(raw, { mcpProxy: PROXY });
  assert.match(next, /npx, because it is already on PATH/);
  const parsed = parse(next);
  assert.deepEqual(parsed.mcpServers["codebase-memory-mcp"], { type: "stdio", command: "npx", args: ["-y", "cbm"] });
  assert.deepEqual(parsed.disabledServers, ["something-else"]);
  assert.equal(parsed.mcpServers.openviking.args[0], PROXY);
});

test("a server the user disabled is not resurrected by a reinstall", () => {
  const raw = `${JSON.stringify({ mcpServers: { openviking: { type: "stdio", command: "node", args: [PROXY], enabled: false } } }, null, 2)}\n`;
  assert.equal(updateOmpMcpConfig(raw, { mcpProxy: PROXY }), raw);
});

test("settings the user added to our entry survive a reinstall, a stale proxy path does not", () => {
  const raw = `${JSON.stringify({
    mcpServers: {
      openviking: { type: "stdio", command: "node", args: ["/old/place/mcp-proxy.mjs"], timeout: 90000, env: { OV_DEBUG: "1" } },
    },
  }, null, 2)}\n`;
  const entry = parse(updateOmpMcpConfig(raw, { mcpProxy: PROXY })).mcpServers.openviking;
  assert.deepEqual(entry.args, [PROXY]);
  assert.equal(entry.timeout, 90000);
  assert.deepEqual(entry.env, { OV_DEBUG: "1" });
});

test("an empty file becomes a config rather than a crash", () => {
  const next = parse(updateOmpMcpConfig("", { mcpProxy: PROXY }));
  assert.equal(next.mcpServers.openviking.args[0], PROXY);
});

test("the write path install.sh calls lands in the file", async () => {
  await withConfig('{\n  "mcpServers": {}\n}\n', async (file) => {
    execFileSync(process.execPath, [CLI, file, "", PROXY, "omp"], { stdio: "pipe" });
    const entry = parse(await readFile(file, "utf8")).mcpServers.openviking;
    assert.equal(entry.command, "node");
    assert.deepEqual(entry.args, [PROXY]);
  });
});

test("removal takes our entry and leaves the file valid", () => {
  const raw = `${JSON.stringify({
    mcpServers: {
      openviking: { type: "stdio", command: "node", args: [PROXY] },
      "codebase-memory-mcp": { type: "stdio", command: "npx", args: ["-y", "cbm"] },
    },
  }, null, 2)}\n`;
  const next = removeOmpMcpEntry(raw, { extensionDir: EXT_DIR });
  const parsed = parse(next);
  assert.equal("openviking" in parsed.mcpServers, false);
  assert.equal(parsed.mcpServers["codebase-memory-mcp"].command, "npx");
});

test("the last server leaves no empty map behind", () => {
  const raw = `${JSON.stringify({ mcpServers: { openviking: { command: "node", args: [PROXY] } } }, null, 2)}\n`;
  assert.deepEqual(parse(removeOmpMcpEntry(raw, { extensionDir: EXT_DIR })), {});
});

test("an entry that points somewhere else is not ours to delete", () => {
  const raw = `${JSON.stringify({ mcpServers: { openviking: { type: "http", url: "http://localhost:1933/mcp" } } }, null, 2)}\n`;
  assert.equal(removeOmpMcpEntry(raw, { extensionDir: EXT_DIR }), raw);
});

test("a file we cannot read is left alone", () => {
  const raw = '{ "mcpServers": { "openviking": { … } } }\n';
  assert.equal(removeOmpMcpEntry(raw, { extensionDir: EXT_DIR }), raw);
});

test("the remove path install.sh calls edits the file in place", async () => {
  await withConfig(`${JSON.stringify({ mcpServers: { openviking: { command: "node", args: [PROXY] } } }, null, 2)}\n`, async (file) => {
    execFileSync(process.execPath, [CLI, "remove-omp", file, EXT_DIR], { stdio: "pipe" });
    assert.deepEqual(JSON.parse(await readFile(file, "utf8")), {});
  });
});

/** What the file parses to once the comments and trailing commas are gone. */
function parse(raw) {
  return JSON.parse(stripJsonc(raw));
}
