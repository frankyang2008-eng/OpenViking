#!/usr/bin/env node

/**
 * stdio -> streamable-HTTP MCP proxy for the OpenViking omp extension.
 *
 * omp starts this process as a local MCP server, from the `openviking` entry
 * `install.sh` writes into `<omp agent dir>/mcp.json`. The proxy resolves its
 * connection through the extension's own config — the same `loadOmpConfig()`
 * every hook already uses — so credentials stay in `ovcli.conf` / `ov.conf`
 * rather than in mcp.json, and a rotated token is picked up without a
 * reinstall. stdout stays protocol-clean; anything the proxy wants to say goes
 * to the debug log.
 */

import { resolve as resolvePath } from "node:path";
import { fileURLToPath } from "node:url";
import { loadOmpConfig } from "../lib/omp-config.mjs";
import { createLogger } from "../shared/debug-log.mjs";
import { toMcpProxyConfig } from "../shared/mcp-proxy-config.mjs";
import { createOpenVikingMcpProxy } from "../shared/mcp-proxy-core.mjs";

const EXTENSION_ROOT = resolvePath(fileURLToPath(import.meta.url), "..", "..");

export function readProxyConfig(env = process.env, cwd = process.cwd()) {
  return toMcpProxyConfig(loadOmpConfig(EXTENSION_ROOT, { env, cwd }), { env });
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolvePath(process.argv[1])) {
  createOpenVikingMcpProxy({ readConfig: readProxyConfig, loggerFactory: createLogger }).start();
}
