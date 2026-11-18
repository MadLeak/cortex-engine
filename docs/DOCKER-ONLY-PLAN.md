# Plan: Cortex solo en Docker (sin tocar el host)

Rama `docker-only` · worktree `cortex-engine-docker` · borrador para discutir, nada implementado aún.

## Objetivo

Correr todo Cortex (hub, motor, MCP, creación de vaults) dentro de contenedores. El host solo pone Docker Desktop,
una carpeta de vaults y, como mucho, una línea de config MCP en el agente. Nada de admin, DNS, certificados ni Python.

## Qué toca hoy el host

| Pieza | Qué hace en el host | Destino |
|---|---|---|
| `install.ps1` | Exige admin, reglas NRPT de DNS para `cortex.local`, mete la CA en `LocalMachine\Root` | Quitar del camino por defecto; queda como opción "LAN" |
| `installers/setup_host.sh` | Lo mismo en Linux/macOS (nmcli, `/etc/resolver`, keychain, nssdb) | Igual: opcional |
| `compose.yaml` puertos | Ocupa 443, 80 y 53/udp en 127.0.0.153 | Un solo puerto alto en `127.0.0.1` (p. ej. 8790), sin DNS |
| Prompt del hub (`agent_prompt`) | Le dice al agente que corra `python brain.py` en el host | Nuevo modo: el agente usa el MCP del contenedor |
| `wire_agent.py` | Escribe un bloque en `~/.claude/CLAUDE.md` (o el del agente) | Se reemplaza por `claude mcp add ...` (config del agente, no del sistema) |
| Hook `pre-commit` del vault | Busca Python 3.10+ en el host para validar | Commits desde el contenedor, o hook que cae a `docker compose run` |
| `new_vault.py`, `doctor`, `upgrade_engine.py` | Corren con el Python del host | Servicio `cli` del compose |
| Bind mount `%USERPROFILE%\Vaults` | Carpeta normal | Se mantiene (Obsidian la necesita) |

## Arquitectura propuesta

```
compose.yaml
  hub   cortex_hub.py   -> 127.0.0.1:8790   (HTTP, sin --data: sin CA, sin DNS)
  mcp   cortex_mcp.py --http 0.0.0.0:8777 --vaults /vaults  -> 127.0.0.1:8777
  cli   perfil "tools": brain.py / new_vault.py / git, `docker compose run --rm cli ...`
volumen: ${CORTEX_VAULTS} -> /vaults  (misma imagen para los tres)
```

El agente se conecta así, sin Python en el host:

```bash
claude mcp add --transport http cortex "http://127.0.0.1:8777/mcp/<vault>?by=claude-code"
```

## Fases

1. **Línea base en Linux.** Correr los 31 tests del motor y los de `scripts/` dentro de la imagen. Esperado: los 2
   fallos de Windows (`PermissionError` en escrituras concurrentes) desaparecen. Añadir `compose run --rm test`.
2. **Hub solo-localhost.** Perfil por defecto sin `--data`, sin `--dns-port`, puerto alto en `127.0.0.1`.
   Verificar cómo arma el hub las URLs de panel sin dominio ni TLS (`cortex_hub.py:347-351`) y que el chequeo de Host
   (`allowed`, línea 280) acepte `localhost:8790`.
3. **Servicio MCP.** Nuevo servicio con `cortex_mcp.py --http`. **Bloqueo confirmado:** `serve_http`
   (`cortex_mcp.py:817`) sale si el host no es loopback, y en el contenedor hay que escuchar en `0.0.0.0`. Añadir un
   flag explícito (p. ej. `--in-container`) que permita `0.0.0.0` solo cuando el puerto se publica en `127.0.0.1`, y
   mantener el chequeo de Origin y de Host. Tests en `test_cortex_mcp.py`. Ver también "Más allá de MCP".
4. **CLI en contenedor.** Servicio `cli` + un shim `cortex.ps1` / `cortex.sh` que solo llama a
   `docker compose run --rm cli "$@"` (crear vault, doctor, recall, commit). Identidad git por variables de `.env`.
5. **Hooks de git.** Decidir: (a) los commits siempre por `cortex commit` dentro del contenedor, o (b) el `pre-commit`
   prueba Python local y si no hay, llama a Docker. (a) es más limpio; (b) deja usar Obsidian Git / git del host.
