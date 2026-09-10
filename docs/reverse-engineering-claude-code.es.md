# Cómo aplicar el método a Claude Code — plan completo (versión legítima)

Este documento aplica el mismo método que usamos con agy (aislar → reconocer → capturar → reproducir →
diffear → documentar) al **Claude Code CLI**. La meta es **aprender cómo Anthropic construye un agente de
producción** (prompt caching, manejo de contexto, tools, betas) y **depurar tus propios hooks/MCPs**, usando
tu **API key** de Console.

Lo que este documento NO cubre, a propósito: tomar el fingerprint capturado para hacer pasar un token OAuth
de suscripción (Pro/Max) por Claude Code desde otro programa. Desde febrero de 2026 los términos de Anthropic lo
prohíben y desde abril lo bloquean server-side; hacerlo arriesga la cuenta. Con API key no hay nada que
"disfrazar": el Messages API está documentado y es exactamente lo que el bridge de la rama `dev` habla.

---

## Diferencias clave respecto a agy (antes de empezar)

| | agy | Claude Code |
|---|---|---|
| Runtime | binario Go | **Node.js** (instalado con npm o el installer nativo) |
| Cómo confiar en una CA | `SSL_CERT_FILE` | **`NODE_EXTRA_CA_CERTS`** |
| Redirigir el backend | no (host fijo) | **`ANTHROPIC_BASE_URL`** — variable oficial. Es la puerta grande |
| Aislar config | `HOME` falso | **`CLAUDE_CONFIG_DIR`** — variable oficial |
| Modo no-interactivo | stream-json (el `-p` estaba roto) | **`claude -p`** funciona; `--output-format json\|stream-json` |
| Servidor local | sí (RPC de quota) | no (solo la integración con IDEs, opcional) |
| Doc del protocolo | ninguna | **completa**: docs.claude.com (Messages API, betas, caching) |
| Env vars | `strings` del binario | documentadas (`claude --help`, doc de settings) |

La consecuencia práctica: **en Claude Code casi nunca hace falta mitmproxy.** Con `ANTHROPIC_BASE_URL` le dices
que hable con tu propio servidor, y ahí ves todo en texto plano.

---

## Fase 0 — Preparar

### 0.1 Un directorio de config aislado

```bash
mkdir -p ~/.claude-test
export CLAUDE_CONFIG_DIR=~/.claude-test
claude --version
```

Con `CLAUDE_CONFIG_DIR` apuntando a un dir vacío, Claude Code arranca sin tus MCPs (`~/.claude.json`), sin
tus hooks (`settings.json`), sin skills ni memoria (`CLAUDE.md` global), y sin sesiones. Para que use tu API
key sin pasar por login:

```bash
export ANTHROPIC_API_KEY=sk-ant-api03-...      # key de console.anthropic.com
```

Corre desde un directorio vacío (`mkdir /tmp/ct && cd /tmp/ct`) para que tampoco cargue `CLAUDE.md`,
`.claude/` ni `.mcp.json` de un proyecto.

### 0.2 El modo no-interactivo

```bash
claude -p "responde unicamente con la palabra: OK"                       # texto
claude -p "..." --output-format json                                     # resultado + usage + costo
claude -p "..." --output-format stream-json --verbose                    # cada evento (system, assistant, tool, result)
claude -p "..." --model claude-haiku-4-5 --max-turns 1                   # modelo y límite de turnos
claude -p "..." --allowedTools "Read,Grep" / --dangerously-skip-permissions   # control de tools
```

Verifica que responde y anota el `usage` del JSON: ahí ya ves `cache_creation_input_tokens` y
`cache_read_input_tokens` — el primer indicio de cómo usa caching.

### 0.3 Herramientas

```bash
pip install mitmproxy --break-system-packages   # solo si quieres la vía MITM
apt install jq
```

---

## Fase 1 — Reconocer sin descifrar

### 1.1 Hosts

```bash
claude -p "responde unicamente: OK" >/dev/null &
PID=$!; sleep 2
for i in 1 2 3 4 5 6; do ss -tnp | grep "pid=$PID," | grep -v 127.0.0.1; sleep 0.5; done | sort -u
wait $PID
dig -x <ip> +short
```

Esperable: `api.anthropic.com` (el API), `statsig.anthropic.com` (feature flags), Sentry (errores). Con
`DISABLE_TELEMETRY=1` / `DISABLE_ERROR_REPORTING=1` desaparecen los dos últimos — buena práctica para que la
captura sea solo del API.

### 1.2 Env vars y flags

```bash
claude --help
claude config list                      # config efectiva
cat $CLAUDE_CONFIG_DIR/settings.json    # lo que hay en el dir aislado (nada, al inicio)
```

