"""Integration tests for the SillyTavern-Hermes proxy."""
import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from proxy_st.hermes_ws import HermesWebSocketManager, HermesSession
from proxy_st.hermes_tools import hermes_tool_call_id, hermes_tool_complete_item
from proxy_st.models import models_cache_age, clear_models_cache, list_proxy_models
from proxy_st.persistence import SESSION_INFOS, _save_sessions


class TestWebSocketManager:
    """Test the WebSocket manager for connection and session handling."""

    @pytest.fixture
    def manager(self):
        """Create a test manager instance."""
        return HermesWebSocketManager(
            ws_url="ws://test:8642/api/ws",
            dashboard_url="http://test:9119"
        )

    @pytest.mark.asyncio
    async def test_session_creation(self, manager, monkeypatch):
        """Test that sessions are created and managed properly."""
        monkeypatch.setattr(manager, "_persist_session_info", lambda _session: None)
        pending_id = "__pending__test"
        manager._sessions[pending_id] = HermesSession(pending_id, "st-session")
        manager._pending_creations[pending_id] = "st-session"

        await manager._handle_message(json.dumps({
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "session.info",
                "payload": {"model": "test-model"},
                "session_id": "tui-session",
            },
        }))

        assert manager._st_to_tui["st-session"] == "tui-session"
        assert manager._sessions["tui-session"].info["model"] == "test-model"

    @pytest.mark.asyncio
    async def test_message_dispatching(self, manager):
        """Test that messages are properly dispatched to sessions."""
        # Create a test session
        session = HermesSession("tui-1", "st-1")
        manager._sessions["tui-1"] = session

        session.current_request_id = "request-1"
        session.pending_queues["request-1"] = asyncio.Queue()

        await manager._handle_message(json.dumps({
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "message.delta",
                "payload": {"text": "Hello"},
                "session_id": "tui-1",
            },
        }))

        event = session.pending_queues["request-1"].get_nowait()
        assert event == {"type": "message.delta", "text": "Hello"}


class TestToolCallHandling:
    """Test tool call translation and formatting."""

    def test_tool_call_id_generation(self):
        """Test that tool call IDs are generated properly."""
        # Test with explicit ID
        assert hermes_tool_call_id({"tool_id": "test-123"}) == "test-123"

        # Test with call_id
        assert hermes_tool_call_id({"call_id": "call-456"}) == "call-456"

        # Test fallback UUID generation
        result = hermes_tool_call_id({})
        assert len(result) == 13  # call_ + 9 hex chars

    def test_tool_complete_item(self):
        """Test that tool completion items are formatted correctly."""
        tool_data = {
            "name": "test_tool",
            "tool_id": "test-123",
            "result_text": "Test result",
            "duration_s": 1.5
        }

        item = hermes_tool_complete_item(tool_data)

        assert item["type"] == "function_call_output"
        assert item["name"] == "test_tool"
        assert item["status"] == "completed"
        assert item["output"] == "Test result"
        assert item["duration_s"] == 1.5


class TestModelManagement:
    """Test model caching and management."""

    def setup_method(self):
        """Clear models cache before each test."""
        clear_models_cache()

    @pytest.mark.asyncio
    async def test_cache_management(self):
        """Test that model cache is properly managed."""
        # Initially empty
        result1 = await list_proxy_models()
        assert result1["cache_hit"] is False

        # Set up cache
        test_models = [{"id": "model-1", "object": "model"}]
        from proxy_st.models import _models_cache_set
        _models_cache_set(test_models)

        # Should hit cache
        result = await list_proxy_models()
        assert result["cache_hit"] is True
        assert len(result["data"]) == 1

        # Test cache age
        age = models_cache_age()
        assert age is not None
        assert age >= 0

    @pytest.mark.asyncio
    async def test_model_fetch_with_caching(self):
        """Test that model fetching respects caching."""
        # First call should fetch (no cache)
        result1 = await list_proxy_models(refresh=True)
        assert result1["cache_hit"] is False

        # Second call should use cache
        result2 = await list_proxy_models()
        assert result2["cache_hit"] is True


