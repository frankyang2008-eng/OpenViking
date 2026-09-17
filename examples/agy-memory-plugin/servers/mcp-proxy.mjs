#!/usr/bin/env node

import { fileURLToPath } from "node:url";
import { resolve as resolvePath } from "node:path";

import { loadAgentHookConfig } from "../../memory-plugin-shared/lib/agent-hook-runtime.mjs";
import { createLogger } from "../../memory-plugin-shared/lib/debug-log.mjs";
import { toMcpProxyConfig } from "../../memory-plugin-shared/lib/mcp-proxy-config.mjs";
import { createOpenVikingMcpProxy } from "../../memory-plugin-shared/lib/mcp-proxy-core.mjs";

export function readProxyConfig(env = process.env) {
  const cfg = loadAgentHookConfig("agy", undefined, { env });
  // Shaped by the shared mapper, like every other proxy: it owns the /mcp
  // derivation, the identity and extra headers, the peer-header decision, and
  // the env passthrough.
  return toMcpProxyConfig(cfg, { env });
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolvePath(process.argv[1])) {
  createOpenVikingMcpProxy({ readConfig: readProxyConfig, loggerFactory: createLogger }).start();
}
