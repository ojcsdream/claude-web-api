import json
import sys
import types
import unittest
import importlib.util
from unittest.mock import patch

if importlib.util.find_spec("fastapi") is None:
    sys.modules.setdefault("fastapi", types.SimpleNamespace(UploadFile=object))
sys.modules.setdefault(
    "chat_utils",
    types.SimpleNamespace(
        estimate_round_tokens=lambda *args, **kwargs: 0,
        is_image_file=lambda filename: False,
    ),
)
sys.modules.setdefault(
    "config",
    types.SimpleNamespace(
        BASE_DIR=".",
        DEFAULT_MODEL="gpt-5.5",
        MAX_IMAGE_UPLOAD_BYTES=20_000_000,
        MAX_UPLOAD_BYTES=20_000_000,
        MODEL_TEMPERATURE=0.1,
        UPLOAD_DIR="uploads",
    ),
)
sys.modules.setdefault("db", types.SimpleNamespace(db_add_message=lambda *args, **kwargs: None))

import services


class _FakeResponse:
    def __init__(self, payload=None, lines=None):
        self.payload = payload or {"output_text": "ok"}
        self.lines = lines or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __iter__(self):
        return iter(self.lines)

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ResponsesPayloadTest(unittest.TestCase):
    def test_plain_responses_input_without_default_tools(self):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse()

        prompt = "请解释这段普通文本"
        with patch.object(services, "resilient_urlopen", fake_urlopen):
            result = services.call_direct_responses_api(
                prompt,
                "https://api.openai.com",
                "test-token",
                api_model="gpt-5.5",
            )

        self.assertEqual(result, "ok")
        self.assertEqual(captured["body"]["input"], prompt)
        self.assertNotIn("tools", captured["body"])
        self.assertNotIn("tool_choice", captured["body"])

    def test_github_url_stays_in_responses_input_without_default_tools(self):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse()

        prompt = "请分析 https://github.com/owner/repo 这个项目"
        with patch.object(services, "resilient_urlopen", fake_urlopen):
            result = services.call_direct_responses_api(
                prompt,
                "https://api.openai.com",
                "test-token",
                api_model="gpt-5.5",
            )

        self.assertEqual(result, "ok")
        self.assertEqual(captured["body"]["input"], prompt)
        self.assertNotIn("tools", captured["body"])
        self.assertNotIn("tool_choice", captured["body"])

    def test_web_search_tool_is_only_added_when_requested(self):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse()

        with patch.object(services, "resilient_urlopen", fake_urlopen):
            services.call_direct_responses_api(
                "OpenAI latest news",
                "https://api.openai.com",
                "test-token",
                api_model="gpt-5.5",
                use_web_search=True,
            )

        self.assertEqual(captured["body"]["tools"], [{"type": "web_search"}])
        self.assertEqual(captured["body"]["tool_choice"], "auto")

    def test_responses_system_prompt_uses_instructions_field(self):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse()

        with patch.object(services, "resilient_urlopen", fake_urlopen):
            services.call_direct_responses_api(
                "你好",
                "https://api.openai.com/v1",
                "test-token",
                api_model="gpt-5.5",
                system_prompt="系统提示词：\n只输出 JSON\n请在整个回复中遵守上面的系统提示词。",
            )

        self.assertEqual(captured["body"]["instructions"], "只输出 JSON")
        self.assertEqual(captured["body"]["input"], "你好")
        self.assertNotIn("messages", captured["body"])

    def test_streaming_responses_payload_uses_same_contract(self):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse(lines=[
                b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n',
            ])

        with patch.object(services, "resilient_urlopen", fake_urlopen):
            chunks = list(services.stream_direct_responses_api_text(
                "你好",
                "https://api.openai.com",
                "test-token",
                api_model="gpt-5.5",
                system_prompt="只输出 JSON",
                use_web_search=True,
            ))

        self.assertEqual("".join(chunks), "ok")
        self.assertEqual(captured["body"]["instructions"], "只输出 JSON")
        self.assertEqual(captured["body"]["input"], "你好")
        self.assertTrue(captured["body"]["stream"])
        self.assertEqual(captured["body"]["tools"], [{"type": "web_search"}])

    def test_responses_input_payload_includes_images_and_files(self):
        with patch.object(services, "_file_data_url", lambda path, media_type="": f"data:{media_type or 'text/plain'};base64,xxx"):
            payload = services.build_responses_input_payload(
                "分析这些附件",
                image_local_paths=["./uploads/a.png"],
                file_items=[{"name": "note.txt", "local_path": "./uploads/note.txt"}],
            )

        self.assertEqual(payload[0]["role"], "user")
        content = payload[0]["content"]
        self.assertEqual(content[0], {"type": "input_text", "text": "分析这些附件"})
        self.assertEqual(content[1]["type"], "input_image")
        self.assertEqual(content[1]["image_url"], "data:image/png;base64,xxx")
        self.assertEqual(content[2]["type"], "input_file")
        self.assertEqual(content[2]["filename"], "note.txt")
        self.assertEqual(content[2]["file_data"], "data:text/plain;base64,xxx")

    def test_responses_vision_streaming_payload_uses_input_array(self):
        captured = {}

        def fake_responses_stream(prompt, api_base_url, api_auth_token, **kwargs):
            captured["prompt"] = prompt
            captured["body"] = {
                "api_base_url": api_base_url,
                "api_auth_token": api_auth_token,
                **kwargs,
            }
            yield "ok"

        with patch.object(services, "build_vision_payload_parts", return_value=([], [])):
            with patch.object(services, "build_responses_input_payload", return_value=[{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "看图"}],
            }]):
                with patch.object(services, "stream_direct_responses_api_text", fake_responses_stream):
                    chunks = list(services.stream_direct_vision_api_text(
                        "看图",
                        ["./uploads/a.png"],
                        "https://api.openai.com",
                        "test-token",
                        api_model="gpt-5.5",
                        api_protocol="responses",
                        system_prompt="只描述图片",
                        file_items=[{"name": "note.txt", "local_path": "./uploads/note.txt"}],
                        use_web_search=True,
                    ))

        self.assertEqual("".join(chunks), "ok")
        self.assertEqual(captured["prompt"], "看图")
        self.assertEqual(captured["body"]["system_prompt"], "只描述图片")
        self.assertEqual(captured["body"]["input_payload"][0]["role"], "user")
        self.assertTrue(captured["body"]["use_web_search"])

    def test_github_observation_keeps_large_source_excerpt(self):
        long_source = "x" * 50000
        observation = services.build_search_tool_observation(
            "分析 GitHub 源码",
            [{
                "index": 1,
                "title": "owner/repo: app.py",
                "url": "https://github.com/owner/repo/blob/main/app.py",
                "excerpt": long_source,
                "provider": "github-mcp",
                "quality": "official",
                "query": "",
            }],
            {"search_queries": [], "parse_links": ["https://github.com/owner/repo"]},
        )

        self.assertIn("github_mcp", observation)
        self.assertIn(long_source, observation)


