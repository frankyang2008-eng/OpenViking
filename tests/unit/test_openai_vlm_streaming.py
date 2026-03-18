# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Tests for OpenAI VLM streaming response handling."""

from unittest.mock import MagicMock, patch

import pytest

from openviking.models.vlm.backends.openai_vlm import OpenAIVLM


class MockChunk:
    """Mock streaming chunk."""

    def __init__(self, content=None, usage=None, finish_reason=None, empty_choices=False):
        self.choices = []
        if not empty_choices:
            delta = MagicMock()
            delta.content = content
            delta.role = "assistant"
            choice = MagicMock()
            choice.delta = delta
            choice.index = 0
            choice.finish_reason = finish_reason
            self.choices.append(choice)
        self.usage = usage


class MockUsage:
    def __init__(self, prompt_tokens=0, completion_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class TestOpenAIVLMStreamingDetection:
    """Test streaming vs non-streaming detection."""

    def test_is_streaming_response_iterator(self):
        """Test detection of streaming response (has __iter__)."""
        vlm = OpenAIVLM({"api_key": "test"})

        class IteratorResponse:
            def __iter__(self):
                return iter([])

        response = IteratorResponse()
        assert vlm._is_streaming_response(response) is True

    def test_is_streaming_response_with_choices(self):
        """Test non-streaming response has choices attribute."""
        vlm = OpenAIVLM({"api_key": "test"})

        class ResponseWithChoices:
            def __init__(self):
                self.choices = [MagicMock()]

        response = ResponseWithChoices()
        assert vlm._is_streaming_response(response) is False

    def test_is_streaming_response_with_iterator_attr(self):
        """Test detection via _iterator attribute."""
        vlm = OpenAIVLM({"api_key": "test"})

        class ResponseWithIterator:
            _iterator = True

        response = ResponseWithIterator()
        assert vlm._is_streaming_response(response) is True

    def test_is_streaming_response_excludes_basic_types(self):
        """Test that string/list/dict are not detected as streaming."""
        vlm = OpenAIVLM({"api_key": "test"})

        # Strings have __iter__ but should not be streaming
        assert vlm._is_streaming_response("hello") is False
        # Lists have __iter__ but should not be streaming
        assert vlm._is_streaming_response([1, 2, 3]) is False
        # Dicts have __iter__ but should not be streaming
        assert vlm._is_streaming_response({"key": "value"}) is False
        # Bytes have __iter__ but should not be streaming
        assert vlm._is_streaming_response(b"hello") is False

    def test_is_streaming_response_excludes_async(self):
        """Test that async iterators are not detected as sync streaming."""
        vlm = OpenAIVLM({"api_key": "test"})

        class AsyncIteratorResponse:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        response = AsyncIteratorResponse()
        # Should NOT be detected as sync streaming (has __aiter__)
        assert vlm._is_streaming_response(response) is False

    def test_is_async_streaming_response(self):
        """Test detection of async streaming response."""
        vlm = OpenAIVLM({"api_key": "test"})

        class AsyncIteratorResponse:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        response = AsyncIteratorResponse()
        assert vlm._is_async_streaming_response(response) is True

        class ResponseWithChoices:
            def __init__(self):
                self.choices = [MagicMock()]

        response_with_choices = ResponseWithChoices()
        assert vlm._is_async_streaming_response(response_with_choices) is False


class TestOpenAIVLMChunkExtraction:
    """Test chunk content and usage extraction."""

    def test_extract_content_from_chunk(self):
        """Test extracting content from a valid chunk."""
        vlm = OpenAIVLM({"api_key": "test"})
        chunk = MockChunk(content="Hello world")
        assert vlm._extract_content_from_chunk(chunk) == "Hello world"

    def test_extract_content_from_chunk_empty(self):
        """Test extracting content from empty chunk."""
        vlm = OpenAIVLM({"api_key": "test"})
        chunk = MockChunk(content=None)
        assert vlm._extract_content_from_chunk(chunk) is None

    def test_extract_content_from_chunk_no_choices(self):
        """Test extracting content from chunk without choices."""
        vlm = OpenAIVLM({"api_key": "test"})
        chunk = MagicMock()
        chunk.choices = []
        assert vlm._extract_content_from_chunk(chunk) is None

    def test_extract_usage_from_chunk(self):
        """Test extracting usage from chunk."""
        vlm = OpenAIVLM({"api_key": "test"})
        chunk = MockChunk(usage=MockUsage(10, 20))
        pt, ct = vlm._extract_usage_from_chunk(chunk)
        assert pt == 10
        assert ct == 20

    def test_extract_usage_from_chunk_no_usage(self):
        """Test extracting usage from chunk without usage."""
        vlm = OpenAIVLM({"api_key": "test"})
        chunk = MockChunk()
        pt, ct = vlm._extract_usage_from_chunk(chunk)
        assert pt == 0
        assert ct == 0


class TestOpenAIVLMStreamingExtraction:
    """Test streaming response extraction."""

    def test_process_streaming_chunks(self):
        """Test processing multiple streaming chunks."""
        vlm = OpenAIVLM({"api_key": "test"})

        chunks = [
            MockChunk(content="Hello"),
            MockChunk(content=" "),
            MockChunk(content="world"),
        ]

        content, pt, ct = vlm._process_streaming_chunks(chunks)
        assert content == "Hello world"
        assert pt == 0
        assert ct == 0

    def test_process_streaming_chunks_with_usage(self):
        """Test processing chunks with token usage."""
        vlm = OpenAIVLM({"api_key": "test"})

        chunks = [
            MockChunk(content="Hello", usage=MockUsage(10, 1)),
            MockChunk(content=" world", usage=MockUsage(10, 2)),
            MockChunk(content="!"),
        ]

        content, pt, ct = vlm._process_streaming_chunks(chunks)
        assert content == "Hello world!"
        # Last chunk with usage wins
        assert pt == 10
        assert ct == 2

    def test_extract_content_and_usage_streaming(self):
        """Test full extraction from streaming response."""
        vlm = OpenAIVLM({"api_key": "test"})

        chunks = [
            MockChunk(content="Streamed"),
            MockChunk(content=" content"),
        ]

        class MockStreamingResponse:
            def __iter__(self):
                return iter(chunks)

        response = MockStreamingResponse()
        content, pt, ct, is_streaming = vlm._extract_content_and_usage(response)

        assert content == "Streamed content"
        assert is_streaming is True

    def test_extract_content_and_usage_non_streaming(self):
        """Test full extraction from non-streaming response."""
        vlm = OpenAIVLM({"api_key": "test"})

        class NonStreamingResponse:
            def __init__(self):
                self.choices = [MagicMock()]
                self.choices[0].message.content = "Normal response"
                self.usage = MagicMock()
                self.usage.prompt_tokens = 10
                self.usage.completion_tokens = 5

        response = NonStreamingResponse()
        content, pt, ct, is_streaming = vlm._extract_content_and_usage(response)

        assert content == "Normal response"
        assert pt == 10
        assert ct == 5
        assert is_streaming is False


class TestOpenAIVLMStreamingAsync:
    """Test async streaming response parsing."""

    @pytest.mark.asyncio
    async def test_extract_content_and_usage_async_streaming(self):
        """Test extracting content from async streaming response."""
        vlm = OpenAIVLM({"api_key": "test"})

        chunks = [
            MockChunk(content="Hello"),
            MockChunk(content=" async"),
            MockChunk(content=" world"),
        ]

        class MockAsyncStreamingResponse:
            def __init__(self, chunks):
                self.chunks = chunks
                self.index = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.index >= len(self.chunks):
                    raise StopAsyncIteration
                chunk = self.chunks[self.index]
                self.index += 1
                return chunk

        response = MockAsyncStreamingResponse(chunks)
        content, pt, ct, is_streaming = await vlm._extract_content_and_usage_async(response)

        assert content == "Hello async world"
        assert is_streaming is True

    @pytest.mark.asyncio
    async def test_extract_content_and_usage_async_non_streaming(self):
        """Test extracting content from async non-streaming response."""
        vlm = OpenAIVLM({"api_key": "test"})

        class AsyncNonStreamingResponse:
            def __init__(self):
                self.choices = [MagicMock()]
                self.choices[0].message.content = "Async response"
                self.usage = None

        response = AsyncNonStreamingResponse()
        content, pt, ct, is_streaming = await vlm._extract_content_and_usage_async(response)

        assert content == "Async response"
        assert is_streaming is False


class TestOpenAIVLMIntegration:
    """Integration tests with mocked OpenAI client."""

    @patch("openviking.models.vlm.backends.openai_vlm.openai.OpenAI")
    def test_get_completion_with_streaming_response(self, mock_openai_class):
        """Test get_completion when API returns streaming format."""
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client

        chunks = [
            MockChunk(content="Streamed"),
            MockChunk(content=" response"),
            MockChunk(content=" text"),
        ]

        class MockStream:
            def __iter__(self):
                return iter(chunks)

        mock_client.chat.completions.create.return_value = MockStream()

        vlm = OpenAIVLM({"api_key": "test", "model": "gpt-4o-mini"})
        result = vlm.get_completion("Hello")

        assert result == "Streamed response text"

    @patch("openviking.models.vlm.backends.openai_vlm.openai.OpenAI")
    def test_get_completion_with_non_streaming_response(self, mock_openai_class):
        """Test get_completion when API returns normal format."""
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client

        class NonStreamingResponse:
            def __init__(self):
                self.choices = [MagicMock()]
                self.choices[0].message.content = "Normal response"
                self.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

        mock_client.chat.completions.create.return_value = NonStreamingResponse()

        vlm = OpenAIVLM({"api_key": "test", "model": "gpt-4o-mini"})
        result = vlm.get_completion("Hello")

        assert result == "Normal response"
        # Verify token usage was updated
        usage = vlm.get_token_usage_summary()
        assert usage["total_prompt_tokens"] == 10
        assert usage["total_completion_tokens"] == 5

    @patch("openviking.models.vlm.backends.openai_vlm.openai.OpenAI")
    def test_get_vision_completion_with_streaming(self, mock_openai_class):
        """Test vision completion with streaming response."""
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client

        chunks = [
            MockChunk(content="Vision"),
            MockChunk(content=" result"),
        ]

        class MockStream:
            def __iter__(self):
                return iter(chunks)

        mock_client.chat.completions.create.return_value = MockStream()

        vlm = OpenAIVLM({"api_key": "test", "model": "gpt-4o-mini"})
        result = vlm.get_vision_completion("Describe this", ["image_url"])

        assert result == "Vision result"

    @patch("openviking.models.vlm.backends.openai_vlm.openai.OpenAI")
    def test_token_usage_with_streaming_chunks(self, mock_openai_class):
        """Test token usage extraction from streaming chunks."""
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client

        chunks = [
            MockChunk(content="Hello", usage=MockUsage(40, 1)),
            MockChunk(content=" world", usage=MockUsage(40, 2)),
            MockChunk(content="!", usage=MockUsage(40, 3)),
        ]

        class MockStream:
            def __iter__(self):
                return iter(chunks)

        mock_client.chat.completions.create.return_value = MockStream()

        vlm = OpenAIVLM({"api_key": "test", "model": "test-model"})
        result = vlm.get_completion("Hello")

        assert result == "Hello world!"
        usage = vlm.get_token_usage_summary()
        assert usage["total_prompt_tokens"] == 40
        assert usage["total_completion_tokens"] == 3
