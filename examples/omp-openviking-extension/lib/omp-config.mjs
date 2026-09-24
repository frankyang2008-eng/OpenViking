/**
 * omp's configuration, resolved through the shared schema.
 *
 * Every knob comes from `buildPluginConfig` — the same call every other harness
 * makes — so the layer stack (env → workspace → `ovcli.conf`'s `plugin.omp` →
 * `ov.conf` → defaults), the aliases, the clamps and the `*Configured` flags are
 * the shared implementation rather than this extension's own list. What is left
 * here is what only omp knows: the `config.json` this extension used to keep
 * beside itself, and the `takeover` object its six takeover knobs used to nest
 * inside.
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { KNOBS } from "../shared/config-schema.mjs";
import { loadCredentialFiles } from "../shared/credentials.mjs";
import { buildPluginConfig, ovConfSection, resolveSettings } from "../shared/plugin-config.mjs";

/** Hand-maintained: this extension ships no manifest to read a version from. */
export const EXTENSION_VERSION = "0.1.0";

/**
 * Which harness this process is: the omp fork runs the same extension, and the
 * `plugin.<name>` section and the User-Agent both have to say which one it is.
 * The executable is the only signal — the extension code is identical in both.
 */
export function detectHarness() {
  const exe = process.execPath || process.argv[0] || "";
  const base = exe.split(/[/\\]/).pop() || "";
  return base === "omp" || base.startsWith("omp") ? "omp" : "pi";
}

/**
 * The knobs this extension actually reads.
 *
 * The schema declares 78 and this list is the subset with a consumer, which is
 * not a detail: a knob a user sets that nothing reads looks exactly like a knob
 * that worked. `recallPreferAbstract`, `captureTimeoutMs` and the `*TimeoutMs`
 * family are declared in the extension's own interface and read nowhere, so
 * they stay out of this set on purpose — `collectInertKnobs` is what tells the
 * operator, and leaving them in would be the silent switch again.
 */
export const OMP_CONSUMED_KNOBS = new Set([
  // connection
  "enabled",
  "mcpEnabled",
  // recall
  "recallLimit",
  "scoreThreshold",
  "minQueryLength",
  "recallTokenBudget",
  "recallMaxContentChars",
  "recallPeerScope",
  "recallQueryExpansion",
  "recallLedger",
  "workspacePeer",
  // session
  "profileTokenBudget",
  "resumeContextBudget",
  "skillCatalog",
  "skillCatalogTokenBudget",
  "commitTokenThreshold",
  "commitKeepRecentCount",
  "takeoverEnabled",
  "takeoverTokenThreshold",
  "takeoverKeepRecentTurns",
  "takeoverOverviewBudget",
  "takeoverOverviewPollMs",
  "takeoverOverviewPollMax",
  // capture
  "autoCapture",
  "captureToolResults",
  "captureMode",
  "captureMaxLength",
  "captureToolMaxChars",
  "captureAssistantTurns",
  // bypass, debug
  "bypassSession",
  "bypassSessionPatterns",
  "debug",
  "debugLogPath",
  "logLevel",
]);

/** The nested spelling of the takeover knobs, which `config.json` may still use. */
const TAKEOVER_NESTED = {
  enabled: "takeoverEnabled",
  tokenThreshold: "takeoverTokenThreshold",
  keepRecentTurns: "takeoverKeepRecentTurns",
  overviewBudget: "takeoverOverviewBudget",
  overviewPollMs: "takeoverOverviewPollMs",
  overviewPollMax: "takeoverOverviewPollMax",
};

/**
 * The `config.json` this extension kept beside itself, or `{}`.
 *
 * It stays the lowest configured layer so an existing deployment keeps working;
 * everything above it comes from files a client may edit.
 */
export function readOmpConfigJson(extensionDir) {
  const configPath = join(extensionDir, "config.json");
  try {
    if (!existsSync(configPath)) return {};
    const parsed = JSON.parse(readFileSync(configPath, "utf8"));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    // An unreadable layer is no layer. A hook must never die over a config file.
    return {};
  }
}

/**
 * Flatten the nested `takeover` object onto the six flat knobs.
 *
 * The nesting was this extension's own; the schema has one flat name per knob
 * and every other harness already spells them that way. The canonical name wins
 * when a file carries both, which is the shared resolver's rule for aliases and
 * makes the nested object a fallback rather than an override.
 */
function expandTakeover(file) {
  const out = { ...file };
  const nested = file.takeover;
  delete out.takeover;
  if (!nested || typeof nested !== "object" || Array.isArray(nested)) return out;
  for (const [nestedKey, flatKey] of Object.entries(TAKEOVER_NESTED)) {
    if (out[flatKey] === undefined && nested[nestedKey] !== undefined) out[flatKey] = nested[nestedKey];
  }
  return out;
}

/**
 * The knobs a layer supplied that nothing here reads.
 *
 * Reported once at session start instead of swallowed: the schema is shared, so
 * a knob that works on another harness reaches omp as a setting that looks
 * accepted and changes nothing.
 */
export function collectInertKnobs(config) {
  const configured = new Set(config?.configuredKnobs || []);
  const inert = [];
  for (const knob of KNOBS) {
    if (!configured.has(knob.name)) continue;
    if (OMP_CONSUMED_KNOBS.has(knob.name)) continue;
    inert.push(knob.name);
  }
  return inert;
}

/**
 * The extension's whole configuration.
 *
 * Takes the extension directory rather than a cwd because `config.json` lives
 * beside the code; the workspace layers still resolve against the process's cwd.
 */
export function loadOmpConfig(extensionDir, { env = process.env, cwd = process.cwd() } = {}) {
  // `plugin-config.mjs` reads `legacy || ovConfSection(ovFile, key)`, so handing
  // it `config.json` alone would drop `ov.conf`'s own `omp` block entirely.
  // Merging here keeps both, with `ov.conf` the higher of the two.
  const legacy = {
    ...expandTakeover(readOmpConfigJson(extensionDir)),
    ...ovConfSection(loadCredentialFiles(env).ovFile, detectHarness()),
  };
  const harness = detectHarness();
  const config = buildPluginConfig(harness, {
    cwd,
    env,
    legacy,
    version: EXTENSION_VERSION,
    deriveEffectivePeer: true,
  });

  // Which knobs a layer actually supplied. `buildPluginConfig` resolves them and
  // keeps only the two `*Configured` flags its own callers needed, and the inert
  // report needs the whole set — so this is one more pass over the same layers,
  // through the same function, at startup.
  const { configured } = resolveSettings(harness, {
    env,
    cwd,
    legacy,
    clientVersion: EXTENSION_VERSION,
  });

  return {
    ...config,
    configuredKnobs: [...configured],
    // `syncTurns` is the name this extension reads; `autoCapture` is the knob.
    syncTurns: config.autoCapture,
    // `bypassPatterns` was this extension's own spelling; the shared matcher
    // reads `bypassSessionPatterns`, so both hold the same list.
    bypassPatterns: config.bypassSessionPatterns,
    // OV_DEBUG_LOG is omp's older spelling, kept working so existing setups log.
    debugLogPath: config.debugLogPath || String(env.OV_DEBUG_LOG || "").trim(),
    // The pre-schema spellings of the connection, which the rest of the
    // extension still reads.
    endpoint: config.baseUrl,
    // The whole resolution, not just the id: `legacyPeerId` is what lets recall
    // under `actor` scope still reach memories written before the git-derived
    // peer replaced the path-derived one.
    peerId: config.effectivePeer.peerId,
  };
}
