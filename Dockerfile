# The agents service (HTTP API, approvals page, metrics) as a container.
# Build:  docker build -t governed-agents .
# Run:    docker run --rm -p 127.0.0.1:8090:8090 -v govagents-data:/data \
#           -e GOVAGENTS_API_TOKENS="alice:requester:...,bob:approver:..." \
#           -e GOVAGENTS_PROVIDER=openai_compatible -e GOVAGENTS_BASE_URL=http://gateway:8080/v1 \
#           -e GOVAGENTS_API_KEY=... governed-agents
# Runs, the audit trail, approvals and the demo outbox live on the /data volume.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GOVAGENTS_DATA_DIR=/data \
    GOVAGENTS_SCENARIOS_DIR=/app/scenarios

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[server,tracing]"
COPY scenarios ./scenarios
COPY examples ./examples

RUN useradd --create-home --uid 10001 appuser && mkdir -p /data && chown appuser /data
USER appuser
VOLUME ["/data"]

EXPOSE 8090
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=2)"
CMD ["govagents", "serve", "--host", "0.0.0.0", "--port", "8090"]
