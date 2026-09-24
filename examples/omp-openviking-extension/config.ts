import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

import { EXTENSION_VERSION, loadOmpConfig } from "./lib/omp-config.mjs";

/** Hand-maintained: this extension ships no manifest to read a version from. */
export { EXTENSION_VERSION };

export interface OVConfig {
  enabled: boolean;
  endpoint: string;
  apiKey: string;
  account: string;
  user: string;
  peerId: string;
  userAgent: string;
  workspacePeer: boolean;
  recallPeerScope: "actor" | "all";
  recallQueryExpansion: "auto" | "off";
  recallQueryExpansionConfigured: boolean;
  syncTurns: boolean;
  recallTokenBudget: number;
  recallMaxContentChars: number;
  recallPreferAbstract: boolean;
  recallLimit: number;
  recallLimitConfigured: boolean;
  recallLedger: boolean;
  scoreThreshold: number;
  minQueryLength: number;
  profileTokenBudget: number;
  skillCatalog: boolean;
  skillCatalogTokenBudget: number;
  resumeContextBudget: number;
  commitTokenThreshold: number;
  commitKeepRecentCount: number;
  takeoverEnabled: boolean;
  takeoverTokenThreshold: number;
  takeoverKeepRecentTurns: number;
  takeoverOverviewBudget: number;
  takeoverOverviewPollMs: number;
  takeoverOverviewPollMax: number;
  captureToolResults: boolean;
  captureMode: "semantic" | "keyword";
  captureMaxLength: number;
  captureToolMaxChars: number;
  captureAssistantTurns: boolean;
  /** Kept as this extension's original spelling; projected onto the shared one. */
  bypassPatterns: string[];
  bypassSession: boolean;
  bypassSessionPatterns: string[];
  /** The only switch left for the tool prompt line now the harness owns the tools. */
  mcpEnabled: boolean;
  logLevel: "silent" | "error" | "info";
  debug: boolean;
  debugLogPath: string;
}

export function loadConfigFromModuleUrl(moduleUrl: string): OVConfig {
  return loadConfig(dirname(fileURLToPath(moduleUrl)));
}

/**
 * Load the extension's configuration.
 *
 * This used to be the whole layer stack written out by hand — the `config.json`
 * beside the extension, a dozen `OPENVIKING_*` reads, a clamp per numeric knob —
 * and every other harness had its own slightly different copy. It is one call
 * into the shared schema now; `lib/omp-config.mjs` holds what only omp knows.
 */
export function loadConfig(extensionDir: string): OVConfig {
  return loadOmpConfig(extensionDir) as OVConfig;
}