6. **Prompt del hub en modo MCP.** `agent_prompt` con un `kind` nuevo que devuelve el `claude mcp add ...` (y los
   equivalentes de Codex/Gemini) en vez de rutas a `brain.py`. El bloque de instrucciones puede ir en la descripción
   de la herramienta MCP, que ya existe (`describe()`), y dejar `~/.claude/CLAUDE.md` intacto.
7. **Instalador.** `install.ps1` pasa a ser: comprobar Docker, escribir `.env`, crear la carpeta, `compose up`.
   Sin admin. DNS + CA + LAN se mueven a `install.ps1 -Lan` (lo de hoy, opt-in).
8. **Docs.** README quickstart "solo Docker" primero; RUNBOOK §1, §4 y §8 actualizados.

## Riesgos a vigilar

- **Bind mounts en Windows:** rendimiento de muchos archivos pequeños y permisos/propietario (ya hay
  `safe.directory '*'`). Probar con 1.000 nodos.
- **Fin de línea:** vaults editados desde Obsidian en Windows y commiteados en Linux; revisar `.gitattributes`.
- **Hook ejecutable:** el bit `+x` de `.githooks/*` sobre NTFS.
- **Sesión automática del motor:** MCP y CLI en contenedores distintos comparten budget por archivo; validar que el
  lock del vault funciona entre contenedores sobre el mismo bind mount.
- **Licencia:** sin cambios, PolyForm Noncommercial.

## Decisiones tomadas (2026-11-18)

1. **Solo MCP.** Se elimina el modo "python en el host" del camino por defecto (`wire_agent.py`, rutas a `brain.py`).
2. **Bind mount en `~/Vaults`** (`%USERPROFILE%\Vaults` en Windows), la misma ruta de siempre; Obsidian la ve.
3. **HTTP en `127.0.0.1`.** HTTPS sin aviso exige instalar una CA en el host, que es justo lo que se evita; los
   navegadores ya tratan `localhost`/`127.0.0.1` como contexto seguro. HTTPS queda solo para el modo LAN opcional.
4. **Commits:** los del agente, dentro del contenedor con la identidad del agente; los del humano (Obsidian, git de
   Windows) con sus propios datos. El `pre-commit` de esos commits humanos llama a `docker compose run` para validar,
   así que Docker es la única dependencia en el host.
5. **Identidad del agente:** sus commits en el vault van con su propio nombre (p. ej.
   `claude-code <claude-code@cortex.local>`), nunca con el del humano.
6. **Un commit por tarea:** el agente commitea al cerrar la tarea con `finish`, no en cada `learn`.
7. **Hub y MCP, un solo servicio:** un proceso y un contenedor que sirve paneles, crea vaults y registra agentes,
   y expone el MCP.
8. **Token por vault:** el MCP exige un token por vault, guardado dentro de ese vault en un archivo ignorado por
   git (p. ej. `_brain/state/mcp.token`; `state/` ya está en `.gitignore`). El agente lo lleva en la cabecera
   `Authorization` de su config MCP. Los vaults hermanos (`vault=<sibling>`) siguen leyéndose si el config del
   vault lo permite.
9. **Motor desactualizado:** el contenedor lo actualiza solo al arrancar (`upgrade_engine.py`) y deja un commit con
   la identidad del hub.
10. **Rama `docker-only`:** convive con el modo actual (Python en el host) y cubre Windows, Linux y macOS.
11. **Cualquier agente o harness:** nada específico de Claude Code. Base común: MCP streamable HTTP + instrucciones
    en la descripción de herramientas + extensión Skills. Para clientes que solo hablan stdio, un puente dentro de
    la imagen: `docker exec -i cortex cortex-mcp-stdio --vault <x>`, sin Python en el host. El hub genera el
    snippet de config para cada harness (JSON `mcpServers` genérico + los formatos de Claude Code, Codex, Gemini,
    OpenCode, Hermes).
