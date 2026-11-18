# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
# Cortex hub: every agent's panel on one HTTPS page, and the form that connects a new agent.
# Build and run with compose (compose.yaml, settings in .env); see docs/RUNBOOK.md §8.
FROM python:3.13-slim
LABEL org.opencontainers.image.title="Cortex hub" \
      org.opencontainers.image.authors="Adhonys Diaz" \
      org.opencontainers.image.vendor="MadLeak" \
      org.opencontainers.image.licenses="LicenseRef-PolyForm-Noncommercial-1.0.0" \
      org.opencontainers.image.description="Every agent's Cortex panel on one HTTPS page. By MadLeak."

RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssl \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system safe.directory '*'   # on Docker Desktop the vaults' owner differs from ours

# the engine: the template new vaults are made from, the hub and the installers it borrows the agent block from
WORKDIR /opt/cortex
COPY template-vault/ template-vault/
COPY skills/ skills/
COPY examples/ examples/
COPY installers/ installers/
COPY scripts/ scripts/

# HOME on the tmpfs: git keeps nothing there
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

USER 1000:1000
EXPOSE 8443 8080 5353/udp 9443 9080 5354/udp
HEALTHCHECK --interval=60s --timeout=5s CMD ["python3", "-c", "import socket; socket.create_connection(('127.0.0.1', 8443), 3)"]
ENTRYPOINT ["python3", "/opt/cortex/scripts/cortex_hub.py", "--root", "/vaults", "--data", "/vaults/.cortex-hub", "--host", "0.0.0.0", "--port", "8443"]
