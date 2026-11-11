# Environment Variables

<!-- AUTO-GENERATED: Generated from .env.example on 2026-06-04 -->

This document describes all environment variables used by the Finance Guardrail service.

## Required Variables

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `OPENAI_API_KEY` | **Yes** | OpenAI-compatible API key for LLM access | `sk-xxxxxxxxxxxxxxxx` |

## AI / LLM Configuration

| Variable | Default | Description | Example |
|----------|---------|-------------|---------|
| `OPENAI_BASE_URL` | `https://api.siliconflow.cn/v1/` | Base URL for OpenAI-compatible API | `https://api.openai.com/v1/` |
| `OPENAI_MODEL` | `your-model` | LLM model identifier | `Qwen/Qwen3-8B`, `gpt-4o` |
| `EMBEDDING_BASE_URL` | `https://api.siliconflow.cn/v1/` | Base URL for embedding API | `https://api.openai.com/v1/` |
| `EMBEDDING_MODEL` | `BAAI/bge-large-zh-v1.5` | Embedding model identifier | `text-embedding-3-small` |
| `LLM_TEMPERATURE` | `0.7` | LLM sampling temperature | `0.0` – `1.0` |

## Server Configuration

| Variable | Default | Description | Example |
|----------|---------|-------------|---------|
| `HOST` | `0.0.0.0` | Server bind address | `127.0.0.1`, `0.0.0.0` |
| `PORT` | `80` | Server listen port | `8000`, `8080`, `80` |
| `DEBUG` | `False` | Enable debug mode | `True`, `False` |
| `RELOAD` | `False` | Enable auto-reload on code changes | `True`, `False` |
| `ALLOWED_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | CORS origins (comma-separated, no spaces) | `http://localhost:5173` |
| `MAX_REQUEST_SIZE` | `102400` | Max request body size in bytes | `102400` (100KB) |

## Database & Storage

| Variable | Default | Description | Example |
|----------|---------|-------------|---------|
| `DATABASE_URL` | `sqlite+aiosqlite:///data/finance_guardrail.db` | Async SQLite database URL | `sqlite+aiosqlite:///data/finance_guardrail.db` |
| `CHECKPOINTER_DB_PATH` | `./data/checkpoints.db` | LangGraph checkpoint SQLite path | `./data/checkpoints.db` |
| `STATIC_DIR` | `./static` | Static files directory | `./static` |
| `GUARDRAILS_CUSTOM_PATH` | `./data/guardrails_custom.json` | Custom guardrails rules file | `./data/guardrails_custom.json` |
| `SAMPLES_FILE_PATH` | `./data/samples.json` | Test samples JSON file | `./data/samples.json` |
| `SPACY_PATH` | `./data/zh_core_web_sm` | Chinese spaCy model path | `./data/zh_core_web_sm` |
| `SPACY_EN_PATH` | `./data/en_core_web_sm` | English spaCy model path | `./data/en_core_web_sm` |

## Logging

| Variable | Default | Description | Example |
|----------|---------|-------------|---------|
| `LOG_LEVEL` | `INFO` | Logging verbosity | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_DIR` | `./logs` | Log files directory | `./logs` |
| `LOG_FILENAME` | `agent_service.log` | Main log filename | `agent_service.log` |
| `LOG_MAX_BYTES` | `10485760` | Max log file size before rotation (bytes) | `10485760` (10MB) |
| `LOG_BACKUP_COUNT` | `5` | Number of rotated log backups to keep | `5` |
| `LOG_STR_MAX` | `100` | Max string length in log entries | `100` |

## Streaming & Performance

| Variable | Default | Description | Example |
|----------|---------|-------------|---------|
| `STREAM_YIELD_INTERVAL` | `0.025` | SSE yield interval in seconds | `0.025` |
| `EMBEDDING_BATCH_SIZE` | `32` | Batch size for embedding operations | `32` |

## Content Filter / Classifier

| Variable | Default | Description | Example |
|----------|---------|-------------|---------|
| `CLASSIFIER_MIN_TEXT_LENGTH` | `8` | Minimum text length for classification | `8` |
| `CLASSIFIER_ENGLISH_RATIO_THRESHOLD` | `0.5` | English text ratio threshold | `0.5` |
| `CLASSIFIER_DEFAULT_THRESHOLD` | `0.75` | Default classification threshold | `0.75` |
| `CONTENT_FILTER_LOG_THRESHOLD` | `0.5` | Content filter logging threshold | `0.5` |

## GuardRails

| Variable | Default | Description | Example |
|----------|---------|-------------|---------|
| `GUARDRAILS_REJECTION_MESSAGE` | `安全警告：检测到非法输入...` | Default rejection message | `Request blocked by security policy` |

## Configuration Notes

- **Production**: Set `DEBUG=False` and `RELOAD=False`
- **Development**: Set `DEBUG=True` and `RELOAD=True`
- The service uses `python-dotenv` to load `.env` file automatically if present
- Default values are defined in `config/config.py` and used when environment variables are not set
- The service uses **SQLite via SQLAlchemy (async with `aiosqlite`)**; no separate database server is required

<!-- END AUTO-GENERATED -->
