// GENERATED FROM examples/memory-plugin-shared/lib. DO NOT EDIT.
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve as resolvePath } from "node:path";
import { fileURLToPath } from "node:url";

const DEFAULT_OVCLI_CONF_PATH = join(homedir(), ".openviking", "ovcli.conf");
const DEFAULT_OV_CONF_PATH = join(homedir(), ".openviking", "ov.conf");
const DEFAULT_BASE_URL = "http://127.0.0.1:1933";

function str(val, fallback = "") {
  if (typeof val === "string" && val.trim()) return val.trim();
  return fallback;
}

function normalizePath(value) {
  const raw = str(value, "");
  if (!raw) return "";
  if (raw === "~") return homedir();
  if (raw.startsWith("~/")) return resolvePath(join(homedir(), raw.slice(2)));
  return resolvePath(raw);
}

function tryLoadJson(path) {
  if (!path) return null;
  try {
    return JSON.parse(readFileSync(path, "utf-8"));
  } catch {
    return null;
  }
}

/**
 * Build the User-Agent every harness plugin sends on OpenViking-bound requests.
 * Shape is `name/semver` so downstream stats layers can parse it as one token.
 */
export function buildUserAgent(harness, version) {
  return `openviking-memory-${harness}/${str(version, "") || "0.0.0"}`;
}

/**
 * Detect which harness loaded this plugin, from the plugin's own install path.
 * Materialized installs land under the harness's config dir (~/.claude/...,
 * ~/.codebuddy/marketplaces/..., ~/.qoder/plugins/...), so the module URL
 * carries the harness identity. Falls back to "claude-code" — the historical
 * default — when no marker matches (e.g. running from a dev checkout), which
 * preserves pre-detection behavior instead of failing open to a wrong label.
 *
 * OPENVIKING_HARNESS overrides detection for non-standard install layouts.
 *
 * `moduleUrl` accepts an import.meta.url (file:// URL) or a plain path string.
 */
export function detectHarness({ moduleUrl, env = process.env } = {}) {
  const override = str(env?.OPENVIKING_HARNESS, "");
  if (override) {
    // The override lands verbatim in a User-Agent HTTP header. Strip anything
    // outside the UA-token charset — control chars (e.g. CR/LF) make undici
    // reject the header, which would fail every plugin request. Mirrors
    // safePart() in agent-hook-runtime.mjs. Path detection below returns
    // hardcoded literals, so only this env path needs sanitizing.
    const safe = override.replace(/[^A-Za-z0-9._-]/g, "-");
    if (safe) return safe;
  }

  let modulePath = "";
  try {
    modulePath = fileURLToPath(moduleUrl);
  } catch {
    modulePath = str(moduleUrl, "");
  }
  const normalized = modulePath.replace(/\\/g, "/");

  // Markers are disjoint config-dir names, so check order only sets precedence
  // when a path somehow carries both (codebuddy wins). No marker → fallback.
  if (normalized.includes("/.codebuddy/")) return "codebuddy";
  if (normalized.includes("/.qoder/")) return "qoder";
  return "claude-code";
}

/**
 * Read a plugin manifest's `version` field. Accepts a path or a URL (so callers
 * can resolve relative to import.meta.url). Returns "" when unreadable so the
 * User-Agent falls back to 0.0.0 instead of throwing inside a short-lived hook.
 */
export function readManifestVersion(manifest) {
  try {
    return str(JSON.parse(readFileSync(manifest, "utf-8")).version, "");
  } catch {
    return "";
  }
}

function looksLikeOvcli(obj) {
  if (!obj || typeof obj !== "object") return false;
  if (obj.server && typeof obj.server === "object") return false;
  return Boolean(
    typeof obj.url === "string" ||
    typeof obj.api_key === "string" ||
    typeof obj.account === "string" ||
    typeof obj.account_id === "string" ||
    typeof obj.user === "string" ||
    typeof obj.user_id === "string" ||
    typeof obj.actor_peer_id === "string",
  );
}

function hasCredentialFields(obj) {
  if (!obj || typeof obj !== "object") return false;
  return [
    "url",
    "api_key",
    "account",
    "account_id",
    "user",
    "user_id",
    "actor_peer_id",
    "peer_id",
  ].some((key) => typeof obj[key] === "string");
}

export function loadCredentialFiles(env = process.env) {
  const cliPathCandidate = normalizePath(env.OPENVIKING_CLI_CONFIG_FILE) || DEFAULT_OVCLI_CONF_PATH;
  const ovPathCandidate = normalizePath(env.OPENVIKING_CONFIG_FILE) || DEFAULT_OV_CONF_PATH;
  const cliPathEnv = Boolean(str(env.OPENVIKING_CLI_CONFIG_FILE, ""));
  const ovPathEnv = Boolean(str(env.OPENVIKING_CONFIG_FILE, ""));

  let cliFile = tryLoadJson(cliPathCandidate);
  let cliPath = cliFile ? cliPathCandidate : "";
  let ovFile = tryLoadJson(ovPathCandidate);
  let ovPath = ovFile ? ovPathCandidate : "";

  // Backward compat: older plugin installs used OPENVIKING_CONFIG_FILE for
  // both ov.conf and ovcli.conf. Preserve that when the file is ovcli-shaped.
  if (ovPathEnv && !cliPathEnv && looksLikeOvcli(ovFile)) {
    cliFile = ovFile;
    cliPath = ovPath;
    ovFile = null;
    ovPath = "";
  }

  return {
    cliFile: cliFile || {},
    cliPath,
    cliPathCandidate,
    ovFile: ovFile || {},
    ovPath,
  };
}

