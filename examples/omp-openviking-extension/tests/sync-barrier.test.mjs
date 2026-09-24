import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { SyncManager } from "../sync.ts";
import { enqueue, listPending } from "../shared/pending-queue.mjs";
import { BATCH_LIMIT } from "../shared/batch-send.mjs";

function config(overrides = {}) {
  return {
    commitTokenThreshold: 20000,
    commitKeepRecentCount: 10,
    captureAssistantTurns: true,
    captureToolMaxChars: 2000,
    captureMaxLength: 24000,
    takeoverEnabled: true,
    ...overrides,
  };
}

function client(overrides = {}) {
  return {
    connected: true,
    addMessagePayload: async () => true,
    getSession: async () => ({ pending_tokens: 0 }),
    commitSession: async () => ({ task_id: "t-1", archive_uri: "viking://archive/1" }),
    commitSessionResponse: async () => ({
      result: { task_id: "t-1", archive_uri: "viking://archive/1" },
    }),
    fetchJSON: async () => ({ ok: true, result: {} }),
    ...overrides,
  };
}

async function withPendingDir(fn) {
  const previous = process.env.OPENVIKING_PENDING_DIR;
  const dir = await mkdtemp(join(tmpdir(), "ov-pi-pending-"));
  process.env.OPENVIKING_PENDING_DIR = dir;
  try {
    return await fn(dir);
  } finally {
    if (previous === undefined) delete process.env.OPENVIKING_PENDING_DIR;
    else process.env.OPENVIKING_PENDING_DIR = previous;
    await rm(dir, { recursive: true, force: true });
  }
}

test("syncBranch returns added token accounting and delivered status", async () => {
  await withPendingDir(async () => {
    const c = client();
    const sync = new SyncManager(c, config({ takeoverEnabled: false }));
    await sync.ensureSession("pi-session");

    const result = await sync.syncBranch([
      { type: "message", message: { role: "user", content: "Remember this implementation decision for the next run." } },
    ]);

    assert.equal(result.added, 1);
    assert.ok(result.tokens > 0);
    assert.equal(result.allDelivered, true);
    assert.equal(sync.syncedCount, 1);
  });
});

test("syncBranch sends a whole turn in one batch request", async () => {
  await withPendingDir(async () => {
    const calls = [];
    const c = client({
      fetchJSON: async (path, init) => {
        calls.push({ path, body: JSON.parse(init.body) });
        return { ok: true, result: {} };
      },
    });
    const sync = new SyncManager(c, config({ takeoverEnabled: false }));
    await sync.ensureSession("pi-batch-session");

    const branch = ["alpha", "beta", "gamma"].map((marker) => ({
      type: "message",
      message: { role: "user", content: `Remember this implementation decision ${marker} for the next run.` },
    }));
    const result = await sync.syncBranch(branch);

    // One request for the turn, not one per message: the old loop called
    // `addMessagePayload` once per payload.
    assert.ok(result.added >= 2, "the branch has to yield more than one payload to prove batching");
    assert.equal(calls.length, 1);
    assert.match(calls[0].path, /\/messages\/batch$/);
    assert.equal(calls[0].body.messages.length, result.added);
  });
});

test("syncBranch splits a turn larger than BATCH_LIMIT", async () => {
  await withPendingDir(async () => {
    const batches = [];
    const c = client({
      fetchJSON: async (_path, init) => {
        batches.push(JSON.parse(init.body).messages.length);
        return { ok: true, result: {} };
      },
    });
    const sync = new SyncManager(c, config({ takeoverEnabled: false }));
    await sync.ensureSession("pi-batch-overflow");

    const branch = Array.from({ length: BATCH_LIMIT + 1 }, (_, i) => ({
      type: "message",
      message: { role: "user", content: `Remember implementation decision number ${i} for the next run.` },
    }));
    const result = await sync.syncBranch(branch);

    assert.ok(result.added > BATCH_LIMIT, `expected more than ${BATCH_LIMIT} payloads, got ${result.added}`);
    assert.equal(batches[0], BATCH_LIMIT, "the first request fills the batch limit");
    assert.equal(
      batches.reduce((sum, n) => sum + n, 0),
      result.added,
      "every accepted payload rode in one of the batch requests",
    );
  });
});

