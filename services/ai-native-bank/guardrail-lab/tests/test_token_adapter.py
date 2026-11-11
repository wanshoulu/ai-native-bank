import os
import stat
import tempfile
import unittest
from pathlib import Path

from adapters.run_with_ai_harbor_token import AdapterConfigError, build_environment, load_token


class TokenAdapterTest(unittest.TestCase):
    def write_token(self, directory: str, value: str) -> Path:
        path = Path(directory) / "runtime-token"
        path.write_text(value, encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return path

    def test_loads_a_bounded_private_service_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_token(directory, "valid-runtime-service-token-123\n")
            self.assertEqual(load_token(path), "valid-runtime-service-token-123")

    def test_rejects_missing_malformed_and_oversized_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with self.assertRaises(AdapterConfigError):
                load_token(missing)
            for value in ["short", "valid-token-with whitespace", "x" * 513]:
                path = self.write_token(directory, value)
                with self.assertRaises(AdapterConfigError):
                    load_token(path)

    def test_builds_only_ai_harbor_compatible_model_environment(self):
        source = {
            "PATH": os.environ.get("PATH", ""),
            "QWEN_KEY": "must-not-pass",
            "DEEPSEEK_KEY": "must-not-pass",
            "PHOENIX_ENABLED": "True",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://external.invalid/v1/traces",
            "LANGSMITH_API_KEY": "must-not-pass",
            "LANGCHAIN_API_KEY": "must-not-pass",
        }
        result = build_environment(source, "valid-runtime-service-token-123")

        self.assertEqual(result["OPENAI_API_KEY"], "valid-runtime-service-token-123")
        self.assertEqual(result["OPENAI_BASE_URL"], "http://ai-harbor:3000/api/ai-bank/v1/internal/model/v1")
        self.assertEqual(result["OPENAI_MODEL"], "deepseek-chat")
        self.assertEqual(result["EMBEDDING_BASE_URL"], "http://ai-harbor:3000/api/ai-bank/v1/internal/model/v1")
        self.assertEqual(result["EMBEDDING_MODEL"], "text-embedding-v4")
        self.assertEqual(result["EMBEDDING_BATCH_SIZE"], "10")
        self.assertEqual(result["LLM_TEMPERATURE"], "0")
        self.assertEqual(result["PHOENIX_ENABLED"], "False")
        self.assertEqual(result["OTEL_SDK_DISABLED"], "true")
        self.assertEqual(result["LANGSMITH_TRACING"], "false")
        self.assertEqual(result["LANGCHAIN_TRACING_V2"], "false")
        self.assertNotIn("QWEN_KEY", result)
        self.assertNotIn("DEEPSEEK_KEY", result)
        self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", result)
        self.assertNotIn("LANGSMITH_API_KEY", result)
        self.assertNotIn("LANGCHAIN_API_KEY", result)


if __name__ == "__main__":
    unittest.main()
