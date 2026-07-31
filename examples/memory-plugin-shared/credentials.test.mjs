import test from "node:test"
import assert from "node:assert/strict"
import { buildUserAgent, detectHarness } from "./lib/credentials.mjs"

// Real materialized install layouts (from install.sh):
//   claude:    ~/.claude/plugins/...
//   codebuddy: ~/.codebuddy/marketplaces/openviking-local/plugins/openviking-memory/...
//   qoder:     ~/.qoder/plugins/cache/local/<plugin>/...
const CLAUDE_PATH = "/home/u/.claude/plugins/cache/openviking/openviking-memory/scripts/config.mjs"
const CODEBUDDY_PATH =
  "/home/u/.codebuddy/marketplaces/openviking-local/plugins/openviking-memory/scripts/config.mjs"
const QODER_PATH = "/home/u/.qoder/plugins/cache/local/openviking-memory/scripts/config.mjs"
const DEV_CHECKOUT_PATH = "/home/u/OpenViking/examples/claude-code-memory-plugin/scripts/config.mjs"

test("detectHarness identifies codebuddy from its marketplace install path", () => {
  assert.equal(detectHarness({ moduleUrl: CODEBUDDY_PATH, env: {} }), "codebuddy")
})

test("detectHarness identifies qoder from its plugin cache path", () => {
  assert.equal(detectHarness({ moduleUrl: QODER_PATH, env: {} }), "qoder")
})

test("detectHarness identifies claude installs as claude-code", () => {
  assert.equal(detectHarness({ moduleUrl: CLAUDE_PATH, env: {} }), "claude-code")
})

test("detectHarness falls back to claude-code from a dev checkout (no marker)", () => {
  assert.equal(detectHarness({ moduleUrl: DEV_CHECKOUT_PATH, env: {} }), "claude-code")
})

test("detectHarness accepts a file:// URL (import.meta.url shape)", () => {
  assert.equal(detectHarness({ moduleUrl: `file://${QODER_PATH}`, env: {} }), "qoder")
  assert.equal(detectHarness({ moduleUrl: `file://${CODEBUDDY_PATH}`, env: {} }), "codebuddy")
})

test("detectHarness normalizes Windows backslash paths", () => {
  const win = "C:\\Users\\u\\.codebuddy\\marketplaces\\m\\plugins\\openviking-memory\\scripts\\config.mjs"
  assert.equal(detectHarness({ moduleUrl: win, env: {} }), "codebuddy")
})

test("detectHarness handles a Windows file:// URL (import.meta.url shape)", () => {
  const winUrl = "file:///C:/Users/u/.codebuddy/marketplaces/m/plugins/openviking-memory/scripts/config.mjs"
  assert.equal(detectHarness({ moduleUrl: winUrl, env: {} }), "codebuddy")
})

test("detectHarness sanitizes OPENVIKING_HARNESS into a valid UA token", () => {
  const env = { OPENVIKING_HARNESS: "my harness\r\nX-Bad: 1" }
  const detected = detectHarness({ moduleUrl: CODEBUDDY_PATH, env })
  assert.equal(detected, "my-harness--X-Bad--1")
  assert.ok(!/[^A-Za-z0-9._-]/.test(detected))
})

test("buildUserAgent output is always a value undici accepts as a header", () => {
  const env = { OPENVIKING_HARNESS: "codebuddy\r\nX-Injected: evil" }
  const ua = buildUserAgent(detectHarness({ moduleUrl: CODEBUDDY_PATH, env }), "0.4.3")
  assert.doesNotThrow(() => new Headers({ "User-Agent": ua }))
  assert.equal(ua, "openviking-memory-codebuddy--X-Injected--evil/0.4.3")
})

test("detectHarness: OPENVIKING_HARNESS overrides path detection", () => {
  const env = { OPENVIKING_HARNESS: "qoder" }
  assert.equal(detectHarness({ moduleUrl: CODEBUDDY_PATH, env }), "qoder")
  assert.equal(detectHarness({ moduleUrl: DEV_CHECKOUT_PATH, env }), "qoder")
})

test("detectHarness: blank OPENVIKING_HARNESS is ignored", () => {
  assert.equal(detectHarness({ moduleUrl: QODER_PATH, env: { OPENVIKING_HARNESS: "  " } }), "qoder")
})

test("detectHarness: codebuddy marker wins when a path carries both markers", () => {
  const both = "/home/u/.qoder/nested/.codebuddy/plugins/openviking-memory/scripts/config.mjs"
  assert.equal(detectHarness({ moduleUrl: both, env: {} }), "codebuddy")
})

test("detectHarness tolerates missing/garbage moduleUrl without throwing", () => {
  assert.equal(detectHarness({ env: {} }), "claude-code")
  assert.equal(detectHarness({ moduleUrl: "", env: {} }), "claude-code")
  assert.equal(detectHarness({ moduleUrl: null, env: {} }), "claude-code")
  assert.equal(detectHarness({ moduleUrl: "not a url at all ::", env: {} }), "claude-code")
})

test("detectHarness does not match lookalike directory names", () => {
  assert.equal(detectHarness({ moduleUrl: "/home/u/my.codebuddy.old/scripts/config.mjs", env: {} }), "claude-code")
  assert.equal(detectHarness({ moduleUrl: "/home/u/.qoderbackup/scripts/config.mjs", env: {} }), "claude-code")
})

test("buildUserAgent composes detected harness into the UA token", () => {
  assert.equal(
    buildUserAgent(detectHarness({ moduleUrl: CODEBUDDY_PATH, env: {} }), "0.4.3"),
    "openviking-memory-codebuddy/0.4.3",
  )
  assert.equal(
    buildUserAgent(detectHarness({ moduleUrl: DEV_CHECKOUT_PATH, env: {} }), "0.4.3"),
    "openviking-memory-claude-code/0.4.3",
  )
})
