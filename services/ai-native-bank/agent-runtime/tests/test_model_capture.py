import json
import unittest

import httpx

from app.model_capture import (
    MAX_TEXT_BYTES,
    ModelCallRecorder,
    RecordingModel,
    _bounded,
    clear_run_scope,
    current_run_id,
    set_run_scope,
)


class ModelCaptureTests(unittest.IsolatedAsyncioTestCase):
    def _recorder(self, captured, *, status=200):
        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(status)

        return ModelCallRecorder(
            endpoint="http://node.test/api/ai-bank/v1/internal/agent-run-model-calls",
            service_token="token-model-capture-0001",
            model_name="deepseek-chat",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    def test_run_scope_roundtrip(self):
        token = set_run_scope("run-1")
        try:
            self.assertEqual(current_run_id(), "run-1")
        finally:
            clear_run_scope(token)
        self.assertIsNone(current_run_id())

    async def test_recorder_posts_bounded_payload(self):
        captured = []
        recorder = self._recorder(captured)
        token = set_run_scope("run-abc")
        try:
            await recorder.record(
                phase="user_intent",
                messages_text='[{"role":"user","content":"你好"}]' + "x" * 20000,
                response_text='{"content":"ok"}',
                tools_count=2,
                duration_ms=812,
                status="ok",
            )
        finally:
            clear_run_scope(token)
        self.assertEqual(len(captured), 1)
        item = captured[0]
        self.assertEqual(item["agentRunId"], "run-abc")
        self.assertEqual(item["phase"], "user_intent")
        self.assertEqual(item["model"], "deepseek-chat")
        self.assertLessEqual(len(item["promptJson"].encode("utf-8")), MAX_TEXT_BYTES)
        self.assertEqual(item["toolsCount"], 2)
        self.assertEqual(item["status"], "ok")

    async def test_recorder_skips_without_run_scope(self):
        captured = []
        recorder = self._recorder(captured)
        await recorder.record(
            phase="user_intent",
            messages_text="[]",
            response_text="{}",
            tools_count=0,
            duration_ms=1,
            status="ok",
        )
        self.assertEqual(captured, [])

    async def test_recorder_falls_back_to_resolver(self):
        captured = []
        recorder = self._recorder(captured)
        recorder.bind_run_id_fallback(lambda: "run-from-invoker")
        await recorder.record(
            phase="business_planning",
            messages_text="[]",
            response_text="{}",
            tools_count=0,
            duration_ms=3,
            status="ok",
        )
        self.assertEqual(captured[0]["agentRunId"], "run-from-invoker")

    async def test_recorder_swallows_gateway_rejection(self):
        captured = []
        recorder = self._recorder(captured, status=409)
        token = set_run_scope("run-xyz")
        try:
            await recorder.record(
                phase="user_intent",
                messages_text="[]",
                response_text="{}",
                tools_count=0,
                duration_ms=1,
                status="ok",
            )
        finally:
            clear_run_scope(token)
        self.assertEqual(len(captured), 1)

    def test_bounded_truncates_deep_structures(self):
        deep = current = {}
        for _ in range(20):
            current["child"] = {}
            current = current["child"]
        current["leaf"] = "y" * 1000
        bounded = _bounded(deep)
        text = json.dumps(bounded, default=str)
        self.assertLess(len(text), 4000)

        wide = {"k%d" % i: "v" * 100 for i in range(100)}
        self.assertEqual(len(_bounded(wide)), 32)

        long_list = ["x" * 100] * 100
        self.assertEqual(len(_bounded(long_list)), 32)

    def test_bounded_handles_pydantic_and_primitives(self):
        from pydantic import BaseModel

        class Item(BaseModel):
            role: str
            content: str

        bounded = _bounded([Item(role="user", content="hi"), 3, None, 1.5, True])
        self.assertEqual(bounded[0], {"role": "user", "content": "hi"})
        self.assertEqual(bounded[1:], [3, None, 1.5, True])

    def test_recording_model_phase_validation(self):
        from openjiuwen.core.foundation.llm import (
            ModelClientConfig,
            ModelRequestConfig,
        )

        captured = []
        recorder = self._recorder(captured)
        client_config = ModelClientConfig(
            client_provider="openai",
            api_key="test-key-model-capture",
            api_base="http://model.test/v1",
            max_retries=0,
        )
        request_config = ModelRequestConfig(model="deepseek-chat")
        with self.assertRaises(ValueError):
            RecordingModel(
                client_config, request_config, recorder=recorder, phase="BAD PHASE"
            )
        RecordingModel(
            client_config, request_config, recorder=recorder, phase="user_intent"
        )

    async def test_invoke_reports_error_without_breaking_raise(self):
        captured = []
        recorder = self._recorder(captured)
        token = set_run_scope("run-err")
        model = RecordingModel.__new__(RecordingModel)
        model._recorder = recorder
        model._phase = "user_intent"
        try:
            with self.assertRaises(AttributeError):
                await RecordingModel.invoke(
                    model, [{"role": "user", "content": "hi"}], tools=[]
                )
        finally:
            clear_run_scope(token)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["status"], "error")
        self.assertIn("AttributeError", captured[0]["responseText"])
        self.assertEqual(captured[0]["phase"], "user_intent")

    async def test_stream_assembles_tool_call_fragments(self):
        from unittest import mock

        from openjiuwen.core.foundation.llm import Model

        class _ToolCall:
            def __init__(self, index=0, id="", name="", arguments=""):
                self.index = index
                self.id = id
                self.name = name
                self.arguments = arguments

        class _Chunk:
            def __init__(self, content="", tool_calls=None):
                self.content = content
                self.tool_calls = tool_calls

        chunks = [
            _Chunk("Hel"),
            _Chunk("lo"),
            _Chunk("", [_ToolCall(0, id="call_1", name="get_wealth_context")]),
            _Chunk("", [_ToolCall(0, arguments='{"customerId')]),
            _Chunk("", [_ToolCall(0, arguments='":"c1"}')]),
            _Chunk("", [_ToolCall(1, id="call_2", name="get_other", arguments="{}")]),
        ]

        async def fake_stream(self, messages=None, *, tools=None, **kwargs):
            for chunk in chunks:
                yield chunk

        captured = []
        recorder = self._recorder(captured)
        token = set_run_scope("run-stream")
        try:
            with mock.patch.object(Model, "stream", fake_stream):
                model = RecordingModel.__new__(RecordingModel)
                model._recorder = recorder
                model._phase = "business_planning"
                collected = []
                async for chunk in RecordingModel.stream(
                    model, [{"role": "user", "content": "hi"}], tools=[{"type": "function"}]
                ):
                    collected.append(chunk)
        finally:
            clear_run_scope(token)
        self.assertEqual([c.content for c in collected], [c.content for c in chunks])
        self.assertEqual(len(captured), 1)
        item = captured[0]
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["toolsCount"], 1)
        self.assertIn("Hello", item["responseText"])
        payload = json.loads(item["responseText"].split("\n", 1)[1])
        self.assertEqual(
            payload,
            {
                "toolCalls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "name": "get_wealth_context",
                        "arguments": '{"customerId":"c1"}',
                    },
                    {
                        "index": 1,
                        "id": "call_2",
                        "name": "get_other",
                        "arguments": "{}",
                    },
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()