12. **LAN se queda.** Un certificado sin aviso del navegador exige instalar una CA en cada dispositivo, o tener un
    dominio propio con certificado público. Opciones, de menos a más trabajo:
    - **HTTPS autofirmado (por defecto):** cifrado, nada que instalar; cada dispositivo acepta el aviso una vez.
    - **Dominio propio + Let's Encrypt (DNS-01):** certificado de confianza sin instalar nada en los dispositivos
      ni exponer nada a internet; requiere un dominio y un token de su API de DNS en `.env`.
    - **HTTP plano:** no recomendado, porque la clave de acceso viajaría sin cifrar por la red.

## Más allá de MCP (investigación 2026-11-18)

Pregunta: ¿hay algo más moderno o eficiente que MCP para que los agentes usen Cortex?

**Conclusión: MCP sigue siendo el canal correcto, pero el de Cortex está desactualizado.** Lo que ha cambiado son
las extensiones de MCP, no una alternativa que lo reemplace.

| Opción | Qué es | Encaje con Cortex | Veredicto |
|---|---|---|---|
| **MCP 2026-07-28 (stateless)** | Spec nueva: sin `initialize` ni `Mcp-Session-Id`; versión y capacidades viajan en cada petición; `server/discover`. "Stateless" es del protocolo: la conexión y el proceso siguen vivos | `cortex_mcp.py` habla `2025-06-18` (`PROTOCOL`, línea 47). No guarda estado de sesión MCP, así que migrar es barato; el coste real está en el subproceso por llamada (ver "Motor residente") | **Hacer.** Soportar `2025-11-25` y `2026-07-28`, mantener compat hacia atrás |
| **Skills over MCP** (SEP-2640, final sep. 2026) | El servidor sirve Agent Skills (`skills/list`, `skills/get`, `resources/read`) | Las 5 skills de `skills/` y el bloque de instrucciones salen del contenedor: nada en `~/.claude/skills` ni en `CLAUDE.md` | **Hacer**, con respaldo: el soporte en clientes aún es parcial, y la descripción de la herramienta (`describe()`) ya lleva las instrucciones |
| **MCP Apps** (extensión oficial, ene. 2026) | El servidor manda HTML que el cliente pinta en un iframe aislado | El `Panel.html` (órbita/esfera) dentro del chat, sin abrir el hub | **Después.** Depende de que el cliente lo pinte; Claude Code en terminal no lo haría |
| **Code execution / "code mode"** | El agente escribe código contra las herramientas en vez de llamarlas una a una; Anthropic reporta 150k → 2k tokens en su ejemplo | Cortex ya agrupa con `brief`, `ask`, `finish` y `batch` (hasta 12 llamadas), y su presupuesto de lectura ya acota el contexto | **No.** Exige sandbox del lado del agente y el ahorro aquí sería pequeño |
| **Hooks / plugin de Claude Code** | `SessionStart` inyecta contexto (p. ej. la lista Hot de `MAP.md`) sin llamadas a herramientas | Es lo más eficiente en tokens, pero escribe en la config del agente en el host y ejecuta comandos ahí | **Opcional, opt-in.** Choca con "no tocar el host"; `brief` cubre el inicio de sesión |
| **Mem0, Zep/Graphiti, Letta, Cognee** | Servicios de memoria (vectores, grafos temporales, runtime con estado) | Son otro producto, no otro transporte; casi todos se exponen también por MCP. Cortex se diferencia por Markdown + git con dueño humano | **No migrar.** Como mucho, ideas puntuales (validez temporal tipo Zep) |
| **A2A** | Protocolo agente-a-agente | No es para memoria | **No aplica** |

## Motor residente: sin arrancar nada por petición (prioridad)

Hoy la conexión MCP ya es persistente (servidor HTTP de larga vida, `HTTP/1.1` con keep-alive, `cortex_mcp.py:739`).
Lo que se paga en cada llamada es otra cosa: `run_engine` lanza un **proceso Python nuevo** con `brain.py`
(`cortex_mcp.py:317`), que arranca el intérprete, importa el motor y recarga el vault desde `state/snapshot.pickle`.

Medido en este Windows, vault demo de 16 nodos (2026-11-18, mediana de 3):

| Llamada | Proceso nuevo (hoy) | Motor importado una vez, mismo proceso |
|---|---|---|
| arrancar `python` vacío | ~105 ms | 0 |
| `recall` | ~330 ms | ~80 ms |
| `read` | ~230 ms | ~32 ms |

