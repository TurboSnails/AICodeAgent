"""AIClient：Claude CLI JSON 响应解析。"""
import json
from unittest.mock import patch

import pytest

from engine.exceptions import AgentEmptyOutputError, AgentRateLimitError
from services.ai_client import AIClient


class TestParseClaudeStdout:
    def test_json_result_field(self):
        client = AIClient()
        raw = json.dumps({
            "type": "result",
            "is_error": False,
            "result": "=== FILE: app/Foo.kt ===\nclass Foo\n=== END FILE ===",
            "num_turns": 2,
        })
        out = client._parse_claude_stdout(raw, "", 0, 100)
        assert "=== FILE:" in out

    def test_json_rate_limit(self):
        client = AIClient()
        raw = json.dumps({"is_error": True, "api_error_status": "rate limit exceeded"})
        with pytest.raises(AgentRateLimitError):
            client._parse_claude_stdout(raw, "", 1, 100)

    def test_empty_json_result(self):
        client = AIClient()
        raw = json.dumps({"is_error": False, "result": ""})
        with pytest.raises(AgentEmptyOutputError):
            client._parse_claude_stdout(raw, "", 0, 100)

    @patch("services.ai_client.cfg_str", side_effect=lambda key, default="": {
        "ai.claude_output_format": "json",
        "ai.claude_headless_permission_mode": "dontAsk",
        "ai.claude_headless_tools": "Read",
        "ai.claude_agent_permission_mode": "acceptEdits",
        "ai.claude_agent_tools": "Read,Edit",
        "ai.claude_disallowed_tools": "Bash",
    }.get(key, default))
    @patch("services.ai_client.cfg_bool", return_value=False)
    @patch("services.ai_client.cfg_int", return_value=5)
    def test_build_agent_cmd_uses_accept_edits(self, _i, _b, _s):
        client = AIClient()
        cmd = client._build_claude_cmd("hello", headless=False)
        assert cmd[0:3] == ["claude", "-p", "hello"]
        assert "--output-format" in cmd
        assert "acceptEdits" in cmd
        assert "--allowed-tools" in cmd
        assert "Edit" in " ".join(cmd)


class TestFinalizeReturnCode:
    def test_zero_returncode_not_converted_to_minus_one(self):
        """proc.returncode=0 应保持 0，不能被 `or -1` 转成 -1。"""
        client = AIClient()
        good_json = json.dumps({
            "type": "result",
            "is_error": False,
            "result": "ok",
        })
        out = client._finalize_claude_result(
            good_json, "", 0, 100,
            progress_workspace=None, elapsed_sec=1,
        )
        assert out == "ok"
