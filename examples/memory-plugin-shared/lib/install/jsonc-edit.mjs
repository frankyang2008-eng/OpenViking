#!/usr/bin/env node
/**
 * Comment-preserving edits to a host's JSON/JSONC config file.
 *
 * OpenCode reads `opencode.jsonc`, and people keep notes in it. Reparsing and
 * reserializing the file would silently eat every comment and every hand-made
 * formatting choice, so the value is parsed only to decide what to change and
 * the change itself is spliced into the original text. That means walking JSONC
 * by hand: comments, trailing commas and single-quoted strings are all things
 * `JSON.parse` refuses and the splice has to step over.
 *
 * Installer-only, and deliberately outside the runtime closure `sync.mjs`
 * vendors into the plugins: no hook imports it, so no plugin ships it.
 */

import fs from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

function stripJsonc(s) {
  let out = "";
  let i = 0;
  while (i < s.length) {
    const ch = s[i];
    const next = s[i + 1];
    if (ch === '"' || ch === "'") {
      const end = readStringEnd(s, i);
      out += s.slice(i, end);
      i = end;
    } else if (ch === "/" && next === "/") {
      i += 2;
      while (i < s.length && s[i] !== "\n") i++;
    } else if (ch === "/" && next === "*") {
      i += 2;
      while (i < s.length && !(s[i] === "*" && s[i + 1] === "/")) i++;
      i = Math.min(s.length, i + 2);
    } else {
      out += ch;
      i++;
    }
  }
  return out.replace(/,\s*([}\]])/g, "$1");
}

function readStringEnd(s, start) {
  const quote = s[start];
  let i = start + 1;
  while (i < s.length) {
    if (s[i] === "\\") {
      i += 2;
    } else if (s[i] === quote) {
      return i + 1;
    } else {
      i++;
    }
  }
  return s.length;
}

function skipTrivia(s, i, end = s.length) {
  while (i < end) {
    if (/\s/.test(s[i])) {
      i++;
    } else if (s[i] === "/" && s[i + 1] === "/") {
      i += 2;
      while (i < end && s[i] !== "\n") i++;
    } else if (s[i] === "/" && s[i + 1] === "*") {
      i += 2;
      while (i < end && !(s[i] === "*" && s[i + 1] === "/")) i++;
      i = Math.min(end, i + 2);
    } else {
      break;
    }
  }
  return i;
}

function parseStringLiteral(s, start) {
  const end = readStringEnd(s, start);
  try {
    return { value: JSON.parse(s.slice(start, end)), end };
  } catch {
    return { value: "", end };
  }
}

function findTopLevelObject(s) {
  const start = skipTrivia(s, 0);
  if (s[start] !== "{") return null;
  let depth = 0;
  let i = start;
  while (i < s.length) {
    if (s[i] === '"' || s[i] === "'") {
      i = readStringEnd(s, i);
      continue;
    }
    if (s[i] === "/" && (s[i + 1] === "/" || s[i + 1] === "*")) {
      i = skipTrivia(s, i);
      continue;
    }
    if (s[i] === "{" || s[i] === "[") depth++;
    if (s[i] === "}" || s[i] === "]") {
      depth--;
      if (depth === 0 && s[i] === "}") return { start, end: i };
    }
    i++;
  }
  return null;
}

function findObjectRangeAt(s, start, end = s.length) {
  const objectStart = skipTrivia(s, start, end);
  if (s[objectStart] !== "{") return null;
  let depth = 0;
  let i = objectStart;
  while (i < end) {
    if (s[i] === '"' || s[i] === "'") {
      i = readStringEnd(s, i);
      continue;
    }
    if (s[i] === "/" && (s[i + 1] === "/" || s[i + 1] === "*")) {
      i = skipTrivia(s, i, end);
      continue;
    }
    if (s[i] === "{" || s[i] === "[") depth++;
    if (s[i] === "}" || s[i] === "]") {
      depth--;
      if (depth === 0 && s[i] === "}") return { start: objectStart, end: i };
    }
    i++;
  }
  return null;
}

function findArrayRangeAt(s, start, end = s.length) {
  const arrayStart = skipTrivia(s, start, end);
  if (s[arrayStart] !== "[") return null;
  let depth = 0;
  let i = arrayStart;
  while (i < end) {
    if (s[i] === '"' || s[i] === "'") {
      i = readStringEnd(s, i);
      continue;
    }
    if (s[i] === "/" && (s[i + 1] === "/" || s[i + 1] === "*")) {
      i = skipTrivia(s, i, end);
      continue;
    }
    if (s[i] === "{" || s[i] === "[") depth++;
    if (s[i] === "}" || s[i] === "]") {
      depth--;
      if (depth === 0 && s[i] === "]") return { start: arrayStart, end: i };
    }
    i++;
  }
  return null;
}

