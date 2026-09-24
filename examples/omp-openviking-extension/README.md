# OpenViking Memory Extension for omp (oh-my-pi)

Long-term semantic memory and context takeover for [omp](https://github.com/can1357/omp) (oh-my-pi, a fork of [pi](https://github.com/earendil-works/pi)) sessions, powered by [OpenViking](https://github.com/volcengine/OpenViking). Recall happens automatically before every prompt, capture happens after every turn, and OpenViking can own long-term context by replacing committed history with an archive overview in omp's `context` hook.

> **Requires an OpenViking server with `viking://~` home-alias support.** Recall targets the
> caller's own context space through `viking://~/memories` and `viking://~/skills`; the uid-less
> `viking://user/memories` shorthand is rejected by newer servers.

> Design informed by lessons from all three OpenViking agent plugins: synchronous recall from OpenClaw, production-hardened capture/ranking from Claude Code, and anti-patterns dodged from Hermes's stale prefetch approach. See [DESIGN.md](./DESIGN.md) for the base design and [TAKEOVER.md](./TAKEOVER.md) for the context-takeover layer.

## Quick Start

### Prerequisites

- **omp (oh-my-pi) installed** (pi fork; extension auto-discovered from `~/.omp/agent/extensions/`)
- **Node.js 18+** (for the extension's TypeScript runtime)
- **An OpenViking server** reachable — local or remote — and **kept running**: the tools live behind the MCP proxy, so a stopped server means they are all missing

### 1. Have an OpenViking server reachable

Either run one locally or point at a remote one. The [quickstart guide](../../docs/en/getting-started/02-quickstart.md) walks through both options. Default port is `1933`; local mode runs without authentication. The server must stay running — the extension's tools are served through it, and omp shows a warning when it cannot be reached.

Verify it's up:

```bash
curl http://localhost:1933/health   # or your remote URL
```

### 2. Install the extension

Use the shared installer:

```bash
bash examples/memory-plugin-shared/install.sh --harness omp
```

The installer copies the extension to `~/.omp/agent/extensions/openviking` (profile-scoped installs land in `~/.omp/profiles/<profile>/agent/extensions/openviking`). The extension loads on next `omp` invocation.

It also registers the tool server: an `openviking` entry is written into the `mcp.json` beside that directory, pointing at `servers/mcp-proxy.mjs`. No credentials go in that file — the proxy reads `ovcli.conf` / `ov.conf` when omp starts it. To undo both halves:

```bash
bash examples/memory-plugin-shared/install.sh --harness omp --uninstall
```

### 3. Configure (optional)

Credentials are resolved from `OPENVIKING_*` environment variables, `~/.openviking/ovcli.conf`, then `~/.openviking/ov.conf`. Run the setup wizard when you need to configure a remote server:

```bash
node ~/.omp/agent/extensions/openviking/scripts/setup.mjs
```

`~/.omp/agent/extensions/openviking/config.json` is for behavior knobs only:

```json
{
  "enabled": true,
  "syncTurns": true,
  "recallTokenBudget": 2000,
  "scoreThreshold": 0.35,
  "minQueryLength": 3,
  "profileTokenBudget": 10000,
  "resumeContextBudget": 32000,
  "commitTokenThreshold": 20000,
  "takeover": {
    "enabled": true,
    "tokenThreshold": 30000,
    "keepRecentTurns": 3,
    "overviewBudget": 3000,
    "overviewPollMs": 2000,
    "overviewPollMax": 15
  }
}
```

Credential environment variables:

| Env Var | Meaning |
|---------|---------|
| `OPENVIKING_URL` | OpenViking server URL |
| `OPENVIKING_API_KEY` / `OPENVIKING_BEARER_TOKEN` | Bearer token |
| `OPENVIKING_ACCOUNT` | Trusted-mode account |
| `OPENVIKING_USER` | Trusted-mode user |
| `OPENVIKING_PEER_ID` | Actor peer id |
| `OPENVIKING_WORKSPACE_PEER` | Derive an actor peer from the current workspace by default; set `0` to disable |
| `OPENVIKING_RECALL_PEER_SCOPE` | `all` recalls other project memories with a score penalty; `actor` only sees global plus the current project |

Recall asks the server to assemble the context block in one request
(`POST /api/v1/search/search` with `mode="context"`), so token budgeting, detail
tiers and cross-turn dedup are shared with every other harness. Deployments
without that endpoint fall back to `/api/v1/search/recall`, and that outcome is
cached so only the first turn pays for the probe.

API keys are sent as `Authorization: Bearer ...`. By default the extension derives a peer from the process workspace path using Claude's project-directory naming rule: every non-letter-or-digit character becomes `-`, with no path normalization. For example, `/Users/x/Dev/OpenViking` becomes `-Users-x-Dev-OpenViking`. The effective peer is sent as `X-OpenViking-Actor-Peer` and stored as `peer_id` on captured session messages. `OPENVIKING_PEER_ID` overrides the workspace-derived value.

Recall defaults to the broad mode: global memory, the current workspace, and other workspace memories can all be recalled, with other workspaces penalized and rendered later. Set `OPENVIKING_RECALL_PEER_SCOPE=actor` for the isolation mode, which only sees global memory plus the current workspace. In deployments where one bot serves multiple real people, such as zouk, vikingbot, or AstrBot, use the isolation mode with an explicit actor peer so one person's memories are not recalled into another person's session.

### 4. Start omp

```bash
omp
```

The extension shows an `[OpenViking]` status line on startup. Memories persist across sessions — no additional setup. The tools themselves belong to the MCP server the installer registered, not to the extension: omp starts `servers/mcp-proxy.mjs` from `mcp.json`, and the system-prompt line this extension adds is what tells the model those `mcp__openviking_*` names exist.

## Configuration Reference

### Tuning fields

All fields below live in `config.json`. Defaults are shown.

| Field                    | Default    | Description                                                              |
|--------------------------|------------|--------------------------------------------------------------------------|
| `enabled`                | `true`     | Set `false` to disable the extension entirely                            |
| `syncTurns`              | `true`     | Enable auto-capture of conversation turns                                |

### Recall tuning

| Field                    | Default    | Description                                                              |
|--------------------------|------------|--------------------------------------------------------------------------|
| `recallTokenBudget`      | `2000`     | Token budget for inline recall content                                   |
| `recallMaxContentChars`  | `500`      | Per-item content cap for search results                                  |
| `recallPreferAbstract`   | `true`     | Prefer L0 abstract over L2 full body when available                      |
| `recallLimit`            | `10`       | Legacy quota-scaling input converted to six coding quotas, not a final cap |
| `scoreThreshold`         | `0.35`     | Min relevance score (0–1)                                                |
| `minQueryLength`         | `3`        | Skip recall for queries shorter than N characters                        |
| `recallLedger`           | `true`     | Persist injected blocks and re-apply them to historical user messages so provider prompt-prefix caches keep hitting |

### Recall injection ledger

Pi's `context` hook hands extensions a deep copy of the session messages, so
an injected `<openviking-context>` block is never written back to session
storage. Without compensation, every request's history diverges from what the
provider saw last turn, and strict-prefix prompt caches (DeepSeek and other
OpenAI-compatible providers) miss from the first injected message onward
(#4137). The ledger records exactly which block was injected into which user
message (keyed by stable Pi entry id + content hash) in
`~/.openviking/pi-recall-ledger/<session>.json` and re-applies them on every
request, keeping the prefix byte-identical across turns while the newest
message still gets fresh, current-query recall. Disable with
`"recallLedger": false` or `OPENVIKING_RECALL_LEDGER=0`. Losing the ledger
file only costs one cache miss; alignment resumes on the next turn. Stable
entry ids let compacted retained messages and `/tree` branches recover their
own original blocks even when active-context positions change.

Explicit `recallLimit` values from 1 through 5 produce an effective total
quota of 6 because each coding category keeps one retrieval slot. Direct API
integrations should configure category `quotas` when they need exact ceilings.

### Capture tuning

| Field                    | Default    | Description                                                              |
|--------------------------|------------|--------------------------------------------------------------------------|
| `captureMode`            | `"semantic"` | `"semantic"` (always capture) or `"keyword"` (trigger-based)           |
| `captureMaxLength`       | `24000`    | Max sanitized text length for the capture decision                       |
| `captureAssistantTurns`  | `true`     | Include assistant turns (text + tool USE inputs)                         |
| `captureToolResults`     | `false`    | Include tool result output (noisy — off by default)                      |
| `captureToolMaxChars`    | `1000000`  | Guard cap on one tool part's `tool_output`; the server externalizes oversized output |
| `commitTokenThreshold`   | `20000`    | Pending-token threshold for client-driven commit                         |
| `commitKeepRecentCount`  | `10`       | Live tail kept after commit                                              |

### Context takeover

Takeover is enabled by default. OpenViking commits archived history, polls the
session overview, then the `context` hook replaces covered conversation turns
with a synthetic `[OpenViking Session Context]` user message while keeping the
recent live tail.

| Field                    | Default    | Description                                                              |
|--------------------------|------------|--------------------------------------------------------------------------|
| `takeover.enabled`       | `true`     | Let OpenViking own long-term context through the `context` hook           |
| `takeover.tokenThreshold`| `30000`    | Synced-token pressure that triggers commit and boundary advance           |
| `takeover.keepRecentTurns`| `3`       | Recent user turns retained in full fidelity                              |
| `takeover.overviewBudget`| `3000`    | Token budget for the injected archive overview                           |
| `takeover.overviewPollMs`| `2000`    | Delay between overview polling attempts after commit                     |
| `takeover.overviewPollMax`| `15`     | Max overview polling attempts before fail-open                           |

### Injection tuning

| Field                    | Default    | Description                                                              |
|--------------------------|------------|--------------------------------------------------------------------------|
| `profileTokenBudget`     | `10000`    | Token budget for user profile block                                      |
| `resumeContextBudget`    | `32000`    | Token budget for archive overview on session resume                      |

### Misc

| Field                    | Default    | Description                                                              |
|--------------------------|------------|--------------------------------------------------------------------------|
| `bypassPatterns`         | `[]`       | Glob patterns to skip extension processing                               |
| `logLevel`               | `"error"`  | `"silent"`, `"error"`, or `"info"`                                      |
| `mcpEnabled`             | `true`     | Add the system-prompt line naming the MCP tools. Off hides the pointer only — the server still starts if `mcp.json` lists it |

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    omp (oh-my-pi)                    │
│                                                      │
│  session_start  before_agent_start  context  turn_end│
│  session_before_compact  session_shutdown            │
└────────┬──────────────────┬───────────┬──────────────┘
         │                  │           │
         │  ┌───────────────▼───────────▼──────┐
         │  │   extension modules (.ts)        │──────►  OpenViking
         │  │   client / sync / recall /       │        Server
         │  │   takeover / config              │        (HTTP API)
         │  └──────────────────────────────────┘
         │  ┌──────────────────────────────────┐
         └──►   mcp__openviking_* tools        │
            │   started by omp from mcp.json   │
            └──────────────────────────────────┘
└──────────────────────────────────────────────────────┘
```

The extension is a single directory of TypeScript files loaded by pi's `jiti` transpiler — no build step, no npm dependencies, no MCP server. All communication goes over HTTP to the OpenViking REST API.

### Event Flow

| Pi Event               | Extension Action                                                                 |
|------------------------|----------------------------------------------------------------------------------|
| `session_start`        | Health check → derive OV session → build profile context → restore takeover state |
| `before_agent_start`   | Idempotent startup for `omp -c` + queue the current prompt for recall             |
| `context`              | Run current-prompt recall after UI rendering, then inject takeover and recall context |
| `turn_end`             | Extract branch entries → write or pending-queue OV messages → maybe advance boundary |
| `session_before_compact`| Takeover mode returns OV overview as pi compaction summary; otherwise commits pending messages |
| `session_shutdown`     | Persist takeover state or final non-takeover commit                              |

### Recall: Synchronous, Not Stale

Unlike Hermes's stale prefetch (recall from previous turn's query, injected one turn late), this extension searches OpenViking with the **current** user prompt via pi's `context` event. Pi renders the submitted user message before this hook, so recall latency does not hold the message off-screen. Results are still injected into the same model turn as `<openviking-context>` blocks. This means:

- **First turn** of a session gets relevant context immediately
- **Topic switches** within a session get correct recall
- No waiting for the next turn to see relevant memories

### Memory Pollution Prevention

Before pushing turns to OpenViking, shared capture sanitization strips injected context blocks such as `<openviking-context>` to prevent a self-referential pollution loop where recall context is captured back as user messages.

In takeover mode the adapter uses faithful capture: acknowledgments and short
turns are retained because they may later be represented only through the OV
archive overview. Empty text, slash commands, and OpenViking status messages
remain filtered.

### Tool Use Preservation

Tool capture preserves structured tool parts with bounded inputs and outputs. The memory extractor sees what the agent did without indexing unbounded raw output.

## LLM Tools

The extension registers no tools of its own. The server's catalogue reaches omp over MCP — as `mcp__openviking_*` — because omp starts the proxy named in `mcp.json`:

| Tool | Description |
|------|-------------|
| `mcp__openviking_find` / `_search` | Semantic retrieval; `search` carries session context, `find` does not |
| `mcp__openviking_read` / `_write` / `_edit` | Content by `viking://` URI, with the server owning the schemas |
| `mcp__openviking_list` / `_tree` | Browse a directory, or a whole subtree at once |
| `mcp__openviking_glob` / `_grep` | Find files by pattern; search file contents |
| `mcp__openviking_remember` / `_forget` | Long-term memory: store a fact, delete by URI |
| `mcp__openviking_add_resource` / `_add_skill` | Ingest a URL or document; install a skill |
| `mcp__openviking_list_watches` / `_cancel_watch` | Auto-refresh subscriptions |
| `mcp__openviking_health` | Server reachability |

They load on demand rather than into every prompt. `read xd://` in a session lists what is available, and omp's `tools.xdevDocs` default (`"builtins"`) is why this extension adds one prompt line naming them. To pin a hot one to the top level, name it in `tools.xdevInlineDevices` in omp's settings — `"mcp__openviking_search"` — the same switch that works for any MCP tool.

`viking://` URIs passed to omp's builtin `read`/`write`/`edit`/`grep`/`glob` are redirected to the matching MCP tool, and a call that fails while OV is down explains the guard.

The old `viking_*` names are gone. Where each one went:

| Was | Now |
|-----|-----|
| `viking_search` | `mcp__openviking_search` — or `mcp__openviking_find` for a session-free lookup |
| `viking_read` | `mcp__openviking_read` |
| `viking_browse` | `mcp__openviking_list` / `mcp__openviking_tree` |
| `viking_remember` | `mcp__openviking_remember` |
| `viking_forget` | `mcp__openviking_forget` |
| `viking_add_resource` | `mcp__openviking_add_resource` |
| `viking_archive_expand` | `mcp__openviking_read` on the archive URI |

The canonical `/viking` command (type `/viking` in pi's chat) displays connection status, session info, and accepts `commit` for manual synchronous commit.

## Compared to Pi's Built-in Memory

Pi has a built-in `MEMORY.md` file system. This extension **complements** it:

| Feature      | Built-in `MEMORY.md`              | OpenViking extension                              |
|--------------|-----------------------------------|---------------------------------------------------|
| Storage      | Flat markdown                     | Vector DB + structured extraction                 |
| Search       | Loaded into context wholesale     | Semantic similarity + ranking + token budget      |
| Scope        | Per-project                       | Cross-project, cross-session, cross-agent         |
| Capacity     | Context-limited                    | Unlimited (server-side storage)                   |
| Extraction   | Manual rules                      | LLM-powered entity / preference / event extraction|
| Subagents    | Same as parent                    | Isolated session + typed agent namespace          |

## Compared to Claude Code Plugin

Both plugins share the same core design (informed by each other):

| Feature             | Claude Code Plugin                     | Pi Extension                           |
|---------------------|----------------------------------------|----------------------------------------|
| Architecture        | Hook scripts (.mjs) + MCP delegation   | Native TypeScript extension            |
| Recall timing       | Synchronous (UserPromptSubmit hook)     | Synchronous (context event)            |
| Tool delivery       | OV server's MCP endpoint (16 tools)     | pi.registerTool() (7 tools)            |
| Write path          | Detached worker (async)                 | Async promise (pi's event loop)        |
| Installation        | `claude plugin install` + setup script  | Copy directory → auto-discovered       |
| Memory index        | None (flashlight search model)          | Built (map model — model sees what OV knows) |
| Subagent isolation  | Explicit hook management                | Natural process-level isolation        |

## Extension Structure

See [DESIGN.md](./DESIGN.md) for the full design specification — comparison of all three OV plugins, detailed event flow, design rationale, and implementation guidance useful for building OV extensions for any agent harness.

```
omp-openviking-extension/
├── config.json          # Default configuration (edit to customize)
├── config.ts            # Config loader (defaults + config.json merge)
├── client.ts            # OpenViking HTTP client (fetch + response envelope)
├── sync.ts              # Turn capture, write queue, session lifecycle
├── recall.ts            # Synchronous recall with ranking + budget
├── takeover.ts          # Thin omp binding around lib/takeover-core.mjs
├── index.ts             # Extension entry point (events, /viking command, prompt blocks)
├── lib/                 # Adapters + pure state machines (guard, capture, takeover, MCP state)
├── servers/mcp-proxy.mjs # The MCP server omp starts (stdio → HTTP, vendored by sync.mjs)
├── shared/              # Vendored copies of examples/memory-plugin-shared
├── TAKEOVER.md          # Context-takeover design
└── README.md
```

All TypeScript files are loaded directly by omp's built-in `jiti` transpiler — zero dependencies beyond Node.js, and the vendored `shared/` modules are plain ESM.

## Troubleshooting

| Symptom                                 | Cause                                                | Fix                                                         |
|-----------------------------------------|------------------------------------------------------|-------------------------------------------------------------|
| Extension not loading                   | `enabled: false` in config.json                      | Set `"enabled": true`                                       |
| No recall on first prompt               | OpenViking server not running or wrong URL           | `curl http://localhost:1933/health`                         |
| MCP tools missing from `xd://`          | No enabled `openviking` entry in `<agent dir>/mcp.json` | Re-run the installer, or add the server from `omp` with `/mcp` |
| System prompt never names the tools     | `mcpEnabled: false`, or the entry exists but is disabled | Check both — the extension only advertises what `mcp.json` declares |
| Extension crashes on load               | Wrong OV server URL or network issue                 | Check `logLevel` and server accessibility                   |
| No memories extracted                   | Wrong embedding/extraction model in OV config        | Check OV's `embedding` / `vlm` configuration                |
| Takeover never advances                  | Pending addMessage replay, commit, or overview polling failed | Set `OV_DEBUG_LOG=/tmp/ov-pi.log` and retry `/viking commit` |

## License

Apache-2.0 — same as [OpenViking](https://github.com/volcengine/OpenViking).