class ClaudeMessagesPayloadTest(unittest.TestCase):
    def test_claude_system_prompt_uses_top_level_system_field(self):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse({"content": [{"type": "text", "text": "ok"}]})

        with patch.object(services, "resilient_urlopen", fake_urlopen):
            result = services.call_direct_text_api(
                "你好",
                "https://api.anthropic.com",
                "test-token",
                api_model="claude-sonnet-4-6",
                api_protocol="claude",
                system_prompt="你必须只回答 JSON",
            )

        self.assertEqual(result, "ok")
        self.assertEqual(captured["body"]["system"], "你必须只回答 JSON")
        self.assertEqual(captured["body"]["messages"], [{"role": "user", "content": "你好"}])
        self.assertNotIn("system", [msg.get("role") for msg in captured["body"]["messages"]])

    def test_claude_streaming_system_prompt_uses_top_level_system_field(self):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse(lines=[
                b'data: {"type":"content_block_delta","delta":{"text":"ok"}}\n\n',
            ])

        with patch.object(services, "resilient_urlopen", fake_urlopen):
            chunks = list(services.stream_direct_api_text(
                "你好",
                "https://api.anthropic.com",
                "test-token",
                api_model="claude-sonnet-4-6",
                api_protocol="claude",
                system_prompt="你必须只回答 JSON",
            ))

        self.assertEqual("".join(chunks), "ok")
        self.assertEqual(captured["body"]["system"], "你必须只回答 JSON")
        self.assertEqual(captured["body"]["messages"], [{"role": "user", "content": "你好"}])
        self.assertTrue(captured["body"]["stream"])

    def test_claude_vision_payload_uses_anthropic_image_blocks_and_top_level_system(self):
        captured = {}
        anthropic_content = [
            {"type": "text", "text": "看图"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "abc",
                },
            },
        ]

        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse({"content": [{"type": "text", "text": "ok"}]})

        with patch.object(services, "build_vision_payload_parts", return_value=([], anthropic_content)):
            with patch.object(services, "resilient_urlopen", fake_urlopen):
                result = services.call_direct_vision_api(
                    "看图",
                    ["./uploads/a.png"],
                    "https://api.anthropic.com",
                    "test-token",
                    api_model="claude-sonnet-4-6",
                    api_protocol="claude",
                    system_prompt="只描述图片",
                )

        self.assertEqual(result, "ok")
        self.assertEqual(captured["body"]["system"], "只描述图片")
        self.assertEqual(captured["body"]["messages"], [{"role": "user", "content": anthropic_content}])
        self.assertEqual(captured["body"]["messages"][0]["content"][1]["source"]["media_type"], "image/png")