Las variables que importan para este ejercicio: `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`,
`CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` (rutea por AWS/GCP — así funciona "Claude Code con tu
infra"), `MAX_THINKING_TOKENS`, `DISABLE_TELEMETRY`, `HTTPS_PROXY`, `NODE_EXTRA_CA_CERTS`. Todo documentado en
la página de settings de Claude Code.

---

## Fase 2A — Capturar con un logger propio (la vía recomendada)

Como Claude Code acepta `ANTHROPIC_BASE_URL`, no hace falta engañar a nadie: levantas un servidor que **guarda
el request, lo reenvía a Anthropic con tu key, guarda la respuesta y la devuelve**. Texto plano, sin
certificados, sin proxy.

```python
# logger.py — proxy transparente que registra cada request/response a disco
import json, time, sys
from pathlib import Path
import requests
from flask import Flask, Response, request

UPSTREAM = "https://api.anthropic.com"
OUT = Path("captures"); OUT.mkdir(exist_ok=True)
app = Flask(__name__)

@app.route("/<path:path>", methods=["GET", "POST"])
def relay(path):
    t = time.strftime("%H%M%S")
    body = request.get_data()
    hdrs = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    (OUT / f"{t}-{path.replace('/', '_')}-req.json").write_text(json.dumps(
        {"method": request.method, "path": path, "query": request.query_string.decode(),
         "headers": {k: ("***" if k.lower() in ("x-api-key", "authorization") else v) for k, v in hdrs.items()},
         "body": json.loads(body) if body else None}, indent=2))
    up = requests.request(request.method, f"{UPSTREAM}/{path}", params=request.query_string,
                          headers=hdrs, data=body, stream=True, timeout=600)
    chunks = []
    def gen():
        for c in up.iter_content(chunk_size=None):
            chunks.append(c); yield c
        (OUT / f"{t}-{path.replace('/', '_')}-resp.txt").write_bytes(b"".join(chunks))
    return Response(gen(), status=up.status_code,
                    headers={k: v for k, v in up.headers.items() if k.lower() not in ("content-length", "transfer-encoding", "content-encoding")})

app.run(port=8089)
```

```bash
python3 logger.py &
ANTHROPIC_BASE_URL=http://127.0.0.1:8089 CLAUDE_CONFIG_DIR=~/.claude-test DISABLE_TELEMETRY=1 \
  claude -p "lista los archivos de este directorio y di OK" --dangerously-skip-permissions --output-format json
ls captures/
```

Ahora tienes, por cada llamada al modelo, el **request completo** (headers con la key enmascarada, body con
system, tools, messages) y la **respuesta SSE cruda**. Nota que la key la enmascara el logger antes de guardar
— acostúmbrate a eso desde el primer día.

## Fase 2B — Capturar con mitmproxy (si no puedes cambiar la base URL)

Mismo mecanismo que con agy, solo cambia la variable de la CA porque es Node:

```bash
mitmdump -p 8080 -w /tmp/claude.flows
HTTPS_PROXY=http://127.0.0.1:8080 NODE_EXTRA_CA_CERTS=/root/.mitmproxy/mitmproxy-ca-cert.pem \
  CLAUDE_CONFIG_DIR=~/.claude-test claude -p "responde unicamente: OK"
mitmdump -nr /tmp/claude.flows --flow-detail 1
mitmdump -nr /tmp/claude.flows --flow-detail 4 '~u messages' | grep -nE '"model"|anthropic-beta|cache_control|"thinking"|max_tokens'
```

Si Node se queja de TLS, `NODE_TLS_REJECT_UNAUTHORIZED=0` lo desbloquea (solo en tu máquina de pruebas).

---

## Fase 3 — Leer la captura: qué buscar y qué aprender

Abre un `*-req.json` de una llamada de chat y recorre esto en orden:

### 3.1 Headers

Fíjate en `anthropic-version`, `anthropic-beta` (qué betas activa: thinking, effort, manejo de contexto,
archivos) y los headers de cliente (`user-agent`, `x-app`, `x-stainless-*`). Aquí solo **observa** — con API
key, tu bridge manda los suyos propios y eso es correcto.

### 3.2 System prompt y `cache_control` — la lección principal

El `system` de Claude Code es un array de bloques, y verás `"cache_control": {"type": "ephemeral"}` en
puntos concretos. Eso son los **breakpoints de prompt caching**: Anthropic cachea todo lo anterior al
breakpoint y en la siguiente llamada lo lee del cache (10% del precio). Preguntas que responder con la captura:

- ¿Cuántos breakpoints usa y dónde? (típicamente: fin del system estable, fin de las tools, último turno)
- ¿Qué pone ANTES del breakpoint (estable: identidad, reglas, tools) y qué DESPUÉS (cambia: fecha, cwd, git status)?
- En la segunda llamada, `usage.cache_read_input_tokens` ¿cuánto es respecto a `input_tokens`?

Esto es directamente aplicable al bridge de Anthropic (que pone un breakpoint al final del system) y a wzapi:
si tu system prompt de WhatsApp es de 5k tokens y lo mandas 10k veces al día, el orden de los bloques decide
si pagas 5k o 500 por mensaje.

### 3.3 Tools

Verás los `input_schema` de las tools de Claude Code (Bash, Read, Edit, Grep, Glob, Agent, WebFetch…). Aprende
de cómo escriben las `description` (largas, con reglas y ejemplos) — es la parte del prompt que más influye en
que el modelo use bien una tool. Compara con las descripciones de tus tools en LISSA/wzapi.

### 3.4 Thinking y effort

Con `MAX_THINKING_TOKENS=8000` verás `"thinking": {"type": "enabled", "budget_tokens": ...}` y, en la
respuesta, bloques `thinking` con `signature`. En el segundo turno de una tool, fíjate cómo **replaya el
bloque thinking** con su signature en el mensaje del assistant — es la regla que implementamos en el bridge
(`_THINKING_CACHE`). Aquí la ves en su hábitat.

### 3.5 Respuesta SSE

En el `*-resp.txt`: `message_start` (usage inicial), `content_block_start/delta/stop` por bloque (`text`,
`thinking` + `signature_delta`, `tool_use` + `input_json_delta`), `message_delta` (stop_reason, usage final),
`message_stop`. Es exactamente lo que parsea el streaming del bridge; si algún día un evento nuevo aparece
aquí, sabrás que hay que actualizar el parser.

### 3.6 Manejo de contexto largo

Genera una conversación larga (o pega un archivo grande) y mira cómo Claude Code hace **compaction**: en algún
momento verás una llamada cuyo prompt pide resumir la conversación, y la siguiente arranca con ese resumen.
Es el patrón para que un agente de WhatsApp con historiales largos no reviente el contexto.

---

## Fase 4 — Reproducir con curl y aplicar al bridge

Con API key, "reproducir" es trivial y ya está hecho: el bridge de la rama `dev` habla el Messages API
documentado. Lo que sí vale la pena es **copiar decisiones de diseño** vistas en la captura:

1. **Breakpoints de caching**: ajustar dónde pone `cache_control` el bridge (hoy: fin del system). Si ves que
   Claude Code también lo pone al final de `tools`, agrégalo: `tools[-1]["cache_control"] = {"type":"ephemeral"}`.
2. **Estructura de descripciones de tools**: aplicar el estilo a las tools de LISSA.
3. **Compaction**: implementar el resumen automático en el orquestador cuando `input_tokens` supere un umbral.
4. **Betas**: si ves una beta pública que tu bridge no manda y te sirve (p.ej. manejo de contexto), agrégala a
   las constantes `BETA_*`.

Prueba de fuego para cualquiera de esas: un curl directo al Messages API con tu key, mirando `usage` antes y
después del cambio.

---

## Fase 5 — Depurar tus propios hooks y MCPs (uso cotidiano del logger)

El logger de la fase 2A se vuelve una herramienta permanente:

```bash
# ver exactamente qué le llegó al modelo después de que tu MCP respondió
ANTHROPIC_BASE_URL=http://127.0.0.1:8089 claude -p "usa la tool X" --mcp-config mi-mcp.json
jq '.body.messages[-1]' captures/*-req.json | tail -40
```

Sirve para: verificar que un hook `PreToolUse` modificó lo que creías, ver el `tool_result` real (y su tamaño
en tokens) que devuelve tu MCP, medir cuánto contexto consume cada tool, y reproducir bugs "el modelo no vio
mi archivo" con evidencia.

---

## Resumen en una hoja

```
0. Aislar:      CLAUDE_CONFIG_DIR=~/.claude-test + ANTHROPIC_API_KEY + dir vacío
   Invocar:     claude -p "..." --output-format json|stream-json
1. Reconocer:   ss -tnp + dig (api.anthropic.com, statsig, sentry) · DISABLE_TELEMETRY=1 · claude --help
2. Capturar:    A) ANTHROPIC_BASE_URL=http://127.0.0.1:8089 → logger.py (texto plano, key enmascarada)   ← preferido
                B) mitmproxy + HTTPS_PROXY + NODE_EXTRA_CA_CERTS
3. Leer:        headers/betas · system + cache_control (breakpoints) · tools · thinking+signature · SSE · compaction
4. Aplicar:     caching breakpoints, descripciones de tools, compaction, betas → bridge/LISSA/wzapi; medir con usage
5. Reusar:      el logger como herramienta de depuración de hooks y MCPs
```

Y la línea, para que quede escrita: todo lo anterior es con **API key** (o Bedrock/Vertex). Capturar para
replicar el fingerprint de Claude Code y meter un token de suscripción por otro programa es lo que Anthropic
prohíbe y bloquea desde 2026 — no es un problema técnico, es un problema de términos y de cuenta.
