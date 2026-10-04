FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REPO_AGENT_WORKSPACE_ROOT=/workspace \
    REPO_AGENT_PROVIDER=mock \
    REPO_AGENT_HOST=0.0.0.0 \
    REDIS_URL=redis://redis:6379/0

WORKDIR /app
COPY pyproject.toml README.md LICENSE.md THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir '.[service]' \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /workspace \
    && chown appuser:appuser /workspace

USER appuser
EXPOSE 8000
CMD ["repo-agent-api"]