class ChatCompletionsPayloadTest(unittest.TestCase):
    def test_openai_chat_completions_system_prompt_uses_system_message(self):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

        with patch.object(services, "resilient_urlopen", fake_urlopen):
            result = services.call_direct_chat_completions_text(
                "你好",
                "https://api.openai.com/v1",
                "test-token",
                api_model="gpt-5.5",
                system_prompt="只输出 JSON",
            )

        self.assertEqual(result, "ok")
        self.assertEqual(captured["body"]["messages"][0], {"role": "system", "content": "只输出 JSON"})
        self.assertEqual(captured["body"]["messages"][1], {"role": "user", "content": "你好"})
        self.assertNotIn("system", captured["body"])

    def test_openai_streaming_chat_completions_payload(self):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse(lines=[
                b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
                b'data: [DONE]\n\n',
            ])

        with patch.object(services, "resilient_urlopen", fake_urlopen):
            chunks = list(services.stream_direct_api_text(
                "你好",
                "https://api.openai.com",
                "test-token",
                api_model="gpt-5.5",
                api_protocol="completions",
                system_prompt="只输出 JSON",
            ))

        self.assertEqual("".join(chunks), "ok")
        self.assertEqual(captured["body"]["messages"][0]["role"], "system")
        self.assertEqual(captured["body"]["messages"][1], {"role": "user", "content": "你好"})
        self.assertTrue(captured["body"]["stream"])

    def test_openai_vision_payload_uses_image_url_blocks(self):
        captured = {}
        openai_content = [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ]

        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

        with patch.object(services, "build_vision_payload_parts", return_value=(openai_content, [])):
            with patch.object(services, "resilient_urlopen", fake_urlopen):
                result = services.call_direct_vision_api(
                    "看图",
                    ["./uploads/a.png"],
                    "https://api.openai.com",
                    "test-token",
                    api_model="gpt-5.5",
                    api_protocol="completions",
                    system_prompt="只描述图片",
                )

        self.assertEqual(result, "ok")
        self.assertEqual(captured["body"]["messages"][0], {"role": "system", "content": "只描述图片"})
        self.assertEqual(captured["body"]["messages"][1], {"role": "user", "content": openai_content})
        self.assertEqual(captured["body"]["messages"][1]["content"][1]["type"], "image_url")


if __name__ == "__main__":
    unittest.main()