function findTopLevelProperty(s, objectRange, name) {
  let depth = 1;
  let i = objectRange.start + 1;
  while (i < objectRange.end) {
    if (s[i] === "/" && (s[i + 1] === "/" || s[i + 1] === "*")) {
      i = skipTrivia(s, i, objectRange.end);
      continue;
    }
    if (s[i] === '"' || s[i] === "'") {
      const keyStart = i;
      const parsed = parseStringLiteral(s, i);
      i = parsed.end;
      const afterKey = skipTrivia(s, i, objectRange.end);
      if (depth === 1 && parsed.value === name && s[afterKey] === ":") {
        const valueStart = skipTrivia(s, afterKey + 1, objectRange.end);
        return {
          keyStart,
          valueStart,
          replaceEnd: findPropertyReplaceEnd(s, valueStart, objectRange.end),
        };
      }
      continue;
    }
    if (s[i] === "{" || s[i] === "[") depth++;
    if (s[i] === "}" || s[i] === "]") depth--;
    i++;
  }
  return null;
}

function findPropertyReplaceEnd(s, valueStart, objectEnd) {
  let depth = 0;
  let i = skipTrivia(s, valueStart, objectEnd);
  let lastTokenEnd = i;
  while (i < objectEnd) {
    if (s[i] === '"' || s[i] === "'") {
      i = readStringEnd(s, i);
      lastTokenEnd = i;
      continue;
    }
    if (s[i] === "/" && (s[i + 1] === "/" || s[i + 1] === "*")) {
      i = skipTrivia(s, i, objectEnd);
      continue;
    }
    if (depth === 0 && s[i] === ",") return lastTokenEnd;
    if (s[i] === "{" || s[i] === "[") depth++;
    if (s[i] === "}" || s[i] === "]") depth--;
    if (!/\s/.test(s[i])) lastTokenEnd = i + 1;
    i++;
  }
  return lastTokenEnd;
}

function findLineIndent(s, index) {
  const lineStart = s.lastIndexOf("\n", index - 1) + 1;
  const prefix = s.slice(lineStart, index);
  return /^[ \t]*$/.test(prefix) ? prefix : "";
}

function detectPropertyIndent(s, objectRange) {
  let i = objectRange.start + 1;
  while (i < objectRange.end) {
    i = skipTrivia(s, i, objectRange.end);
    if (s[i] === '"' || s[i] === "'") return findLineIndent(s, i) || "  ";
    if (s[i] === "{" || s[i] === "[") break;
    i++;
  }
  const closeIndent = findLineIndent(s, objectRange.end);
  return `${closeIndent}  `;
}

function hasTopLevelProperty(s, objectRange) {
  let i = objectRange.start + 1;
  while (i < objectRange.end) {
    i = skipTrivia(s, i, objectRange.end);
    if (s[i] === '"' || s[i] === "'") return true;
    i++;
  }
  return false;
}

function objectEndsWithComma(s, objectRange) {
  const body = s.slice(objectRange.start + 1, objectRange.end);
  const end = endOfLastToken(body);
  return end > 0 && body[end - 1] === ",";
}

/** The index just past the last character that is neither whitespace nor part
 *  of a comment. `String.prototype.trimEnd` only strips whitespace, so an
 *  anchor computed from it lands inside a trailing `// note` and any comma
 *  inserted there dies with the comment when it is stripped. */
function endOfLastToken(body) {
  let inString = false;
  let quote = "";
  let inLineComment = false;
  let inBlockComment = false;
  let last = -1;
  for (let i = 0; i < body.length; i++) {
    const ch = body[i];
    if (inLineComment) {
      if (ch === "\n") inLineComment = false;
    } else if (inBlockComment) {
      if (ch === "*" && body[i + 1] === "/") { inBlockComment = false; i++; }
    } else if (inString) {
      if (ch === "\\") i++;
      else if (ch === quote) inString = false;
      last = i;
    } else if (ch === '"' || ch === "'") {
      inString = true;
      quote = ch;
      last = i;
    } else if (ch === "/" && body[i + 1] === "/") {
      inLineComment = true;
    } else if (ch === "/" && body[i + 1] === "*") {
      inBlockComment = true;
    } else if (!/\s/.test(ch)) {
      last = i;
    }
  }
  return last + 1;
}

function rangeHasValue(s, range) {
  let i = range.start + 1;
  while (i < range.end) {
    i = skipTrivia(s, i, range.end);
    if (i < range.end) return true;
  }
  return false;
}

function formatProperty(name, value, indent) {
  const json = JSON.stringify(value, null, 2);
  const formatted = json.split("\n").map((line, idx) => idx === 0 ? line : `${indent}${line}`).join("\n");
  return `${JSON.stringify(name)}: ${formatted}`;
}

