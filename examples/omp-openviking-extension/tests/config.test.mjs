import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { pathToFileURL } from "node:url";
import { loadConfig, loadConfigFromModuleUrl } from "../config.ts";
import { collectInertKnobs, loadOmpConfig } from "../lib/omp-config.mjs";

async function withConfigFile(body, fn, env = {}) {
  const dir = await mkdtemp(join(tmpdir(), "ov-pi-config-用户-"));
  const oldEnv = {
    OPENVIKING_URL: process.env.OPENVIKING_URL,
    OPENVIKING_API_KEY: process.env.OPENVIKING_API_KEY,
    OPENVIKING_ACCOUNT: process.env.OPENVIKING_ACCOUNT,
    OPENVIKING_USER: process.env.OPENVIKING_USER,
    OPENVIKING_PEER_ID: process.env.OPENVIKING_PEER_ID,
    OPENVIKING_WORKSPACE_PEER: process.env.OPENVIKING_WORKSPACE_PEER,
    OPENVIKING_RECALL_PEER_SCOPE: process.env.OPENVIKING_RECALL_PEER_SCOPE,
    OPENVIKING_CREDENTIAL_SOURCE: process.env.OPENVIKING_CREDENTIAL_SOURCE,
    OPENVIKING_CLI_CONFIG_FILE: process.env.OPENVIKING_CLI_CONFIG_FILE,
    OPENVIKING_CONFIG_FILE: process.env.OPENVIKING_CONFIG_FILE,
    OPENVIKING_SKILL_CATALOG: process.env.OPENVIKING_SKILL_CATALOG,
    OPENVIKING_SKILL_CATALOG_TOKEN_BUDGET: process.env.OPENVIKING_SKILL_CATALOG_TOKEN_BUDGET,
  };
  process.env.OPENVIKING_CREDENTIAL_SOURCE = "env";
  process.env.OPENVIKING_URL = "http://127.0.0.1:1933";
  delete process.env.OPENVIKING_API_KEY;
  delete process.env.OPENVIKING_ACCOUNT;
  delete process.env.OPENVIKING_USER;
  delete process.env.OPENVIKING_PEER_ID;
  delete process.env.OPENVIKING_WORKSPACE_PEER;
  delete process.env.OPENVIKING_RECALL_PEER_SCOPE;
  delete process.env.OPENVIKING_CLI_CONFIG_FILE;
  delete process.env.OPENVIKING_CONFIG_FILE;
  for (const [key, value] of Object.entries(env)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }

  try {
    await writeFile(join(dir, "config.json"), JSON.stringify(body), "utf8");
    return await fn(loadConfig(dir), dir);
  } finally {
    for (const [key, value] of Object.entries(oldEnv)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    await rm(dir, { recursive: true, force: true });
  }
}

test("loadConfig defaults takeover on", async () => {
  await withConfigFile({}, (cfg) => {
    assert.equal(cfg.takeoverEnabled, true);
    assert.equal(cfg.takeoverTokenThreshold, 30000);
    assert.equal(cfg.takeoverKeepRecentTurns, 3);
    assert.equal(cfg.takeoverOverviewBudget, 3000);
    assert.equal(cfg.takeoverOverviewPollMs, 2000);
    assert.equal(cfg.takeoverOverviewPollMax, 15);
  });
});

test("loadConfig maps nested takeover block", async () => {
  await withConfigFile({
    takeover: {
      enabled: false,
      tokenThreshold: 600,
      keepRecentTurns: 1,
      overviewBudget: 1200,
      overviewPollMs: 10,
      overviewPollMax: 2,
    },
  }, (cfg) => {
    assert.equal(cfg.takeoverEnabled, false);
    assert.equal(cfg.takeoverTokenThreshold, 600);
    assert.equal(cfg.takeoverKeepRecentTurns, 1);
    assert.equal(cfg.takeoverOverviewBudget, 1200);
    assert.equal(cfg.takeoverOverviewPollMs, 10);
    assert.equal(cfg.takeoverOverviewPollMax, 2);
  });
});

test("loadConfigFromModuleUrl decodes Unicode paths", async () => {
  await withConfigFile({
    takeover: {
      tokenThreshold: 2000,
    },
  }, (_cfg, dir) => {
    const moduleUrl = pathToFileURL(join(dir, "index.ts")).href;
    const cfg = loadConfigFromModuleUrl(moduleUrl);
    assert.equal(cfg.takeoverTokenThreshold, 2000);
  });
});

test("loadConfig keeps top-level takeover aliases for compatibility", async () => {
  await withConfigFile({
    takeoverTokenThreshold: 42,
    takeoverKeepRecentTurns: 4,
  }, (cfg) => {
    assert.equal(cfg.takeoverTokenThreshold, 42);
    assert.equal(cfg.takeoverKeepRecentTurns, 4);
  });
});

test("loadConfig clamps invalid takeover values", async () => {
  await withConfigFile({
    takeover: {
      enabled: "no",
      tokenThreshold: -1,
      keepRecentTurns: -5,
      overviewBudget: 1,
      overviewPollMs: -2,
      overviewPollMax: 0,
    },
  }, (cfg) => {
    // `"no"` is a false word now. The hand-written loader kept takeover on for
    // every spelling except the literal `false`, so a config saying `"no"`
    // turned it on — the shared coercion reads the words.
    assert.equal(cfg.takeoverEnabled, false);
    assert.equal(cfg.takeoverTokenThreshold, 1);
    assert.equal(cfg.takeoverKeepRecentTurns, 0);
    assert.equal(cfg.takeoverOverviewBudget, 100);
    assert.equal(cfg.takeoverOverviewPollMs, 0);
    assert.equal(cfg.takeoverOverviewPollMax, 1);
  });
});

test("loadConfig falls back for a takeover switch that is not a boolean", async () => {
  await withConfigFile({ takeover: { enabled: "maybe" } }, (cfg) => {
    assert.equal(cfg.takeoverEnabled, true);
  });
});

test("loadConfig reads the alias spellings the schema already owns", async () => {
  // `recallBudget`, `profileBudget` and `syncTurns` are registered aliases in
  // `shared/config-schema.mjs`, so they keep working without a shim here.
  await withConfigFile({ recallBudget: 3000, profileBudget: 20000, syncTurns: false }, (cfg) => {
    assert.equal(cfg.recallTokenBudget, 3000);
    assert.equal(cfg.profileTokenBudget, 20000);
    assert.equal(cfg.syncTurns, false);
  });
});

test("loadOmpConfig merges ov.conf's omp block over config.json", async () => {
  // `plugin-config.mjs` reads `legacy || ovConfSection(...)`, so passing the
  // extension's own `config.json` as the legacy layer used to drop `ov.conf`'s
  // `omp` block entirely. Both have to land, `ov.conf` the higher of the two.
  const dir = await mkdtemp(join(tmpdir(), "ov-omp-config-json-"));
  const ovDir = await mkdtemp(join(tmpdir(), "ov-omp-ov-conf-"));
  const ovConf = join(ovDir, "ov.conf");
  try {
    await writeFile(join(dir, "config.json"), JSON.stringify({ recallLimit: 7, recallLedger: false }), "utf8");
    // The section is named after the detected harness, and node is not `omp`:
    // the same code runs under both, so `detectHarness()` reads the executable.
    await writeFile(ovConf, JSON.stringify({ pi: { recallLimit: 25 } }), "utf8");
    const cfg = loadOmpConfig(dir, {
      env: { ...process.env, OPENVIKING_CONFIG_FILE: ovConf, OPENVIKING_CREDENTIAL_SOURCE: "" },
      cwd: dir,
    });
    assert.equal(cfg.recallLimit, 25);
    assert.equal(cfg.recallLedger, false);
  } finally {
    await rm(dir, { recursive: true, force: true });
    await rm(ovDir, { recursive: true, force: true });
  }
});

test("collectInertKnobs names the knobs nothing here reads", async () => {
  await withConfigFile({ recallPreferAbstract: false, recallLimit: 5 }, (cfg) => {
    const inert = collectInertKnobs(cfg);
    assert.deepEqual(inert, ["recallPreferAbstract"]);
  });
});

test("loadConfig derives workspace peer by default", async () => {
  // The default is now the repository, so the expectation follows whichever
  // template resolves where the suite runs — inside a checkout that is the
  // remote, and outside one it is still the old working-directory id.
  const { resolveEffectivePeerId } = await import("../shared/workspace-peer.mjs");
  const expected = resolveEffectivePeerId({ cfg: {}, cwd: process.cwd() });
  await withConfigFile({}, (cfg) => {
    assert.equal(cfg.peerId, expected.peerId);
    assert.equal(cfg.workspacePeer, true);
    assert.equal(cfg.recallPeerScope, "all");
  });
});

test("loadConfig keeps explicit peer and actor recall scope", async () => {
  await withConfigFile({
    recallPeerScope: "actor",
    workspacePeer: false,
  }, (cfg) => {
    assert.equal(cfg.peerId, "explicit-peer");
    assert.equal(cfg.workspacePeer, false);
    assert.equal(cfg.recallPeerScope, "actor");
  }, { OPENVIKING_PEER_ID: "explicit-peer" });
});

// The resolved config is the only thing that turns the catalog on: index.ts
// hands it to buildProfileBlock, and without the knob the session block loses
// <available-skills> entirely.
test("loadConfig enables the skill catalog the session block reads", async () => {
  await withConfigFile({}, async (cfg) => {
    assert.equal(cfg.skillCatalog, true);
    assert.equal(cfg.skillCatalogTokenBudget, 1200);

    const { buildProfileBlock } = await import("../shared/profile-inject.mjs");
    const fetchJSON = async (path) => {
      if (path.startsWith("/api/v1/skills")) {
        return {
          ok: true,
          result: {
            skills: [{
              name: "pr-review",
              uri: "viking://user/default/skills/pr-review",
              description: "Review checklist",
            }],
          },
        };
      }
      if (path === "/api/v1/system/status") return { ok: true, result: { user: "default" } };
      if (path.startsWith("/api/v1/content/read")) return { ok: false, status: 404 };
      return { ok: true, result: [] };
    };

    const profile = await buildProfileBlock(fetchJSON, cfg.profileTokenBudget, cfg.peerId, cfg);
    assert.match(profile.block, /<available-skills>/);
    assert.match(profile.block, /pr-review/);
  });

  await withConfigFile({ skillCatalog: false }, (cfg) => assert.equal(cfg.skillCatalog, false));
  await withConfigFile({}, (cfg) => assert.equal(cfg.skillCatalog, false), {
    OPENVIKING_SKILL_CATALOG: "0",
  });
});