Es decir, entre 3 y 7 veces más rápido solo por no lanzar procesos, y eso aún recarga el `Vault` en cada `main()`.
En Docker sobre un bind mount de Windows cada lectura de disco es más cara, así que el ahorro crece.

Diseño propuesto para el servicio MCP:

1. **Importar el motor una vez** y llamar a `brain_core.main(argv)` en el proceso (ya acepta `argv`,
   `brain_core.py:3445`), capturando stdout y el código de salida para mantener el contrato 0/1/2.
2. **Vault en memoria por vault**: conservar el objeto `Vault` cargado y revalidarlo barato antes de cada llamada
   (un `scandir` de mtimes/tamaños, lo que el motor ya hace para su snapshot). Sobre bind mounts de Windows no hay
   inotify fiable, así que se sondea, no se escucha.
3. **Un motor por versión**: todos los vaults servidos por el contenedor con la versión de la imagen
   (`upgrade_engine.py` al arrancar). Si un vault trae otra versión, se avisa en vez de cargar dos motores.
4. **Concurrencia igual que hoy**: una cola por vault (el gate actual) y el lock del vault para escrituras; el
   motor tiene estado global (`lru_cache`, módulos), así que una llamada a la vez por vault.
5. **Respaldo**: si una llamada en proceso lanza algo inesperado, se reintenta en subproceso y se registra.
6. **Escrituras de telemetría y sesión** (`state/`) se mantienen síncronas al principio; diferirlas es una
   optimización posterior, medida.

### Sesiones: una por tarea del agente, no una detrás de otra

Hay dos niveles de "sesión":

- **MCP:** una conexión por proceso de agente (cada conversación de Claude Code abre la suya y la reutiliza). Con
  la spec 2026-07-28 ni siquiera hay handshake. Con el motor residente, abrirla no cuesta nada en el servidor.
- **Motor (presupuesto de lectura):** hoy hay **una sesión automática por vault** (`state/sessions/auto.json`,
  `brain_core.py:946`). Se crea otra cuando pasan 10 min sin llamadas (`AUTO_IDLE = 600`), cuando se agotan sus
  consultas o cuando se pide un modo más profundo. Problemas: una pausa larga parte una tarea en dos sesiones; dos
  agentes (o dos conversaciones) sobre el mismo vault comparten la misma sesión; y cada llamada reescribe
  `auto.json` y el `.json` de la sesión en disco.

Propuesta:

1. **Sesión ligada a la tarea, no al reloj:** `brief` (la primera llamada de una tarea) abre la sesión y devuelve
   su id; las llamadas siguientes lo pasan como `session`. Sin id, se mantiene la sesión automática actual como
   respaldo. Así una pausa no corta la tarea y dos agentes no se mezclan.
2. **Sesiones en memoria** en el motor residente: se leen una vez y se escriben al cerrar la tarea (`finish`), al
   expirar o cada N segundos, en vez de en cada llamada.
3. **Idle solo como red de seguridad (híbrido):**
   - **Sesión explícita (con `brief`)**: sin idle. Termina con `finish` o con el siguiente `brief` del mismo agente
     (`by`), que cierra la anterior y le aplica el crédito automático de hoy (`auto_credit`). Es el camino normal.
   - **Sesión de respaldo (llamadas sin `brief`)**: una por agente, no por vault, cerrada tras un idle largo y
     configurable por vault (`_brain/config.json`, p. ej. 30-60 min en vez de los 10 actuales). Existe porque no todos
     los agentes seguirán el protocolo (Codex, Gemini, un `finish` olvidado, un `recall` suelto) y, sin ningún
     cierre, esa sesión acumularía presupuesto durante días hasta bloquear al agente (punto 4) y nunca daría el
     crédito automático, así que los pesos y la lista Hot se congelarían. Con la spec 2026 no hay sesión MCP que
     avise del fin de una conversación, así que el reloj es la única señal en este caso.
   - Una sesión explícita que nadie cierra (el agente se cayó) se queda abierta sin estorbar y la borra la limpieza
     de archivos viejos que ya existe (`Sessions.clean`, 3 días).