test("commit writes success trace_id to the omp debug log", async () => {
  await withPendingDir(async (dir) => {
    const previous = process.env.OV_DEBUG_LOG;
    const debugLogPath = join(dir, "omp-debug.log");
    // The logger reads the resolved `debugLogPath`; mapping OV_DEBUG_LOG onto
    // it is the config layer's job, and config.test.mjs covers that mapping.
    delete process.env.OV_DEBUG_LOG;
    try {
      const c = client({
        commitSessionResponse: async () => ({
          result: {
            task_id: "t-trace",
            archive_uri: "viking://archive/trace",
            trace_id: "trace-pi-commit",
          },
          traceId: "trace-pi-commit",
        }),
      });
      const sync = new SyncManager(c, config({ debugLogPath }));
      await sync.ensureSession("pi-trace-session");

      const result = await sync.commit();
      assert.equal(result.trace_id, "trace-pi-commit");
      const line = JSON.parse((await readFile(debugLogPath, "utf8")).trim());
      assert.equal(line.hook, "omp");
      assert.equal(line.stage, "commit");
      assert.equal(line.data.ok, true);
      assert.equal(line.data.trace_id, "trace-pi-commit");
    } finally {
      if (previous === undefined) delete process.env.OV_DEBUG_LOG;
      else process.env.OV_DEBUG_LOG = previous;
    }
  });
});

test("commit writes failure trace_id to the omp debug log", async () => {
  await withPendingDir(async (dir) => {
    const previous = process.env.OV_DEBUG_LOG;
    const debugLogPath = join(dir, "omp-debug-error.log");
    delete process.env.OV_DEBUG_LOG;
    try {
      const c = client({
        commitSessionResponse: async () => ({
          result: null,
          status: 500,
          traceId: "trace-pi-error",
          error: { message: "commit failed" },
        }),
      });
      const sync = new SyncManager(c, config({ debugLogPath }));
      await sync.ensureSession("pi-trace-error");

      assert.equal(await sync.commit({ queueOnFailure: false }), null);
      const line = JSON.parse((await readFile(debugLogPath, "utf8")).trim());
      assert.equal(line.stage, "commit");
      assert.equal(line.data.ok, false);
      assert.equal(line.data.trace_id, "trace-pi-error");
      assert.equal(line.data.error, "commit failed");
    } finally {
      if (previous === undefined) delete process.env.OV_DEBUG_LOG;
      else process.env.OV_DEBUG_LOG = previous;
    }
  });
});

test("queued addMessage makes takeover flush barrier false until replay succeeds", async () => {
  await withPendingDir(async () => {
    let replayOk = false;
    const c = client({
      fetchJSON: async () => ({ ok: replayOk, status: replayOk ? 200 : 500, result: {} }),
    });
    const sync = new SyncManager(c, config());
    await sync.ensureSession("pi-session");

    const result = await sync.syncBranch([
      { type: "message", message: { role: "user", content: "This should be queued for takeover barrier testing." } },
    ]);

    assert.equal(result.added, 1);
    assert.equal(result.allDelivered, false);
    assert.equal((await listPending()).length, 1);
    assert.equal(await sync.flushForTakeover(), false);

    replayOk = true;
    assert.equal(await sync.flushForTakeover(), true);
    assert.equal((await listPending()).length, 0);
  });
});

test("current-session addMessage 500 remains queued and keeps barrier closed", async () => {
  await withPendingDir(async () => {
    const c = client({
      fetchJSON: async () => ({ ok: false, status: 500 }),
    });
    const sync = new SyncManager(c, config());
    await sync.ensureSession("pi-session");

    await sync.addPayload({ role: "user", content: "Queued content with retryable server failure." });

    assert.equal(await sync.flushForTakeover(), false);
    const pending = await listPending();
    assert.equal(pending.length, 1);
    assert.equal(pending[0].entry.type, "addMessage");
    assert.equal(pending[0].entry.sessionId, sync.sessionId);
  });
});

test("other-session addMessage and commit queue entries do not block takeover barrier", async () => {
  await withPendingDir(async () => {
    const c = client({
      fetchJSON: async () => ({ ok: false, status: 500 }),
    });
    const sync = new SyncManager(c, config());
    await sync.ensureSession("pi-session");

    await enqueue("addMessage", "different-session", { role: "user", content: "other" });
    await enqueue("commitSession", sync.sessionId, { keep_recent_count: 1 });

    assert.equal(await sync.flushForTakeover(), true);
  });
});

test("restoreWatermark prevents pi -c from re-syncing already captured entries", async () => {
  await withPendingDir(async () => {
    const sent = [];
    const c = client({
      // The batch path posts to `/messages/batch`; `addMessagePayload` is no
      // longer on the sync path, so the fake counts what actually goes out.
      fetchJSON: async (_path, init) => {
        sent.push(...JSON.parse(init.body).messages);
        return { ok: true, result: {} };
      },
    });
    const sync = new SyncManager(c, config());
    await sync.ensureSession("pi-session");
    sync.restoreWatermark(1);

    const result = await sync.syncBranch([
      { type: "message", message: { role: "user", content: "Already captured entry should be skipped." } },
      { type: "message", message: { role: "user", content: "Fresh entry should be captured now." } },
    ]);

    assert.equal(result.added, 1);
    assert.equal(sent.length, 1);
    assert.match(sent[0].parts[0].text, /Fresh entry/);
  });
});
