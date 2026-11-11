"""Start an isolated upstream command with an AI Harbor service token.

The adapter never reads or forwards upstream provider keys. It only maps the
private AI Harbor service token to the OpenAI-compatible variables expected by
the unmodified third-party project.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Mapping


AI_HARBOR_MODEL_BASE_URL = "http://ai-harbor:3000/api/ai-bank/v1/internal/model/v1"
DEFAULT_TOKEN_FILE = "/run/ai-bank-secrets/runtime-token"
SENSITIVE_PROVIDER_KEYS = {
    "DEEPSEEK_KEY",
    "QWEN_KEY",
    "GUARDRAILS_API_KEY",
}
DISALLOWED_OBSERVABILITY_PREFIXES = ("OTEL_", "LANGSMITH_", "LANGCHAIN_")


class AdapterConfigError(RuntimeError):
    """Raised when the isolated runtime identity cannot be loaded safely."""


def load_token(path: str | Path) -> str:
    token_path = Path(path)
    try:
        file_stat = token_path.stat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size < 16 or file_stat.st_size > 513:
            raise AdapterConfigError("AI Harbor service identity is invalid")
        if os.name == "posix" and file_stat.st_mode & 0o077:
            raise AdapterConfigError("AI Harbor service identity permissions are invalid")
        raw = token_path.read_bytes()
    except AdapterConfigError:
        raise
    except OSError as error:
        raise AdapterConfigError("AI Harbor service identity is unavailable") from error

    if len(raw) > 513:
        raise AdapterConfigError("AI Harbor service identity is invalid")
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise AdapterConfigError("AI Harbor service identity is invalid") from error
    if len(token) < 16 or len(token) > 512 or any(character.isspace() for character in token):
        raise AdapterConfigError("AI Harbor service identity is invalid")
    return token


def build_environment(source: Mapping[str, str], token: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in source.items()
        if key not in SENSITIVE_PROVIDER_KEYS
        and not key.startswith(DISALLOWED_OBSERVABILITY_PREFIXES)
    }
    environment.update(
        {
            "OPENAI_API_KEY": token,
            "OPENAI_BASE_URL": AI_HARBOR_MODEL_BASE_URL,
            "OPENAI_MODEL": "deepseek-v4-flash",
            "EMBEDDING_BASE_URL": AI_HARBOR_MODEL_BASE_URL,
            "EMBEDDING_MODEL": "qwen3.7-text-embedding-flash",
            "EMBEDDING_BATCH_SIZE": "10",
            "LLM_TEMPERATURE": "0",
            "PHOENIX_ENABLED": "False",
            "OTEL_SDK_DISABLED": "true",
            "LANGSMITH_TRACING": "false",
            "LANGCHAIN_TRACING_V2": "false",
        }
    )
    return environment


def main(arguments: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if arguments is None else arguments)
    if not command:
        raise AdapterConfigError("No upstream command configured")
    token_file = os.environ.get("AI_BANK_AGENT_RUNTIME_TOKEN_FILE", DEFAULT_TOKEN_FILE)
    token = load_token(token_file)
    os.execvpe(command[0], command, build_environment(os.environ, token))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AdapterConfigError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(78)
