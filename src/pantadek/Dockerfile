FROM node:22-bookworm-slim AS runtime
ARG CODEX_VERSION=0.167.0
ARG PLAYWRIGHT_MCP_VERSION=0.0.84
RUN apt-get update && apt-get install -y --no-install-recommends chromium python3 ca-certificates fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @openai/codex@${CODEX_VERSION} @playwright/mcp@${PLAYWRIGHT_MCP_VERSION} \
    && npm cache clean --force
ENV HOME=/tmp/pantadek-home CODEX_HOME=/tmp/codex PYTHONDONTWRITEBYTECODE=1
WORKDIR /tmp/pantadek-home
COPY worker.py /opt/pantadek/worker.py
USER node
ENTRYPOINT ["python3", "/opt/pantadek/worker.py"]