function sourceMode(env) {
  const raw = str(env.OPENVIKING_CREDENTIAL_SOURCE, str(env.OPENVIKING_CREDENTIALS_SOURCE, "auto"))
    .toLowerCase();
  if (raw === "env" || raw === "environment") return "env";
  if (raw === "cli" || raw === "ovcli" || raw === "file" || raw === "config") return "cli";
  return "auto";
}

function hasEnvCredentialFields(env) {
  return Boolean(
    str(env.OPENVIKING_URL, str(env.OPENVIKING_BASE_URL, "")) ||
    str(env.OPENVIKING_MCP_URL, "") ||
    str(env.OPENVIKING_BEARER_TOKEN, str(env.OPENVIKING_API_KEY, "")) ||
    str(env.OPENVIKING_ACCOUNT, "") ||
    str(env.OPENVIKING_USER, "") ||
    str(env.OPENVIKING_PEER_ID, ""),
  );
}

function deriveBaseUrl({ env, cliFile, ovFile, mode, useCli }) {
  const envUrl = str(env.OPENVIKING_URL, str(env.OPENVIKING_BASE_URL, ""));
  const cliUrl = str(cliFile.url, "");

  if (mode !== "cli" && envUrl) return envUrl.replace(/\/+$/, "");
  if (useCli && cliUrl) return cliUrl.replace(/\/+$/, "");
  if (mode !== "env" && cliUrl) return cliUrl.replace(/\/+$/, "");

  const server = ovFile.server || {};
  const ovUrl = str(server.url, "");
  if (ovUrl) return ovUrl.replace(/\/+$/, "");

  const host = str(server.host, "127.0.0.1").replace("0.0.0.0", "127.0.0.1");
  const port = Number.isFinite(Number(server.port)) ? Math.floor(Number(server.port)) : 1933;
  return `http://${host}:${port}`;
}

export function resolveOpenVikingCredentials(env = process.env) {
  const files = loadCredentialFiles(env);
  const mode = sourceMode(env);
  const envHasCredentials = hasEnvCredentialFields(env);
  const useCli = mode === "cli" ||
    (mode === "auto" && !envHasCredentials && files.cliPath && hasCredentialFields(files.cliFile));
  const cx = files.ovFile.codex || {};
  const server = files.ovFile.server || {};

  const baseUrl = deriveBaseUrl({ env, ...files, mode, useCli });

  const apiKey = useCli
    ? str(files.cliFile.api_key, "")
    : (
        str(env.OPENVIKING_BEARER_TOKEN, "") ||
        str(env.OPENVIKING_API_KEY, "") ||
        str(files.cliFile.api_key, "") ||
        str(cx.apiKey, "") ||
        str(server.root_api_key, "")
      );

  const account = useCli
    ? str(files.cliFile.account, str(files.cliFile.account_id, ""))
    : (
        str(env.OPENVIKING_ACCOUNT, "") ||
        str(files.cliFile.account, str(files.cliFile.account_id, "")) ||
        str(cx.accountId, "")
      );

  const user = useCli
    ? str(files.cliFile.user, str(files.cliFile.user_id, ""))
    : (
        str(env.OPENVIKING_USER, "") ||
        str(files.cliFile.user, str(files.cliFile.user_id, "")) ||
        str(cx.userId, "")
      );

  const peerId = useCli
    ? str(files.cliFile.actor_peer_id, str(files.cliFile.peer_id, ""))
    : (
        str(env.OPENVIKING_PEER_ID, "") ||
        str(files.cliFile.actor_peer_id, str(files.cliFile.peer_id, "")) ||
        str(cx.peerId, str(cx.peer_id, ""))
      );

  const explicitMcpUrl = str(env.OPENVIKING_MCP_URL, "");
  const mcpUrl = (mode !== "cli" && explicitMcpUrl) ? explicitMcpUrl : `${baseUrl.replace(/\/+$/, "")}/mcp`;

  // The file that actually supplied the api_key, following the same chain —
  // empty when the key came from the environment or was never found.
  let credentialPath = "";
  if (apiKey) {
    if (useCli) credentialPath = files.cliPath;
    else if (str(env.OPENVIKING_BEARER_TOKEN, str(env.OPENVIKING_API_KEY, ""))) credentialPath = "";
    else if (str(files.cliFile.api_key, "")) credentialPath = files.cliPath;
    else credentialPath = files.ovPath;
  }

  return {
    ...files,
    credentialSource: useCli ? "ovcli" : ((mode === "env" || envHasCredentials) ? "env" : "auto"),
    credentialPath,
    baseUrl,
    mcpUrl,
    apiKey,
    account,
    user,
    peerId,
    hasApiKey: Boolean(apiKey),
  };
}

function main() {
  const cmd = process.argv[2] || "";
  if (cmd === "mcp-url") {
    process.stdout.write(resolveOpenVikingCredentials().mcpUrl);
    return;
  }
  if (cmd === "has-api-key") {
    process.stdout.write(resolveOpenVikingCredentials().hasApiKey ? "1" : "0");
    return;
  }
  if (cmd === "has-peer-id") {
    process.stdout.write(resolveOpenVikingCredentials().peerId ? "1" : "0");
    return;
  }
  process.stderr.write("usage: ov-credentials.mjs <mcp-url|has-api-key|has-peer-id>\n");
  process.exitCode = 2;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolvePath(process.argv[1])) {
  main();
}