function setPropertyInObject(s, objectRange, name, value) {
  const existing = findTopLevelProperty(s, objectRange, name);
  if (existing) {
    const indent = findLineIndent(s, existing.keyStart) || detectPropertyIndent(s, objectRange);
    return `${s.slice(0, existing.keyStart)}${formatProperty(name, value, indent)}${s.slice(existing.replaceEnd)}`;
  }

  const indent = detectPropertyIndent(s, objectRange);
  const closeIndent = findLineIndent(s, objectRange.end);
  const needsComma = hasTopLevelProperty(s, objectRange) && !objectEndsWithComma(s, objectRange);
  const prefix = needsComma ? "," : "";
  // Anchored after the last member rather than at the closing brace, so the comma
  // lands at the end of that member's line instead of on a line of its own. The
  // splice replaces everything from the anchor to the brace, so a comment that
  // trails the member has to be re-emitted: it rides along between the comma
  // and the new member, and pure whitespace is left to the `insertion`.
  const body = s.slice(objectRange.start + 1, objectRange.end);
  const anchor = endOfLastToken(body);
  const trailingComment = body.slice(anchor).trimEnd();
  const separator = trailingComment ? `${trailingComment}\n` : "\n";
  const insertion = `${prefix}${separator}${indent}${formatProperty(name, value, indent)}\n${closeIndent}`;
  const insertAt = objectRange.start + 1 + anchor;
  return `${s.slice(0, insertAt)}${insertion}${s.slice(objectRange.end)}`;
}

function setNestedObjectProperty(s, parentName, childName, childValue, fallbackParentValue) {
  let objectRange = findTopLevelObject(s);
  if (!objectRange) {
    s = "{\n}\n";
    objectRange = findTopLevelObject(s);
  }
  const parent = findTopLevelProperty(s, objectRange, parentName);
  if (!parent) return setPropertyInObject(s, objectRange, parentName, fallbackParentValue);
  const parentRange = findObjectRangeAt(s, parent.valueStart, parent.replaceEnd);
  if (!parentRange) return setPropertyInObject(s, objectRange, parentName, fallbackParentValue);
  return setPropertyInObject(s, parentRange, childName, childValue);
}

function appendStringToTopLevelArray(s, name, value) {
  let objectRange = findTopLevelObject(s);
  if (!objectRange) {
    s = "{\n}\n";
    objectRange = findTopLevelObject(s);
  }
  const prop = findTopLevelProperty(s, objectRange, name);
  if (!prop) return setPropertyInObject(s, objectRange, name, [value]);
  const arrayRange = findArrayRangeAt(s, prop.valueStart, prop.replaceEnd);
  if (!arrayRange) return setPropertyInObject(s, objectRange, name, [value]);
  const propIndent = findLineIndent(s, prop.keyStart) || detectPropertyIndent(s, objectRange);
  const itemIndent = `${propIndent}  `;
  const closeIndent = findLineIndent(s, arrayRange.end) || propIndent;
  const needsComma = rangeHasValue(s, arrayRange) && !objectEndsWithComma(s, arrayRange);
  const prefix = needsComma ? "," : "";
  // Same anchor and trailing-comment bargain as `setPropertyInObject`.
  const body = s.slice(arrayRange.start + 1, arrayRange.end);
  const anchor = endOfLastToken(body);
  const trailingComment = body.slice(anchor).trimEnd();
  const separator = trailingComment ? `${trailingComment}\n` : "\n";
  const insertion = `${prefix}${separator}${itemIndent}${JSON.stringify(value)}\n${closeIndent}`;
  const insertAt = arrayRange.start + 1 + anchor;
  return `${s.slice(0, insertAt)}${insertion}${s.slice(arrayRange.end)}`;
}

/** The config text with the plugin registered and the MCP fallback pointed at `mcpProxy`. */
export function updateOpencodeConfig(raw, { pluginSpec = "", mcpProxy = "" } = {}) {
  let data = {};
  try { data = raw.trim() ? JSON.parse(stripJsonc(raw)) : {}; } catch { data = {}; }
  let nextRaw = raw.trim() ? raw : "{\n}\n";
  if (pluginSpec) {
    const next = Array.isArray(data.plugin) ? data.plugin.slice() : [];
    if (!next.includes(pluginSpec)) {
      next.push(pluginSpec);
      nextRaw = appendStringToTopLevelArray(nextRaw, "plugin", pluginSpec);
    }
    data.plugin = next;
  }
  if (mcpProxy) {
    data.mcp = data.mcp && typeof data.mcp === "object" && !Array.isArray(data.mcp) ? data.mcp : {};
    if (!data.mcp.openviking || data.mcp.openviking.enabled !== false) {
      data.mcp.openviking = {
        type: "local",
        command: ["node", mcpProxy],
        enabled: true,
        timeout: 15000,
      };
      nextRaw = setNestedObjectProperty(nextRaw, "mcp", "openviking", data.mcp.openviking, data.mcp);
    }
  }
  if (!nextRaw.endsWith("\n")) nextRaw += "\n";
  return nextRaw;
}