4. **Agotar consultas = parar, no renovar en silencio:** devolver el código 2 ("responde con lo que tienes") igual
   que con el presupuesto de cuerpos, para que el agente no encadene sesiones nuevas sin darse cuenta.

Meta: `recall` < 50 ms y `read` < 20 ms dentro del contenedor con el vault de 1.000 nodos del test de rendimiento.

### Resultados de la fase 1 (2026-11-18)

Tests: `docker compose --profile tools run --rm test` → **4 de 4 suites OK** (motor 50, hub 14, MCP 55,
review 4). Los 2 fallos de Windows (`PermissionError` en escrituras concurrentes) no aparecen en Linux.

Benchmark: `scripts/bench_calls.py`, vault de 1.000 nodos, 20 llamadas por caso, mediana:

| Dónde vive el vault | Llamada | Proceso nuevo (hoy) | Motor residente | Residente + escaneo en memoria |
|---|---|---|---|---|
| tmpfs del contenedor | `recall` | 181 ms | 29 ms | (no medido) |
| tmpfs del contenedor | `read` | 154 ms | 19 ms | (no medido) |
| **bind mount de Windows** | `recall` | **1.251 ms** | 900 ms | **121 ms** |
| **bind mount de Windows** | `read` | **1.150 ms** | 858 ms | **86 ms** |

**El cuello de botella en el escenario real es el disco, no el proceso.** En el bind mount, cada llamada
revalida el vault con `Vault._scan` (`brain_core.py:562`), que hace un `stat` por cada `.md`: ~0,55 ms por
archivo a través del puente Windows→Linux, ~0,85 s de 1,09 s en un `recall` perfilado. Cada escritura atómica en
`state/` cuesta ~6,5 ms más.

Cambios al diseño del motor residente, ahora obligatorios:

1. **Escaneo fuera de la petición:** el vault en memoria se revalida en un hilo de fondo cada pocos segundos
   (configurable), no en cada llamada. Las escrituras propias del MCP (`learn`, `finish`) invalidan al momento; una
   edición desde Obsidian se ve con ese retraso como máximo. No se usa inotify porque los cambios hechos desde
   Windows no llegan de forma fiable al contenedor.
2. **Estado en memoria:** snapshot, sesiones y telemetría se mantienen cargados y se vuelcan a `state/` en lote
   (al cerrar la tarea o cada N segundos). Es lo que separa los 121 ms medidos de los ~29 ms del tmpfs.
3. La meta se mide sobre el bind mount, no sobre tmpfs.

Otros hallazgos de la fase 1:

- **Fin de línea:** con `core.autocrlf=true`, los `.sh` salían con CRLF en Windows y se copiaban así a la imagen.
  Arreglado con un `.gitattributes` en la raíz (`*.sh` y los hooks siempre LF).
- **Identidad git en el contenedor:** el hub devuelve 500 al aplicar una revisión si no tiene identidad git
  (`HOME` es un tmpfs vacío). El servicio `test` la fija por variables; el hub real la necesitará por agente
  (decisión 5).
- El README dice 31 tests del motor; hoy son 50.

### Resultados de la fase 2 (2026-11-18)

- **Por defecto, nada en el host:** `docker compose up -d` publica solo `127.0.0.1:8790` en HTTP. El hub vive en
  `http://cortex.localhost:8790` y cada agente en `http://<agente>.cortex.localhost:8790`: los navegadores resuelven
  `*.localhost` a la propia máquina sin DNS y lo tratan como contexto seguro. Sin puertos 80/443/53, sin CA.
- **`--local-http`** (nuevo en `cortex_hub.py`): con `--data`, la escucha local queda en HTTP y solo la LAN usa
  HTTPS. Sin LAN no se genera ninguna CA. Los enlaces a la LAN llevan su propio esquema y puerto
  (`lan_tls`, `--lan-public-port`), y un test nuevo lo cubre.
- **`compose.trusted.yaml`:** el modo anterior (`https://cortex.local`, DNS propio, CA en el host) pasa a ser un
  override opcional. `install.ps1` e `install.sh` lo añaden al `.env` que escriben, así que siguen haciendo lo de
  antes; con un `.env` anterior se paran y piden añadirlo.
