import {
  DEFAULT_TOOL_HINTS,
  addSkillExample,
  evaluateUriGuard,
  evaluateUriNotice,
  isSkillUri,
  normalizeToolName,
} from "../shared/uri-guard.mjs";

// omp's builtin tools mapped to the OpenViking MCP tools the server exposes.
//
// The catalogue reaches omp through `<omp agent dir>/mcp.json` (the `openviking`
// entry install.sh writes), and omp names bridged tools `mcp__<server>_<tool>`
// — so the names below are what the model actually has to call, and the server
// owns their schemas: `read` takes a `uris` array, `grep` takes `uri` plus a
// `pattern` array, `glob` takes a pattern plus an optional `uri`, and
// `write`/`edit` address content by `uri`. Tools absent from this table are
// never guarded, which is why the `mcp__openviking_*` tools need no allowlist.
//
// `DEFAULT_TOOL_HINTS` is the base so nothing here silently loses a branch the
// shared table already got right — notably write and edit, which point at
// `add_skill` for skill URIs because write and edit refuse that subtree.
const VIKING_URI_TOOL_HINTS = {
  ...DEFAULT_TOOL_HINTS,
  read: {
    tool: "mcp__openviking_read",
    example: (uri) => `mcp__openviking_read(uris=["${uri}"])`,
  },
  grep: {
    tool: "mcp__openviking_grep",
    example: (uri, input = {}) => `mcp__openviking_grep(uri="${uri}", pattern=["${String(input.pattern ?? "").replaceAll('"', '\\"')}"])`,
  },
  find: {
    tool: "mcp__openviking_glob",
    example: (uri) => `mcp__openviking_glob(uri="${uri}", pattern="**/*")`,
  },
  ls: {
    tool: "mcp__openviking_list",
    example: (uri) => `mcp__openviking_list(uri="${uri}")`,
  },
  write: {
    tool: (uri) => (isSkillUri(uri) ? "mcp__openviking_add_skill" : "mcp__openviking_write"),
    example: (uri) => (isSkillUri(uri)
      ? addSkillExample(uri, { call: "mcp__openviking_add_skill" })
      : `mcp__openviking_write(uri="${uri}", content="...")`),
  },
  edit: {
    tool: (uri) => (isSkillUri(uri) ? "mcp__openviking_add_skill" : "mcp__openviking_edit"),
    example: (uri) => (isSkillUri(uri)
      ? addSkillExample(uri, { call: "mcp__openviking_add_skill", edited: true })
      : `mcp__openviking_edit(uri="${uri}", old_string="...", new_string="...")`),
  },
  bash: {
    tool: "mcp__openviking_read or mcp__openviking_search",
    example: (uri) => `mcp__openviking_read(uris=["${uri}"])`,
  },
};

// The only keys the guard may see for a given tool. omp's edit takes
// `{ filePath, edits: [{ oldText, newText }] }` (the binary carries `filePath`
// far more often than `path`), and `oldText`/`newText` are not in the shared
// guard's content-key allowlist. Handed the whole input, its generic sweep
// reads them as locations and blocks any edit whose replacement text merely
// mentions a viking:// URI — which fires the moment somebody edits this repo's
// own docs. Only the path keys say where the edit lands, so only they get
// through.
const GUARD_INPUT_KEYS_BY_TOOL = { edit: ["filePath", "path"] };

function narrowGuardInput(toolName, input) {
  const keys = GUARD_INPUT_KEYS_BY_TOOL[normalizeToolName(toolName)];
  if (!keys) return input;
  if (!input || typeof input !== "object") return {};
  return Object.fromEntries(keys.filter((key) => input[key] !== undefined).map((key) => [key, input[key]]));
}

function readToolEvent(event) {
  const toolName = event?.toolName ?? event?.tool_name ?? event?.name;
  return {
    toolName,
    input: narrowGuardInput(toolName, event?.input ?? event?.args ?? event?.params ?? {}),
  };
}

export function guardVikingUriToolCall(event) {
  const { toolName, input } = readToolEvent(event);
  const decision = evaluateUriGuard(toolName, input, { hints: VIKING_URI_TOOL_HINTS });
  return decision ? { block: true, reason: decision.reason } : null;
}

// A tool_result handler's content replaces the result's content, so the
// original blocks are carried over and the notice is appended after them.
export function noticeVikingUriToolResult(event) {
  const { toolName, input } = readToolEvent(event);
  const notice = evaluateUriNotice(toolName, input, { hints: VIKING_URI_TOOL_HINTS });
  if (!notice) return null;
  const content = Array.isArray(event?.content) ? event.content : [];
  return { content: [...content, { type: "text", text: notice.reason }] };
}