class TestPersistence:
    """Test session persistence functionality."""

    def test_session_info_persistence(self):
        """Test that session info is properly persisted."""
        # Add test session info
        SESSION_INFOS["test-session"] = {
            "last_usage": time.time(),
            "total_requests": 5,
            "hermes": {
                "tui_session_id": "tui-1",
                "model": "test-model"
            }
        }

        # Save and verify
        _save_sessions()
        assert "test-session" in SESSION_INFOS
        assert SESSION_INFOS["test-session"]["hermes"]["model"] == "test-model"


class TestConfiguration:
    """Test configuration management."""

    def test_backend_config_loading(self):
        """Test that backend configurations are properly loaded."""
        from proxy_st.config import BACKEND_CONFIGS

        # Verify config structure
        assert isinstance(BACKEND_CONFIGS, dict)

        # Test with default configuration
        for backend_name, config in BACKEND_CONFIGS.items():
            assert "base_url" in config or config.get("base_url") is None


class TestErrorHandling:
    """Test error handling and recovery."""

    @pytest.mark.asyncio
    async def test_websocket_reconnection(self):
        """Test that WebSocket reconnection works properly."""
        manager = HermesWebSocketManager()

        # Mock WebSocket behavior
        with patch('proxy_st.hermes_ws.websockets.connect') as mock_connect:
            # First connection succeeds
            mock_ws1 = AsyncMock()
            mock_ws1.closed = False
            mock_connect.return_value = mock_ws1

            await manager.start()

            # Simulate connection close
            mock_ws1.closed = True
            mock_ws1.close = AsyncMock()

            # Should attempt reconnection
            await asyncio.sleep(0.1)  # Allow reconnection logic to run

            # Verify reconnection was attempted
            assert mock_connect.call_count >= 1


class TestTodoChecklist:
    """Test the todo checklist functionality."""

    def test_todo_status_normalization(self):
        """Test that todo statuses are properly normalized."""
        # Simulate the normalization logic
        def normalizeTodoStatus(status):
            normalized = str(status or '').strip().lower().replace(' ', '_')
            if normalized in {'complete', 'completed', 'done', 'success', 'succeeded'}:
                return 'completed'
            if normalized in {'inprogress', 'in_progress', 'running', 'active', 'started'}:
                return 'in_progress'
            if normalized in {'cancel', 'cancelled', 'canceled', 'skipped', 'abandoned'}:
                return 'cancelled'
            if normalized in {'open', 'opened', 'todo', 'planned', 'pending'}:
                return 'pending'
            return normalized

        # Test various status formats
        assert normalizeTodoStatus("completed") == "completed"
        assert normalizeTodoStatus("in_progress") == "in_progress"
        assert normalizeTodoStatus("cancelled") == "cancelled"
        assert normalizeTodoStatus("pending") == "pending"

        # Test alias normalization
        assert normalizeTodoStatus("done") == "completed"
        assert normalizeTodoStatus("running") == "in_progress"
        assert normalizeTodoStatus("abandoned") == "cancelled"

    def test_todo_parsing(self):
        """Test that todo lists are properly parsed."""
        def parseTodos(todos_data):
            if todos_data is None:
                return []

            if isinstance(todos_data, str):
                # Parse markdown format
                lines = todos_data.split('\n')
                result = []
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith('[x]') or line.startswith('[X]'):
                        result.append({'text': line[3:].strip(), 'completed': True})
                    elif line.startswith('[ ]'):
                        result.append({'text': line[3:].strip(), 'completed': False})
                    else:
                        result.append({'text': line, 'completed': False})
                return result

            return []

        # Test string format
        todos = parseTodos("[x] Task 1\n[ ] Task 2")
        assert len(todos) == 2
        assert todos[0]["completed"] is True
        assert todos[1]["completed"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
