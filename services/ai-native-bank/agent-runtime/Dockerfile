FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY services/ai-native-bank/agent-runtime/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --requirement requirements.txt

COPY services/ai-native-bank/agent-runtime/app ./app
COPY services/ai-native-bank/agent-runtime/tests ./tests
COPY data/ai-native-bank/intent-graph/graph.ccb.intents.v1.0.0.json ./data/ai-native-bank/intent-graph/graph.ccb.intents.v1.0.0.json
COPY data/ai-native-bank/intent-graph/graph.ccb.intents.v2.1.0.json ./data/ai-native-bank/intent-graph/graph.ccb.intents.v2.1.0.json
COPY data/ai-native-bank/intent-graph/skill-bindings.v1.json ./data/ai-native-bank/intent-graph/skill-bindings.v1.json
COPY data/ai-native-bank/intent-graph/intent-semantic-index.v1.json ./data/ai-native-bank/intent-graph/intent-semantic-index.v1.json
COPY data/ai-native-bank/faq/ccb-mobile-bank-faq.v1.json ./data/ai-native-bank/faq/ccb-mobile-bank-faq.v1.json
COPY data/ai-native-bank/registries ./data/ai-native-bank/registries
COPY data/ai-native-bank/skills ./data/ai-native-bank/skills
COPY data/ai-native-bank/guardrail ./data/ai-native-bank/guardrail
COPY data/agent-memory/predicate-policies.v1.json ./data/agent-memory/predicate-policies.v1.json
COPY data/eval/ccb-intent-conformance.v1.json ./data/eval/ccb-intent-conformance.v1.json
COPY data/eval/ccb-skill-runtime-contracts.v1.json ./data/eval/ccb-skill-runtime-contracts.v1.json

RUN useradd --create-home --uid 10001 agent-runtime \
    && mkdir -p /app/logs /app/agent-workspaces \
    && chown -R agent-runtime:agent-runtime /app/logs /app/agent-workspaces

USER agent-runtime

EXPOSE 18120

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18120", "--no-access-log"]
