# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""OpenAI VLM backend implementation"""

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Union

from ..base import VLMBase

logger = logging.getLogger(__name__)


class OpenAIVLM(VLMBase):
    """OpenAI VLM backend"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._sync_client = None
        self._async_client = None
        self.provider = "openai"

    def get_client(self):
        """Get sync client"""
        if self._sync_client is None:
            try:
                import openai
            except ImportError:
                raise ImportError("Please install openai: pip install openai")
            client_kwargs = {"api_key": self.api_key, "base_url": self.api_base}
            if self.extra_headers:
                client_kwargs["default_headers"] = self.extra_headers
            self._sync_client = openai.OpenAI(**client_kwargs)
        return self._sync_client

    def get_async_client(self):
        """Get async client"""
        if self._async_client is None:
            try:
                import openai
            except ImportError:
                raise ImportError("Please install openai: pip install openai")
            client_kwargs = {"api_key": self.api_key, "base_url": self.api_base}
            if self.extra_headers:
                client_kwargs["default_headers"] = self.extra_headers
            self._async_client = openai.AsyncOpenAI(**client_kwargs)
        return self._async_client

    def _is_streaming_response(self, response):
        """Check if response is a streaming response.

        Streaming responses are iterators that yield chunks, while non-streaming
        responses have a choices attribute directly.
        """
        # Check for async streaming first to avoid false positives
        if hasattr(response, "__aiter__"):
            return False  # Async responses handled separately
        # Streaming responses: iterators but not strings/lists/dicts with choices
        if hasattr(response, "__iter__") and not hasattr(response, "choices"):
            # Exclude basic iterable types that might slip through
            if isinstance(response, (str, bytes, list, dict)):
                return False
            return True
        # Some streaming responses might have _iterator attribute
        if hasattr(response, "_iterator") and not hasattr(response, "choices"):
            return True
        return False

    def _is_async_streaming_response(self, response):
        """Check if response is an async streaming response."""
        if hasattr(response, "__aiter__") and not hasattr(response, "choices"):
            # Exclude basic types that should never be treated as streaming
            if isinstance(response, (str, bytes, list, dict)):
                return False
            return True
        if hasattr(response, "_iterator") and not hasattr(response, "choices"):
            return True
        return False

    def _extract_content_from_chunk(self, chunk):
        """Extract content string from a single chunk."""
        try:
            choices = getattr(chunk, "choices", None)
            if not choices:
                return None
            delta = getattr(choices[0], "delta", None)
            if not delta:
                return None
            return getattr(delta, "content", None)
        except (AttributeError, IndexError):
            return None

    def _extract_usage_from_chunk(self, chunk):
        """Extract token usage from a chunk. Returns (prompt_tokens, completion_tokens)."""
        usage = getattr(chunk, "usage", None)
        if not usage:
            return 0, 0
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        return prompt_tokens, completion_tokens

    def _process_streaming_chunks(self, chunks):
        """Process streaming chunks and extract content and token usage.

        WARNING: This method consumes the iterator. Do not use the response
        object after calling this method as it will be exhausted.

        Returns (content, prompt_tokens, completion_tokens).
        """
        content_parts = []
        prompt_tokens = 0
        completion_tokens = 0

        for chunk in chunks:
            content = self._extract_content_from_chunk(chunk)
            if content:
                content_parts.append(content)

            pt, ct = self._extract_usage_from_chunk(chunk)
            if pt > 0:
                prompt_tokens = pt
            if ct > 0:
                completion_tokens = ct

        return "".join(content_parts), prompt_tokens, completion_tokens

    def _extract_content_and_usage(self, response):
        """Extract content from response, handling both streaming and non-streaming.

        Returns (content, prompt_tokens, completion_tokens, is_streaming).
        """
        logger.debug(f"[OpenAIVLM] Response type: {type(response)}")

        if self._is_streaming_response(response):
            content, prompt_tokens, completion_tokens = self._process_streaming_chunks(response)
            return content, prompt_tokens, completion_tokens, True
        else:
            # Non-streaming response
            content = response.choices[0].message.content or ""
            usage = getattr(response, "usage", None)
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            return content, prompt_tokens, completion_tokens, False

    async def _extract_content_and_usage_async(self, response):
        """Extract content from async response, handling both streaming and non-streaming.

        Returns (content, prompt_tokens, completion_tokens, is_streaming).
        """
        logger.debug(f"[OpenAIVLM] Async response type: {type(response)}")

        if self._is_async_streaming_response(response):
            # Note: This logic mirrors _process_streaming_chunks but uses
            # async for to handle async iterators. Python's async for and
            # sync for cannot be unified in a single method.
            content_parts = []
            prompt_tokens = 0
            completion_tokens = 0

            async for chunk in response:
                content = self._extract_content_from_chunk(chunk)
                if content:
                    content_parts.append(content)

                pt, ct = self._extract_usage_from_chunk(chunk)
                if pt > 0:
                    prompt_tokens = pt
                if ct > 0:
                    completion_tokens = ct

            return "".join(content_parts), prompt_tokens, completion_tokens, True
        else:
            # Non-streaming response
            content = response.choices[0].message.content or ""
            usage = getattr(response, "usage", None)
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            return content, prompt_tokens, completion_tokens, False

    def _finalize_response(
        self, content, prompt_tokens, completion_tokens, is_streaming, operation_name="completion"
    ):
        """Finalize response: log warnings and update token usage.

        Common post-processing for both sync and async responses.
        """
        if not content:
            logger.warning(
                f"[OpenAIVLM] Empty {operation_name} response received (streaming={is_streaming})"
            )

        if prompt_tokens > 0 or completion_tokens > 0:
            self.update_token_usage(
                model_name=self.model or "gpt-4o-mini",
                provider=self.provider,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        return content

    def _handle_response(self, response, operation_name="completion"):
        """Handle response extraction and token usage update."""
        content, prompt_tokens, completion_tokens, is_streaming = self._extract_content_and_usage(
            response
        )
        return self._finalize_response(
            content, prompt_tokens, completion_tokens, is_streaming, operation_name
        )

    async def _handle_response_async(self, response, operation_name="completion"):
        """Handle async response extraction and token usage update."""
        (
            content,
            prompt_tokens,
            completion_tokens,
            is_streaming,
        ) = await self._extract_content_and_usage_async(response)
        return self._finalize_response(
            content, prompt_tokens, completion_tokens, is_streaming, operation_name
        )

    def get_completion(self, prompt: str, thinking: bool = False) -> str:
        """Get text completion"""
        client = self.get_client()
        kwargs = {
            "model": self.model or "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
        }
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens

        response = client.chat.completions.create(**kwargs)
        content = self._handle_response(response, operation_name="text completion")
        return self._clean_response(content)

    async def get_completion_async(
        self, prompt: str, thinking: bool = False, max_retries: int = 0
    ) -> str:
        """Get text completion asynchronously"""
        client = self.get_async_client()
        kwargs = {
            "model": self.model or "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
        }
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                response = await client.chat.completions.create(**kwargs)
                content = await self._handle_response_async(
                    response, operation_name="text completion"
                )
                return self._clean_response(content)
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    await asyncio.sleep(2**attempt)

        if last_error:
            raise last_error
        else:
            raise RuntimeError("Unknown error in async completion")

    def _detect_image_format(self, data: bytes) -> str:
        """Detect image format from magic bytes.

        Supported formats: PNG, JPEG, GIF, WebP
        """
        if len(data) < 8:
            logger.warning(f"[OpenAIVLM] Image data too small: {len(data)} bytes")
            return "image/png"

        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        elif data[:2] == b"\xff\xd8":
            return "image/jpeg"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        elif data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
            return "image/webp"

        logger.warning(f"[OpenAIVLM] Unknown image format, magic bytes: {data[:8].hex()}")
        return "image/png"

    def _prepare_image(self, image: Union[str, Path, bytes]) -> Dict[str, Any]:
        """Prepare image data for vision completion."""
        if isinstance(image, bytes):
            b64 = base64.b64encode(image).decode("utf-8")
            mime_type = self._detect_image_format(image)
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{b64}"},
            }
        elif isinstance(image, Path) or (
            isinstance(image, str) and not image.startswith(("http://", "https://"))
        ):
            path = Path(image)
            suffix = path.suffix.lower()
            mime_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }.get(suffix, "image/png")
            with open(path, "rb") as f:
                data = f.read()
            b64 = base64.b64encode(data).decode("utf-8")
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{b64}"},
            }
        else:
            return {"type": "image_url", "image_url": {"url": image}}

    def get_vision_completion(
        self,
        prompt: str,
        images: List[Union[str, Path, bytes]],
        thinking: bool = False,
    ) -> str:
        """Get vision completion"""
        client = self.get_client()

        content = []
        for img in images:
            content.append(self._prepare_image(img))
        content.append({"type": "text", "text": prompt})

        kwargs = {
            "model": self.model or "gpt-4o-mini",
            "messages": [{"role": "user", "content": content}],
            "temperature": self.temperature,
        }
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens

        response = client.chat.completions.create(**kwargs)
        content = self._handle_response(response, operation_name="vision completion")
        return self._clean_response(content)

    async def get_vision_completion_async(
        self,
        prompt: str,
        images: List[Union[str, Path, bytes]],
        thinking: bool = False,
    ) -> str:
        """Get vision completion asynchronously"""
        client = self.get_async_client()

        content = []
        for img in images:
            content.append(self._prepare_image(img))
        content.append({"type": "text", "text": prompt})

        kwargs = {
            "model": self.model or "gpt-4o-mini",
            "messages": [{"role": "user", "content": content}],
            "temperature": self.temperature,
        }
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens

        response = await client.chat.completions.create(**kwargs)
        content = await self._handle_response_async(response, operation_name="vision completion")
        return self._clean_response(content)
