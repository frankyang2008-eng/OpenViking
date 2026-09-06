---
name: openviking
description: Guide and runbook for interacting with the OpenViking long-term memory system in Antigravity (AGY). Use when checking memory status, searching context, manual commits, or troubleshooting the OpenViking connection.
---

# OpenViking Interaction Runbook

This skill provides step-by-step guidance for interacting with OpenViking in Antigravity (AGY).

## When to Activate
- Checking OpenViking daemon/server connection status.
- Searching memories, skills, or resources across past conversations.
- Manually forcing a session commit (`/ov commit`).
- Troubleshooting memory recall or capture issues.

---

## 1. Checking Connection Status
Run health check via shell or CLI:
```bash
ov doctor
# Or curl local server endpoint
curl -s http://127.0.0.1:1933/health
```

Expected response:
```json
{"status": "ok", "version": "..."}
```

---

## 2. Searching OpenViking Context
To search context across workspaces or past conversations:
- Use OpenViking MCP tool `ov_search` or CLI:
```bash
ov search "query text" --limit 5
```
- Or inspect specific directory structures:
```bash
ov ls "viking://user/memories/"
```

---

## 3. Manual Session Commit
While the AGY plugin automatically captures and commits conversation turns at the end of runs, you can trigger manual extraction:
```bash
ov commit
```
This instructs OpenViking to run `SessionCompressorV3`, extracting structured memories and reusable skills.

---

## 4. Configuration Reference
OpenViking client settings reside in `ovcli.conf` or environment variables:
- `OPENVIKING_URL`: Server endpoint (default `http://127.0.0.1:1933`)
- `OPENVIKING_API_KEY`: API authentication key (if enabled)
- `OPENVIKING_AUTO_RECALL`: Enable/disable proactive recall (`true`/`false`)
- `OPENVIKING_AUTO_CAPTURE`: Enable/disable turn capture (`true`/`false`)
