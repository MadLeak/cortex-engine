# Cortex hub: every agent's panel on one HTTPS page, and the form that connects a new agent.
# Build and run with compose (compose.yaml, settings in .env); see docs/RUNBOOK.md §8.
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssl \
 && rm -rf /var/lib/apt/lists/*

# the engine: the template new vaults are made from, the hub and the installers it borrows the agent block from
WORKDIR /opt/cortex
COPY template-vault/ template-vault/
COPY skills/ skills/
COPY installers/ installers/
COPY scripts/ scripts/

# HOME on the tmpfs: git keeps nothing there. safe.directory: on Docker Desktop the vaults' owner differs from ours.
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=*

USER 1000:1000
EXPOSE 8443
HEALTHCHECK --interval=60s --timeout=5s CMD ["python3", "-c", "import socket; socket.create_connection(('127.0.0.1', 8443), 3)"]
ENTRYPOINT ["python3", "/opt/cortex/scripts/cortex_hub.py", "--root", "/vaults", "--data", "/vaults/.cortex-hub", "--host", "0.0.0.0", "--port", "8443"]
