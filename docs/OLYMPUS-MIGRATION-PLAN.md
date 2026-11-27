# Plan: el hub de Cortex en Olympus (Coolify en Athena)

Pedido por Adhonys el 2026-11-27: todas las vaults viven en Olympus, en un contenedor que Coolify administra y
despliega, sin puertos expuestos a la LAN, servido solo por una URL común en NPM (Janus). Primer uso: el Cortex de
Great Sage que consulta el agente de soporte de n8n (Great Sage S10, parte 3).

## Estado de partida (inventario del 2026-11-27)

- El hub corre en Docker en el PC (`cortex-hub-1`, imagen 2.26.0) y solo escucha en `127.0.0.1:8790`.
- Hay ocho vaults en `~/Vaults`: agents, claude, codex, copilot, deepseek, gemini, hermes y opencode. Todas son repos git limpios en `main`, con su remoto privado `MadLeak/<nombre>-cortex`.
- Clientes por red (MCP con token por vault):
  - Claude Code: `.claude.json`.
  - Codex: `.codex/config.toml`.
  - Copilot: `.copilot/mcp-config.json`.
  - Gemini: `.gemini/settings.json`.
  - OpenCode: `.config/opencode/opencode.json`.
  - Hermes: `AppData/Local/hermes/config.yaml` y el perfil `tars`.
- Clientes que leen los archivos del disco:
  - El hook SessionStart de Claude (`.claude/hooks/cortex_start.py`).
  - El hook de Hermes (`agent-hooks/cortex_hot.py`).
  - `Dev/hermes/agentes-en-vivo/observer/observer.py`.
  - Obsidian.
- El hub en el PC monta `~/.claude/projects` (solo lectura) y `~/.cortex-secrets/push.token`. Para REM usa Strata en `host.docker.internal:8080`.

## Arquitectura destino

- **Una app de Coolify en Athena, de un solo contenedor** (build pack Dockerfile, desde `MadLeak/cortex-engine`):
  - El paso `vaults_init` corre al arrancar, dentro del mismo contenedor.
  - Así el flujo de n8n *Coolify Publisher* crea el host de NPM solo, porque hoy publica únicamente apps de un contenedor con `ports_mappings`.
  - El hub corre en modo HTTP. NPM pone el TLS con el certificado `*.madleak.com`.
- **Red**:
  - El puerto del contenedor se publica en Athena, pero `docker-user-allow` lo abre **solo a Janus (10.0.0.22)**.
  - Desde la LAN no se llega directo, solo por `https://cortex.madleak.com`.
  - Igual que Great Sage, solo responde a LAN y VPN, nunca a Internet.
  - Cada vault sigue protegida por su propio token.
- **Datos**:
  - Un volumen persistente de Coolify para `/vaults`, que entra en el backup nocturno de Athena.
  - Se siembra con `git clone` de los ocho remotos privados.
  - Lo aprendido vuelve a GitHub con *Save to remote*, usando `push.token` como secreto de Coolify.
- **Secretos** (los pone Adhonys en Coolify): `push.token`, `strata.key` y la identidad git (`CORTEX_GIT_NAME` / `CORTEX_GIT_EMAIL`).
- **Strata**: `CORTEX_STRATA` apunta a Alastor en vez de `host.docker.internal`.
- **`~/.claude/projects`**: no existe en el servidor y deja de montarse. Lo que dependa de él, la importación de memoria, queda solo en el PC.
- **El PC**:
  - Conserva un clon de cada vault, solo para leer: Obsidian, los hooks y el observer.
  - Una tarea programada hace `git pull` cada 10 minutos.
  - Nadie escribe ahí. Escribir solo se hace por el hub.

## Fases (cada una con su vuelta atrás)

1. **Preparar sin tocar nada vivo.**
   - Rama `feat/single-container` en cortex-engine con:
     - el Dockerfile de un contenedor;
     - el modo HTTP detrás de un proxy;
     - `.gitattributes` con `* text=auto eol=lf`, contra el riesgo de finales de línea entre Windows y Linux.
   - Tests en el contenedor: `docker compose run --rm test`.
   - *Vuelta atrás*: no hay nada que revertir.
2. **Congelar y respaldar.**
   - En cada vault: `git fetch` y comprobar que está limpia y al día.
   - Un tar de `~/Vaults`, incluidos los tokens de `_brain/state`, archivado en `C:\Users\AdhonysDiaz\archived\cortex\2026-11-27\`.
   - El hub del PC sigue funcionando.
3. **Desplegar en Athena en paralelo.**
   - App nueva en Coolify, con `cortex.madleak.com` y el volumen.
   - Las vaults se clonan desde GitHub y los tokens `mcp.token` se copian desde el backup, para que los clientes no cambien de token.
   - El Publisher crea el host en NPM. Se comprueba que:
     - `https://cortex.madleak.com/health` responde;
     - `brief` funciona en la vault de Claude;
     - el acceso solo pasa por Janus.
   - Mientras tanto, el servidor no recibe escrituras.
   - *Vuelta atrás*: borrar la app nueva, sin efecto sobre el PC.
4. **Cambio de lado, un agente a la vez.**
   - Se para el hub del PC con `docker compose stop`, sin `down -v`.
   - Se cambia la URL MCP de Claude a `https://cortex.madleak.com/mcp/Claude%20Cortex?by=claude` y se prueba `brief`, `learn` y *Save to remote*.
   - Después siguen Codex, Copilot, Gemini, OpenCode y Hermes.
   - *Vuelta atrás*: devolver las URLs y `docker compose start` en el PC. Las vaults del PC siguen intactas.
5. **Clientes de archivo.**
   - El clon de solo lectura con `git pull` programado.
   - `cortex_start.py` y `cortex_hot.py` siguen leyendo `~/Vaults`, que ahora es ese clon.
   - Se comprueba que el SessionStart de Claude muestra el Hot list.
6. **Retiro del hub del PC.**
   - Tras una semana sin incidentes, la carpeta `cortex-engine/.env` y el contenedor parado se archivan.
   - No se borra nada.
7. **Great Sage Cortex**, que es S10 parte 3 y empieza aquí.
   - Una vault nueva `great-sage-cortex` en el servidor, con remoto privado.
   - Su semilla: ayuda, `docs/` y notas de versión.
   - n8n la consulta con su propio token.
   - Las respuestas confirmadas se aprenden ahí.

## Pendiente de decidir o vigilar

- **Ampliar el Coolify Publisher a las apps compose.** Great Sage y Authentik hoy se publican a mano. Es opcional y va aparte.
- **Que dos hubs no escriban a la vez.** Si el PC se enciende con el hub viejo, debe quedar parado: `restart: "no"` antes del cambio de lado.
- **Clientes nunca probados.** Codex, Gemini y OpenCode no se han probado con un hub remoto (`RUNBOOK.md` §11). Se prueba cada uno en la fase 4.
- **Panel de ajustes.** Solo se ve en local (`RUNBOOK.md:380`). Para *Save to remote* hace falta otro camino en el servidor, por ejemplo un push programado. Se revisa en la fase 1.
