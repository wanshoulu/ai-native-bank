FROM python:3.11-slim

ARG UV_VERSION=0.8.22

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_CACHE_DIR=/tmp/guardrail-uv-cache \
    PYTHONPYCACHEPREFIX=/tmp/guardrail-pycache \
    PYTEST_ADDOPTS="-p no:cacheprovider" \
    PYTHONPATH=/opt

RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app

COPY upstream/pyproject.toml upstream/uv.lock upstream/readme.md ./
RUN uv sync --frozen

COPY upstream/ ./

RUN useradd --create-home --uid 10001 guardrail \
    && chown -R guardrail:guardrail /app

COPY --chown=10001:10001 adapters/ /opt/guardrail-adapters/
COPY --chown=10001:10001 adapters/ /opt/adapters/
COPY --chown=10001:10001 cases/ /opt/guardrail-cases/
COPY --chown=10001:10001 evaluation/ /opt/evaluation/
COPY --chown=10001:10001 tests/ /opt/guardrail-tests/

USER 10001:10001

CMD ["uv", "run", "--frozen", "pytest", "test/", "-v"]
