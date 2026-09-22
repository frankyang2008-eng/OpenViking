---
name: ov-memory-session-investigator
description: Read-only diagnosis of suspicious OpenViking memories by tracing a memory URI through memory_diff.json, its session archive, original messages, and merged session context. Use for wrong paths, unexpected owners, duplicate memories, unexplained updates/deletes, or empty memory directories.
---

# OpenViking Memory Session Investigator

Trace the artifact chain first. Produce a short conclusion, not a general OpenViking explanation.

Use an evidence budget: at most three representative diffs per symptom and two source mechanisms unless the artifacts conflict. Stop as soon as origin, persistence, and ownership boundary are supported.

## Inputs

Get the suspicious memory URI or path fragment and the expected behavior. Use the active `ov config` by default. If the user gives another config file, prefix every `ov` command with:

```bash
OPENVIKING_CREDENTIAL_SOURCE=cli OPENVIKING_CLI_CONFIG_FILE=/absolute/path/to/ovcli.conf
```

Never print API keys or config contents. Stay read-only: do not call `remember`, `commit`, `write`, `rm`, `mv`, `reindex`, or admin mutations.

## Fast path

### 1. Pin the actual identity

```bash
ov health -o json
```

Record server version, `account_id`, and `user_id`. The config filename is not an identity. Derive the session search root as `viking://user/{user_id}/sessions`.

If CLI diagnostics make JSON hard to pipe, use:

```bash
ov_json() { "$@" -o json 2>/dev/null | sed -n '/^{/,$p'; }
```

### 2. Find diffs that actually changed the target

Search the exact URI when possible:

```bash
ov_json ov grep 'EXACT_MEMORY_URI' \
  -u 'viking://user/USER_ID/sessions' -n 200 \
  | jq -r '.result.matches[]?.uri | select(endswith("/memory_diff.json"))' \
  | sort -u
```

For a directory symptom, search its stable path fragment. Ignore matches in `messages.jsonl`, tool outputs, current investigation sessions, and resource mirrors. They only show that somebody mentioned the URI. A provenance claim requires an operation in `memory_diff.json`.

Do not read every session or scan every diff in the library. Select the smallest useful set:

- earliest visible `ADD` for origin;
- latest `UPDATE` for persistence;
- relevant `DELETE` for disappearance or an empty directory.

### 3. Read the diff, then its evidence

For each selected diff:

```bash
ov_json ov read 'DIFF_URI' | jq -r '.result' \
  | jq '{extracted_at, summary, operations}'
```

Derive `SESSION_ID` and `archive_NNN` from the diff URI. Read only the neighboring evidence needed to explain the operation:

```bash
ov read 'ARCHIVE_URI/messages.jsonl'
ov read 'ARCHIVE_URI/.meta.json'
ov read 'ARCHIVE_URI/.overview.md'
ov session get 'SESSION_ID' -o json
ov session get-session-context 'SESSION_ID' -o json
```

Answer these questions from the artifacts:

1. Was the operation `ADD`, `UPDATE`, or `DELETE`?
2. Which role and `peer_id` supplied the source statement?
3. Did the original text contain a stable name/owner, or only words such as “我”, “用户”, or “当前用户”?
4. Does the session prefix indicate a known route (`cx-*`, `mcp-store-*`) or a caller-defined session? Treat this as a routing clue, not proof of the originating client.
5. Does merged context agree with the archived messages? Prefer raw archive messages for provenance; context is useful for what the extractor could summarize or retain.

### 4. Explain persistence, not just creation

When a strange value appears repeatedly, check whether later diffs are updates to the same URI. Existing-page reuse, immutable schema fields, and prefetch of prior memories can preserve the first value even when later messages do not repeat it.

For an empty directory, prove this sequence separately:

- identify its former child file(s);
- find their `ADD` and final `DELETE` diffs;
- compare the directory mtime with the delete time;
- distinguish “empty parent left after delete” from “empty memory created”.

### 5. Escalate to source only for the remaining mechanism

If the artifacts establish what happened but not why, inspect the matching OpenViking revision. Limit this pass to the schema/path mechanism and, when relevant, the delete cleanup mechanism. Search by symbols instead of relying on stale line numbers:

```bash
rg -n 'memory_type: preferences|filename_template' openviking/prompts/templates/memory
rg -n 'prefetch|immutable_fields|generate_uri' openviking/session/memory
rg -n 'class StoreMessage|async def remember' openviking/server
rg -n 'generate_overview|can_delete_directory' openviking/session/memory
```

Separate four contracts:

- authentication identity: outer `viking://user/{user_id}`;
- message attribution: role and optional `peer_id`;
- schema fields used inside the filename/path;
- cleanup behavior after update/delete.

Do not call a model-generated path label a real OpenViking user unless an admin/user record proves it. Do not blame a plugin merely because the message mentions that plugin; require caller metadata, a distinctive session route, or client-side logs.

## Comparison config

Use a second config only to test a concrete contrast. Run the same `health`, memory listing, and one representative session trace with the explicit config prefix. Compare identities and path behavior; never compare or expose credentials.

## Report contract

Keep the report compact:

```text
结论
- 一句话说明发生了什么。
- 主因 / 诱因 / 非原因。

证据链
时间 | session/archive | operation | URI | 原始上下文

机制
- 输入给了什么；抽取器补了什么；为何后续持续。

边界
- 缺少哪些 trace/log，因此哪部分只能推断。
```

Lead with the decision. Usually three to six evidence rows are enough. Stop when the origin, persistence mechanism, and ownership boundary are all supported; do not turn the report into a full-library inventory.
