# Runbook

<!-- AUTO-GENERATED: Generated from server.py, src/api/, config/config.py on 2026-06-04 -->

## Deployment Procedures

### Development Deployment

```bash
# Option 1: Direct Python
python server.py

# Option 2: Uvicorn with reload
uvicorn server:app --reload --host 0.0.0.0 --port 8000

# Option 3: Windows wrapper
start.bat

# Option 4: Linux wrapper (background)
./start.sh
```

### Production Deployment

```bash
# Option 1: Uvicorn with multiple workers
uvicorn server:app --host 0.0.0.0 --port 80 --workers 4

# Option 2: Gunicorn with Uvicorn workers (Linux recommended)
gunicorn -w 4 -k uvicorn.workers.UvicornWorker server:app

# Option 3: Linux background script
./start.sh
# Logs: tail -f logs/server.log
```

### Environment Variables for Production

```bash
export OPENAI_API_KEY=your-production-key
export DEBUG=False
export RELOAD=False
export HOST=0.0.0.0
export PORT=80
```

## Health Check Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `GET /health` | Health | Service health status |
| `GET /` | Info | Service info and available endpoints |

### Expected Health Response

```json
{
  "status": "healthy",
  "timestamp": "2026-06-04T10:00:00",
  "agent_status": "initialized"
}
```

## API Endpoints Overview

### Chat

| Endpoint | Method | Description |
|----------|--------|-------------|
| `POST /chat` | Chat | Standard chat with security guardrails |
| `POST /chat/stream` | Streaming | SSE streaming chat |
| `GET /conversations/{id}/history` | History | Retrieve conversation history |

### GuardRails

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /guardrails/rules` | List | Get all system + custom rules |
| `POST /guardrails/rules/reset` | Reset | Reset system rules to defaults |
| `POST /guardrails/rules/{name}` | Update | Add or update a rule |
| `DELETE /guardrails/rules/{name}` | Delete | Delete a custom rule |

### Whitelist

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /whitelist/rules` | List | Get all whitelist rules |
| `POST /whitelist/rules/reset` | Reset | Clear all whitelist rules |
| `POST /whitelist/rules/{name}` | Update | Add or update a whitelist rule |
| `DELETE /whitelist/rules/{name}` | Delete | Delete a whitelist rule |

### PII

| Endpoint | Method | Description |
|----------|--------|-------------|
| `POST /pii/detect` | Detect | Detect PII in text |
| `POST /pii/anonymize` | Anonymize | Anonymize/ redact PII in text |

### Configuration

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /config/guardrail` | Get | Get guardrail runtime config |
| `POST /config/guardrail` | Update | Update guardrail config |
| `GET /config/pii` | Get | Get PII detection config |
| `POST /config/pii` | Update | Update PII config |
| `GET /config/prompt_defense` | Get | Get prompt defense config |
| `POST /config/prompt_defense` | Update | Update prompt defense config |
| `POST /config/prompt_defense/reset` | Reset | Reset prompt defense to default |
| `GET /config/content_filter` | Get | Get content filter config |
| `POST /config/content_filter` | Update | Update content filter config |

### Content Filter

| Endpoint | Method | Description |
|----------|--------|-------------|
| `POST /content_filter/classify` | Classify | Classify text for harmful content |
| `GET /content_filter/categories` | List | Get all content filter categories |
| `POST /content_filter/categories` | Create | Create a custom category |
| `PUT /content_filter/categories/{name}` | Update | Update a category |
| `DELETE /content_filter/categories/{name}` | Delete | Delete a custom category |
| `POST /content_filter/categories/{name}/anchors` | Add Anchor | Add an anchor to a category |
| `DELETE /content_filter/categories/{name}/anchors/{id}` | Delete Anchor | Delete an anchor |
| `POST /content_filter/categories/reset` | Reset | Reset categories to defaults |
| `POST /content_filter/revectorize` | Revectorize | Recompute all anchor embeddings |

### Dashboard / Monitoring

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /statistics` | Statistics | Overall security statistics |
| `GET /dashboard/statistics?period=24h` | Statistics | Security stats with time filtering |
| `GET /dashboard/attack-types?period=24h` | Analytics | Attack type distribution |
| `GET /dashboard/blocked-by?period=24h` | Analytics | Blocked-by source distribution |
| `GET /dashboard/trends?period=24h` | Analytics | Time-series trends |
| `GET /dashboard/logs?limit=20` | Logs | Paginated, filtered request logs |

Period options: `24h`, `7d`, `30d`, `all`

### Utility

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /samples` | Samples | Get test sample data (white/black) |

## Monitoring

### Log Files

- Application logs: `logs/agent_service.log`
- Server logs (Linux background): `logs/server.log`
- Log rotation: 10MB per file, 5 backups retained
- Security events logged to the `security` logger

### Key Metrics

- Total requests
- Blocked requests (by guardrails, llm, pii, content_filter)
- Attack detection rate
- Safety rate percentage

Access via `GET /statistics` or `GET /dashboard/statistics?period=24h`

## Common Issues and Fixes

### Service Won't Start

1. Check Python version (3.10+ required)
2. Verify all dependencies installed: `pip install -r requirements.txt`
3. Check port availability: `lsof -i :8000` (Linux) / `netstat -ano | findstr :8000` (Windows)
4. Verify API key is set in `.env`
5. Check `logs/agent_service.log` and `logs/server.log` for startup errors

### OpenAI API Errors

1. Verify `OPENAI_API_KEY` is correct
2. Check `OPENAI_BASE_URL` connectivity
3. Confirm `OPENAI_MODEL` is available on your provider
4. Check account/credit balance

### Frontend Can't Connect

1. Verify backend is running (`GET /health`)
2. Check `ALLOWED_ORIGINS` in `.env` includes the frontend URL
3. Check browser console for CORS errors
4. Verify `STATIC_DIR` path is correct for serving frontend assets

### Database Issues

- SQLite database auto-creates at `data/finance_guardrail.db`
- Checkpointer auto-creates at `data/checkpoints.db`
- If corruption occurs, delete the file and restart (data will be lost)

### spaCy Model Missing

```bash
# Download models if not present
python -m spacy download zh_core_web_sm --target ./data/
python -m spacy download en_core_web_sm --target ./data/
```

## Rollback Procedures

1. Stop the service
2. Restore previous code version from git: `git checkout <commit>`
3. Restart the service
4. Verify health endpoint responds correctly: `curl http://localhost/health`

## Alerting

No automated alerting is configured by default. Monitor:

- `logs/agent_service.log` for ERROR level messages
- `GET /health` endpoint for `agent_status` != `initialized`
- Rapid increase in `blocked_requests` in `/dashboard/statistics`
- `GET /dashboard/logs?is_attack=true` for attack spikes

<!-- END AUTO-GENERATED -->
