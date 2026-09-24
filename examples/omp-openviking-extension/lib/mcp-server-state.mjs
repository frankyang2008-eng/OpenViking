import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/** The MCP server name install.sh writes into mcp.json. */
export const OMP_SERVER_NAME = "openviking";

/**
 * omp's agent directory, derived from where this extension is installed.
 *
 * install.sh puts the extension at `<agent dir>/extensions/openviking` with
 * mcp.json beside it, so the extension's own location is the authority — no
 * re-derivation of omp's profile/env precedence, which install.sh already
 * resolved once when it chose where to copy this file.
 */
export function resolveAgentDir(extensionDir) {
  return resolve(extensionDir, "..", "..");
}

export function agentDirForModuleUrl(moduleUrl) {
  return resolveAgentDir(dirname(fileURLToPath(moduleUrl)));
}

/**
 * What omp will do with the `openviking` MCP server, read from the file omp
 * reads.
 *
 * `disabledServers` is a denylist living in that same file and wins over the
 * entry itself, so a user who disabled OpenViking is not told its tools are
 * available. A missing or unreadable mcp.json means no tools, which is the
 * honest answer for a checkout that never went through install.sh.
 */
export function readMcpServerState(agentDir, serverName = OMP_SERVER_NAME) {
  let config;
  try {
    config = JSON.parse(readFileSync(resolve(agentDir, "mcp.json"), "utf8"));
  } catch {
    return { present: false, enabled: false, disabled: false };
  }
  const entry = config?.mcpServers?.[serverName];
  const disabled = Array.isArray(config?.disabledServers) && config.disabledServers.includes(serverName);
  return {
    present: Boolean(entry),
    enabled: Boolean(entry) && entry.enabled !== false && !disabled,
    disabled,
  };
}