export { stripJsonc };

/**
 * The omp `mcp.json` text with the OpenViking server pointing at `mcpProxy`.
 *
 * omp's schema is not OpenCode's: servers live in a top-level `mcpServers` map
 * and the entry is a stdio server with a bare `command` string. Its default
 * timeout is not ours either — omp applies 30s to an entry that names none, so
 * none is written here, and an explicit `timeout` survives. A server the user
 * disabled stays disabled, and keys this installer does not own (`env`, `cwd`)
 * are carried over rather than dropped.
 */
export function updateOmpMcpConfig(raw, { mcpProxy = "" } = {}) {
  let data = {};
  try { data = raw.trim() ? JSON.parse(stripJsonc(raw)) : {}; } catch { data = {}; }
  let nextRaw = raw.trim() ? raw : "{\n}\n";
  if (mcpProxy) {
    const servers = data.mcpServers && typeof data.mcpServers === "object" && !Array.isArray(data.mcpServers) ? data.mcpServers : {};
    const entry = servers.openviking;
    const previous = entry && typeof entry === "object" && !Array.isArray(entry) ? entry : {};
    if (previous.enabled !== false) {
      servers.openviking = {
        ...previous,
        type: "stdio",
        command: "node",
        args: [mcpProxy],
        enabled: true,
      };
      nextRaw = setNestedObjectProperty(nextRaw, "mcpServers", "openviking", servers.openviking, servers);
    }
  }
  if (!nextRaw.endsWith("\n")) nextRaw += "\n";
  return nextRaw;
}

/**
 * The omp `mcp.json` text with our server entry removed, or the input unchanged.
 *
 * A parse and reserialize rather than a splice: this file is JSON this
 * installer wrote, and a delete that leaves a dangling comma behind is worse
 * than one that loses a comment. The entry is only ours when its own text names
 * the extension directory being uninstalled, so a user who pointed the name at
 * a server of their own keeps it.
 */
export function removeOmpMcpEntry(raw, { extensionDir = "" } = {}) {
  let data;
  try { data = JSON.parse(stripJsonc(raw)); } catch { return raw; }
  const servers = data && data.mcpServers;
  const entry = servers && servers.openviking;
  if (!entry || !extensionDir || !JSON.stringify(entry).includes(extensionDir)) return raw;
  delete servers.openviking;
  if (Object.keys(servers).length === 0) delete data.mcpServers;
  return `${JSON.stringify(data, null, 2)}\n`;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  // Subcommand first, as host-json-config.mjs does; a bare path keeps the
  // original OpenCode write call working.
  const [first, ...argv] = process.argv.slice(2);
  if (first === "remove-omp") {
    const [file, extensionDir] = argv;
    let raw = "";
    try { raw = fs.readFileSync(file, "utf8"); } catch { process.exit(0); }
    writeConfigFile(file, removeOmpMcpEntry(raw, { extensionDir: extensionDir || "" }));
  } else {
    const [file, pluginSpec, mcpProxy, kind] = [first, ...argv];
    let raw = "";
    try { raw = fs.readFileSync(file, "utf8"); } catch {}
    writeConfigFile(file, kind === "omp"
      ? updateOmpMcpConfig(raw, { mcpProxy: mcpProxy || "" })
      : updateOpencodeConfig(raw, { pluginSpec: pluginSpec || "", mcpProxy: mcpProxy || "" }));
  }
}

/**
 * Written through a temp file, then renamed over the target.
 *
 * A crash mid-write would otherwise leave a truncated config behind, and this
 * file holds the MCP servers of every other tool the user registered — the
 * `cp` backup the installer takes first is recovery, not a substitute. The
 * temp file is created with the target's own mode (a rename ships the inode,
 * so a 0600 config would otherwise reappear as 0644), and a symlinked config
 * is written through in place: renaming over it would replace the link with
 * a plain file and fork the user's dotfiles.
 */
function writeConfigFile(file, contents) {
  let mode = 0;
  try {
    const stat = fs.lstatSync(file);
    if (stat.isSymbolicLink()) {
      fs.writeFileSync(file, contents);
      return;
    }
    mode = stat.mode & 0o7777;
  } catch {}
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, contents, mode ? { mode } : undefined);
  fs.renameSync(tmp, file);
}