- **Verificado en vivo** con los vaults demo: hub y panel por agente cargan en el navegador, solo se publica
  `127.0.0.1:8790`, un `Host` ajeno recibe 421 y no se crea `.cortex-hub/tls`. Tests: 4 de 4 suites (hub 15).
- **Sin verificar en vivo:** el listener LAN con `--local-http` (solo cubierto por el test de enlaces) y los
  instaladores reescritos.
- **Pendiente para la fase 6:** el formulario "Connect an agent" sigue dando el prompt de Python en el host.

**Fase nueva (después de la 3):** "MCP moderno": `2026-07-28` + `server/discover` + extensión Skills, con tests de
compatibilidad para clientes `2025-06-18`. MCP Apps como fase opcional posterior.

Fuentes: [spec 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28),
[blog MCP 2026-07-28](https://blog.modelcontextprotocol.io/posts/2026-07-28/),
[migración stateless (WorkOS)](https://workos.com/blog/mcp-stateless-spec-2026-07-28),
[Skills extension](https://modelcontextprotocol.io/extensions/skills/overview),
[SEP-2640](https://modelcontextprotocol.io/seps/2640-skills-extension),
[Code execution with MCP (Anthropic)](https://www.anthropic.com/engineering/code-execution-with-mcp),
[referencia de plugins de Claude Code](https://code.claude.com/docs/en/plugins-reference.md),
[comparativa de memorias (Graphlit)](https://www.graphlit.com/blog/survey-of-ai-agent-memory-frameworks).
Pendiente de confirmar en el changelog oficial: si `server/discover` es obligatorio y qué clientes ya soportan Skills.

### Resultados de la fase 3, pasos 3a y 3b (2026-11-18)

**3a · MCP dentro del hub, con token por vault.** `cortex_hub.py --mcp` sirve MCP en el mismo puerto que el hub:
`http://cortex.localhost:8790/mcp/<vault>` (un servicio, un contenedor; decisión 7). Ya no hace falta que
`cortex_mcp.py` escuche fuera de loopback: el hub, que ya escuchaba en el contenedor, lo monta (`McpRoutes`).

- Token por vault en `<vault>/_brain/state/mcp.token`, creado al arrancar; la línea se añade al `.gitignore` del vault
  (antes `state/` no estaba ignorado entero). El agente lo manda como `Authorization: Bearer <token>`.
- Rotar el token = escribir otro en el archivo: el nuevo vale al instante y el viejo deja de valer (la caché va por
  mtime y tamaño; un test lo cubre, y encontró que la primera versión dejaba pasar el viejo).
- Sigue el chequeo de Host del hub (421 a un nombre ajeno) y el de Origin del MCP, ahora con `*.localhost`.
- `cortex_mcp.py --http` en el host sigue igual (convive), con `--require-token` y `--resident` opcionales.

**3b · Motor residente.** El motor de la imagen se importa una vez (`Resident`) y cada llamada corre en el proceso;
la lista de archivos de cada vault vive en memoria y un hilo la refresca cada `CORTEX_SCAN_SECONDS` (5 s por defecto)
mientras el vault tuvo uso en los últimos 10 min. Una llamada que escribe recorre el vault y refresca la lista al
momento. Un vault con otra versión del motor, o una llamada que se cae, va por el subproceso de siempre.

| MCP de punta a punta, `recall`, carpeta de Windows | Antes | Ahora |
|---|---|---|
| vault demo, 16 nodos | ~500 ms | (no medido) |
| vault de 1.000 nodos, primera llamada | — | 1.546 ms |
| vault de 1.000 nodos, siguientes (mediana de 11) | ~1.250 ms | **142 ms** |

Lo que queda en esos ~140 ms (perfil de 5 llamadas): ~100 ms de E/S en la carpeta compartida (19 aperturas de
archivo, ~90 `stat`, 4 escrituras atómicas y 6 `mkdir` de `state/` por llamada) y ~50 ms de cálculo del ranking.
Bajar de 50 ms exige el "estado en memoria" (sesiones, telemetría y snapshot sin tocar disco en cada llamada), que
cambia el motor (`brain_core.py`) y por tanto su versión: queda como paso propio después de 3c.

Tests: 4 de 4 suites (motor 50, hub 16, MCP 71, review 4).
