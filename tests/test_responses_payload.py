import json
import sys
import types
import unittest
from unittest.mock import patch

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
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps({"output_text": "ok"}).encode("utf-8")


class ResponsesPayloadTest(unittest.TestCase):
    def test_github_url_stays_in_input_without_default_tools(self):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse()

        prompt = "请分析 https://github.com/openai/openai-python 这个项目"
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


if __name__ == "__main__":
    unittest.main()
