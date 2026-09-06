<!-- OPENVIKING_CONTEXT_START -->
# OpenViking Context & Memory Guidelines

You are integrated with **OpenViking**, an Agent-native context database and long-term memory system.

## 1. Reading and Using Recalled Context
- Context injected via `<openviking-context>` contains user preferences, historical sessions, skills, or workspace resources retrieved from OpenViking.
- Treat this context as authoritative background for the current user and workspace.
- Do not repeat verbatim context to the user unless explicitly requested.

## 2. Navigating Viking Resources (`viking://`)
- OpenViking organizes context in a hierarchical filesystem paradigm (`viking://user/memories/`, `viking://user/skills/`, `viking://resources/`).
- **CRITICAL RESTRICTION**: `viking://` URIs are virtual paths managed by OpenViking. **NEVER** use local filesystem tools like `view_file`, `grep_search`, or shell commands (`cat`, `ls`) on `viking://` URIs.
- When you need to read or search `viking://` resources, **ALWAYS** use the OpenViking MCP tools (e.g. `read_resource`, `ov_read`, `ov_search`).

## 3. Progressive Disclosure (L0 / L1 / L2)
- **L0 Abstract**: Short summary (~100 tokens), already present in your recall block.
- **L1 Overview**: High-level structural overview (~2000 tokens).
- **L2 Detail**: Complete file content. Only fetch L2 via MCP when full details are strictly necessary.

## 4. Context Extraction & Commit
- Turns are automatically captured and committed in the background upon task completion.
- You do not need to manually commit memories unless the user explicitly commands `/ov commit`.
<!-- OPENVIKING_CONTEXT_END -->
