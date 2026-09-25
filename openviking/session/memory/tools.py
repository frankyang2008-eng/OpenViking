# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Memory tools - encapsulate VikingFS read operations for ReAct loop.

Reference: bot/vikingbot/agent/tools/base.py design pattern
"""

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from openviking.session.memory.utils import add_line_numbers, line_count, slice_content_lines
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.telemetry import bind_telemetry_stage, tracer
from openviking.utils.token_estimation import estimate_text_tokens
from openviking_cli.exceptions import NotFoundError
from openviking_cli.utils import get_logger

if TYPE_CHECKING:
    from openviking.server.identity import ToolContext

logger = get_logger(__name__)

_LLM_HIDDEN_MEMORY_FIELDS = {
    "source_extraction_id",
    "source_extraction_ids",
    "last_update_trace_id",
}


def memory_maintenance_notice(
    content: str, *, review_after_tokens: int | None = None
) -> Optional[Dict[str, Any]]:
    """Return maintenance metadata only when a memory exceeds the soft size threshold."""
    if review_after_tokens is None:
        from openviking_cli.utils.config import get_openviking_config

        review_after_tokens = get_openviking_config().memory.maintenance_review_tokens
    estimated_tokens = estimate_text_tokens(content)
    if estimated_tokens <= review_after_tokens:
        return None
    guidance = (
        f"This memory is estimated at {estimated_tokens:,} tokens, above the "
        f"{review_after_tokens:,}-token readability target, so maintenance is required: do not "
        "leave it as one broad oversized memory. Preserve every distinct valid fact exactly once. "
        "For a topic-keyed collection such as preferences, split different behavioral choice "
        "dimensions into focused same-type memories. For an identity-keyed collection such as "
        "entities, never invent multiple identities for one object; split only if distinct "
        "identities were mixed. Compact only duplicate wording, never concrete facts. Aim for the "
        f"{review_after_tokens:,}-token target, but fact integrity takes priority. Delete or replace "
        "the oversized source only after every fact has one clear destination."
    )
    return {
        "maintenance_required": True,
        "guidance": guidance,
    }


def optimize_search_result(result: Any, limit: int = 10) -> Any:
    """优化搜索结果以减少 Token 消耗，并过滤掉抽象文件。"""
    if isinstance(result, dict) and "error" in result:
        return {"error": extract_error_summary(result["error"])}
    if isinstance(result, dict) and "memories" in result:
        filtered = [
            item
            for item in result["memories"]
            if not (
                item.get("uri", "").endswith(".abstract.md")
                or item.get("uri", "").endswith(".overview.md")
            )
        ]
        return [{"uri": item["uri"], "score": item["score"]} for item in filtered[:limit]]
    return []


def optimize_tool_result(tool_name: str, result: Any) -> Any:
    """优化工具结果以减少 Token 消耗。"""
    if isinstance(result, dict) and "error" in result:
        return {"error": extract_error_summary(result["error"])}
    if tool_name == "search" and isinstance(result, dict) and "memories" in result:
        return optimize_search_result(result)
    # 对 read 工具返回的 dict，如果包含 content 字段，则截断 content
    if tool_name == "read" and isinstance(result, dict) and "content" in result:
        result = result.copy()
        result["content"] = MemoryFileUtils.truncate_content(result["content"])
    return result


def extract_error_summary(error: str) -> str:
    if "File not found" in error:
        return "File not found"
    if "Permission denied" in error:
        return "Permission denied"
    if "Timeout" in error:
        return "Timeout"
    return error[:50]


def add_tool_call_pair_to_messages(
    messages: List[Dict[str, Any]],
    call_id: Union[str, int],
    tool_name: str,
    params: Dict[str, Any],
    result: Any,
) -> None:
    """Add a tool call pair with optimized format to save tokens."""
    messages.append(
        {
            "role": "user",
            "content": json.dumps(
                {"tool_call_name": tool_name, "args": params, "result": result}, ensure_ascii=False
            ),
        }
    )


class MemoryTool(ABC):
    """
    Abstract base class for memory tools.

    Similar to bot/vikingbot/agent/tools/base.py Tool,
    but simplified for memory module's internal use.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name used in function calls."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of what the tool does."""
        pass

    @property
    @abstractmethod
    def parameters(self) -> Dict[str, Any]:
        """JSON Schema for tool parameters."""
        pass

    @abstractmethod
    async def execute(
        self,
        ctx: Optional["ToolContext"],
        **kwargs: Any,
    ) -> Any:
        """
        Execute the tool with given parameters.

        Args:
            ctx: Tool context (contains viking_fs)
            **kwargs: Tool-specific parameters

        Returns:
            Result of the tool execution (can be dict, list, string, etc.)
        """
        pass

    def to_schema(self) -> Dict[str, Any]:
        """Convert tool to OpenAI function schema format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class MemoryReadTool(MemoryTool):
    """Tool to read single memory file."""

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "Read single file"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "Memory URI to read, e.g., 'viking://user/user123/memories/profile.md'",
                },
                "offset": {
                    "type": "integer",
                    "description": "Starting line number to read from (0-indexed)",
                    "default": 0,
                    "minimum": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to read. -1 means read to end",
                    "default": -1,
                    "minimum": -1,
                },
            },
            "required": ["uri"],
        }

    async def execute(
        self,
        ctx: Optional["ToolContext"],
        **kwargs: Any,
    ) -> Any:
        if ctx is None:  # the signature allows it; every path below dereferences ctx
            return {"error": "missing tool context"}
        uri = kwargs.get("uri", "")
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", -1)
        try:
            content = await ctx.viking_fs.read_file(
                uri,
                ctx=ctx.request_ctx,
            )
            # Parse MEMORY_FIELDS from comment and return dict directly
            mf = MemoryFileUtils.read(content, uri=uri)
            if ctx.read_file_contents is not None:
                ctx.read_file_contents[uri] = mf
            # Remove links/backlinks from LLM-visible output (not needed for extraction)
            llm_result = mf.to_metadata()
            llm_result.pop("links", None)
            llm_result.pop("backlinks", None)
            for hidden_field in _LLM_HIDDEN_MEMORY_FIELDS:
                llm_result.pop(hidden_field, None)
            # Annotate with page_id for link extraction
            if ctx and ctx.page_id_map:
                page_id = ctx.page_id_map.get_page_id(uri)
                if page_id is not None:
                    llm_result["page_id"] = page_id
            plain_content = mf.plain_content() or ""
            maintenance_notice = memory_maintenance_notice(plain_content)
            if maintenance_notice is not None:
                llm_result["memory_maintenance_notice"] = maintenance_notice
            visible_content = slice_content_lines(plain_content, offset=offset, limit=limit)
            if visible_content:
                llm_result["content"] = add_line_numbers(visible_content, start_line=offset + 1)
            elif line_count(plain_content) == 0:
                llm_result["content"] = (
                    "<system-reminder>Warning: the file exists but the contents are empty.</system-reminder>"
                )
            else:
                llm_result["content"] = (
                    "<system-reminder>Warning: the file exists but is shorter than the provided "
                    f"offset ({offset + 1}). The file has {line_count(plain_content)} lines.</system-reminder>"
                )
            return llm_result
        except NotFoundError as e:
            return {"error": str(e)}
        except Exception as e:
            tracer.error(f"Failed to execute read: {e}")
            return {"error": str(e)}


class MemorySearchTool(MemoryTool):
    """Tool to perform semantic search."""

    @property
    def name(self) -> str:
        return "search"

    @property
    def description(self) -> str:
        return "Semantic search with session context"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query text",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return, default 10",
                    "default": 10,
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        ctx: Optional["ToolContext"],
        **kwargs: Any,
    ) -> Any:
        if ctx is None:  # the signature allows it; every path below dereferences ctx
            return {"error": "missing tool context"}
        try:
            query = kwargs.get("query", "")
            # Get target_uri from ctx.default_search_uris
            target_uri: Union[str, List[str]] = ""
            if ctx.default_search_uris:
                target_uri = ctx.default_search_uris
            limit = kwargs.get("limit", 10)
            request_ctx = ctx.request_ctx if ctx else None
            # Internal callers name themselves (prefetch / experience / patch_merge), so
            # their rerank spend is attributable per consumer and the query text stays
            # recoverable for offline replay. A ReAct tool call arrives without a label.
            consumer = str(kwargs.get("consumer") or "react")
            logger.info(
                "[MemorySearchTool] consumer=%s target_uri=%s limit=%s query=%s",
                consumer,
                target_uri,
                limit,
                str(query)[:200],
            )
            # L2 files only: L0/L1 summaries previously flooded the candidate pool and
            # starved the usable top-n (step-2 replay: usable=2/5 with rerank on), and
            # the +10 over-fetch existed only to compensate for them. level filters
            # collection, not navigation — the retriever still recurses through L0/L1
            # directories to find these files.
            # A (phase-1 verdict + labeling adjudication, 2026-09-25): prefetch skips
            # the LLM rerank via the rerank=False capability parameter — never by
            # withholding the client, which would degrade retrieval to flat QUICK.
            # Measured on this workspace: rerank scores steer the recursive expansion
            # toward LESS relevant files (blind dual-judge: OFF picks 1.569 vs ON
            # 1.091) while costing ~87k prompt tok/query + 45.7s wall clock.
            with bind_telemetry_stage(f"search_{consumer}"):
                search_result = await ctx.viking_fs.search(
                    query,
                    target_uri=target_uri,
                    limit=limit,
                    ctx=request_ctx,
                    level=[2],
                    rerank=consumer != "prefetch",
                )
            result = optimize_search_result(search_result.to_dict(), limit=limit)
            # What the caller actually gets, so a later offline comparison has a
            # baseline for the top-n this consumer would have read with rerank on.
            returned = result if isinstance(result, list) else result.get("memories", [])
            logger.info(
                "[MemorySearchTool] consumer=%s returned=%s uris=%s",
                consumer,
                len(returned),
                [item.get("uri", "") for item in returned if isinstance(item, dict)],
            )
            return result
        except Exception as e:
            tracer.error(f"Failed to execute search: {e}")
            return {"error": str(e)}


def _format_size(size_bytes: int) -> str:
    """Format size in bytes to human readable format."""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}M"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f}K"
    else:
        return f"{size_bytes}B"


class MemoryLsTool(MemoryTool):
    """Tool to list directory contents."""

    @property
    def name(self) -> str:
        return "ls"

    @property
    def description(self) -> str:
        return "List directory content, includes abstract field when output='agent'"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "Directory URI to list, e.g., 'viking://user/user123/memories'",
                },
            },
            "required": ["uri"],
        }

    async def execute(
        self,
        ctx: Optional["ToolContext"],
        **kwargs: Any,
    ) -> Any:
        if ctx is None:  # the signature allows it; every path below dereferences ctx
            return {"error": "missing tool context"}
        try:
            uri = kwargs.get("uri", "")
            entries = await ctx.viking_fs.ls(
                uri,
                output="agent",
                abs_limit=256,
                show_all_hidden=False,
                node_limit=1000,
                ctx=ctx.request_ctx,
            )
            # Format: filename size (e.g., "file.md 1.2K")
            result_lines = []
            for e in entries:
                if not e.get("isDir", False):
                    # Extract name from entry or fallback to uri
                    name = e.get("name", "")
                    if not name:
                        uri = e.get("uri", "")
                        name = uri.rsplit("/", 1)[-1] if "/" in uri else uri
                    size = e.get("size", 0)
                    result_lines.append(f"{name} {_format_size(size)}")
            if not result_lines:
                return "Directory is empty. You can write new files to create memory content."
            return "\n".join(result_lines)
        except Exception as e:
            tracer.info(f"Failed to execute ls: {e}")
            return {"error": str(e)}


# Tool registry
MEMORY_TOOLS_REGISTRY: Dict[str, MemoryTool] = {}


def register_tool(tool: MemoryTool) -> None:
    """Register a memory tool."""
    MEMORY_TOOLS_REGISTRY[tool.name] = tool


def get_tool(name: str) -> Optional[MemoryTool]:
    """Get a memory tool by name."""
    return MEMORY_TOOLS_REGISTRY.get(name)


# Register default tools
register_tool(MemoryReadTool())
register_tool(MemorySearchTool())
register_tool(MemoryLsTool())
