# Cómo destripamos agy — el plan, paso a paso, con el porqué de cada cosa

Este es el método que usamos para pasar de "el bridge da 401 y no sé por qué" a "sé exactamente qué manda el
cliente oficial, campo por campo, y mi bridge lo replica". Sirve para agy, pero el método es el mismo para
cualquier CLI (Kimi, Codex, Cursor…). Lo escribo como un plan que puedas repetir solo.

La idea de fondo: **un CLI es una caja negra que habla HTTP con un backend.** Si controlas la máquina donde
corre, puedes ver (1) con quién habla, (2) qué le dice, (3) qué le contesta. Con eso, cualquier bridge se
reconstruye. Nunca se adivina: se captura.

---

## Fase 0 — Preparar el terreno (antes de tocar nada)

### 0.1 Aislar al cliente

Antes de capturar, el CLI tiene que estar "limpio": sin tus MCPs, sin skills, sin hooks, sin historial de
conversaciones. Si no, la captura se llena de ruido (tu MCP de postgres, tus skills, tus sesiones viejas) y no
sabes qué es del protocolo y qué es tuyo.

Para eso descubrimos **dónde guarda su estado** el CLI y creamos un home aislado:

```bash
# ¿dónde vive la config? — busca por nombre en tu home
find ~ -maxdepth 3 \( -iname "*antigravity*" -o -iname "*agy*" \) -not -path "*/node_modules/*"
find / -name "mcp_config.json" -o -name "mcp.json" 2>/dev/null | grep -v node_modules
```

Resultado en agy: todo cuelga de `~/.gemini/` (`antigravity-cli/` para runtime y token OAuth, `config/` para
MCPs y hooks). Como agy es un binario Go, resuelve el home con `$HOME` → un home falso funciona:

```bash
mkdir -p ~/.agy-test/.gemini/antigravity-cli
cp ~/.gemini/antigravity-cli/{antigravity-oauth-token,settings.json,installation_id} ~/.agy-test/.gemini/antigravity-cli/
ln -s ~/.gemini/antigravity-cli/bin     ~/.agy-test/.gemini/antigravity-cli/bin
ln -s ~/.gemini/antigravity-cli/builtin ~/.agy-test/.gemini/antigravity-cli/builtin
HOME=~/.agy-test agy models      # ¿lista modelos sin pedir login? → el home falso funciona
HOME=~/.agy-test agy mcp list    # → "No MCP servers configured" = aislamiento logrado
```

**Por qué importa:** copiamos SOLO el token y la config; no copiamos `config/` (MCPs, hooks) ni
`conversations/`. Así cada captura es del protocolo puro y no ensucia tu agy real. Kimi tiene el mismo concepto
con `KIMI_CODE_HOME`.

### 0.2 Encontrar la forma no-interactiva de invocar al CLI

Para capturar necesitas disparar requests desde un script, no desde un TUI. Aquí agy nos hizo sudar:

- `agy -p "prompt"` → colgaba (timeout) siempre. Lo confirmamos después: su print mode escribe a la terminal
  controladora, no a stdout → sin TTY se queda esperando. Es un bug conocido.
- `agy -i "prompt"` → respondía, pero se quedaba en el TUI.
- **`--input-format stream-json --output-format stream-json`** → NDJSON por stdin/stdout. Ese sí.

Y el shape del mensaje de entrada lo sacamos **a base de errores**, que es una técnica en sí misma:

```
{"role":"user","content":"..."}                       → "missing the event field"
{"event":"user","content":"..."}                      → "user message is missing the message field"
{"event":"user","message":"..."}                      → "cannot unmarshal string into streamInputUserMessage"  (o sea: es objeto)
{"event":"user","message":{"content":"..."}}          → ✅ SUCCESS
```

Cada error te dice el siguiente campo. Cuatro intentos, un minuto. **Lección:** los mensajes de error de
validación son documentación gratis; léelos literalmente.

### 0.3 Herramientas

```bash
pip install mitmproxy --break-system-packages   # el proxy que descifra HTTPS
apt install jq                                  # JSON en la shell
# ss, curl, dig, tcpdump ya vienen con el sistema
```

---

## Fase 1 — Reconocimiento sin descifrar nada (10 minutos)

Antes de meter un proxy, mira qué puedes ver "gratis" desde el sistema operativo. No necesitas romper TLS
para saber con quién habla un proceso.

### 1.1 Levantar el CLI "vivo pero quieto"

Truco del fifo: le abres stdin con una tubería que nunca cierras. El CLI arranca todo (servidores internos,
auth, feature flags) y se queda esperando el primer mensaje. Cero tokens gastados.

```bash
mkfifo /tmp/agy-fifo
HOME=~/.agy-test agy --input-format stream-json --output-format stream-json < /tmp/agy-fifo > /tmp/agy-out.txt 2>&1 &
AGYPID=$!
exec 9>/tmp/agy-fifo     # mantener el fifo abierto (writer)
sleep 3
```

### 1.2 ¿Qué puertos abre localmente?

```bash
ss -tlnp | grep "pid=$AGYPID,"
```

En agy salieron **dos puertos loopback** por proceso. Eso es un hallazgo: el CLI trae un servidor embebido.
Le pegamos con curl a los dos:

```bash
curl -s -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:<puerto>/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary
```

Uno contestó "Client sent an HTTP request to an HTTPS server" (listener TLS) y el otro **el JSON de quota
completo, sin auth**. De ahí salió el `agy-quota` del kit: no hacía falta descifrar nada, el propio agy te lo da
por RPC local. (El nombre del método `RetrieveUserQuotaSummary` lo sacamos de un proyecto open source que ya
lo había encontrado — ai-usagebar — buscar en GitHub "antigravity quota" antes de reinventar ahorra horas.)

### 1.3 ¿A dónde sale por la red?

Mandas un mensaje por el fifo y observas las conexiones salientes mientras responde:

```bash
echo '{"event":"user","message":{"content":"responde unicamente con la palabra: OK"}}' >&9
for i in 1 2 3 4 5 6; do ss -tnp | grep "pid=$AGYPID," | grep -v 127.0.0.1; sleep 0.5; done | sort -u
dig -x <ip> +short     # resolver cada IP a hostname
exec 9>&-; rm -f /tmp/agy-fifo; kill $AGYPID
```

Con esto ya sabes los **hosts** (Google APIs, CDNs, telemetría) sin haber descifrado un byte. Si el bridge
apunta a otro host que el CLI, ya encontraste el primer bug (en nuestro caso: `cloudcode-pa` vs
`daily-cloudcode-pa`).

### 1.4 Variables de entorno y strings del binario

```bash
strings $(which agy) | grep -iE '^(AGY|ANTIGRAVITY)_[A-Z_]+$' | sort -u
agy --help ; agy help ; agy <subcomando> --help
```

Buscas flags de debug, rutas de log, variables tipo `*_HOME`, `*_PROXY`. En agy no había `AGY_HOME`, por eso
fuimos por el `HOME` falso. En Kimi sí había `KIMI_CODE_HOME` y un `--log-level`.

---

## Fase 2 — Descifrar el HTTPS con mitmproxy (el corazón del método)

### 2.1 Por qué funciona

Un proxy MITM se pone en medio: el cliente cree que habla con Google, pero habla con mitmproxy, que a su vez
habla con Google. Para que el cliente no rechace el certificado falso, le decimos que confíe en la CA de
mitmproxy. Los binarios en **Go** (agy) respetan dos variables estándar:

- `HTTPS_PROXY=http://127.0.0.1:8080` → manda el tráfico por el proxy
- `SSL_CERT_FILE=/ruta/mitmproxy-ca-cert.pem` → confía en esa CA

Si un cliente "pinea" certificados (rechaza cualquier CA que no sea la suya), esto falla con error TLS y hay
que ir por caminos más duros (frida, parchear el binario). agy **no pinea**. Kimi (Node) tampoco.

### 2.2 Capturar

```bash
# terminal 1 — el proxy, guardando todo a un archivo
mitmdump -p 8080 -w /tmp/agy.flows

# terminal 2 — un run por el proxy, desde el home aislado
echo '{"event":"user","message":{"content":"responde unicamente con la palabra: OK"}}' \
  | HTTPS_PROXY=http://127.0.0.1:8080 HTTP_PROXY=http://127.0.0.1:8080 \
    SSL_CERT_FILE=/root/.mitmproxy/mitmproxy-ca-cert.pem \
    HOME=~/.agy-test agy --model gemini-3.8-flash-low --input-format stream-json --output-format stream-json --disable-slash-commands
```

Detalles que muerden:
- `SSL_CERT_FILE` con **ruta absoluta**: con `HOME=~/.agy-test`, el `~` ya no es tu home.
- La CA se genera la primera vez que corres `mitmdump`; si no existe, `timeout 3 mitmdump` y listo.
- Un prompt trivial ("responde OK") basta: quieres el **shape** del request, no una respuesta larga.

### 2.3 Leer la captura por capas

Primero la vista de pájaro — qué requests hubo y en qué orden:

```bash
mitmdump -nr /tmp/agy.flows --flow-detail 1
```

En agy eso reveló la **secuencia de arranque completa**: feature flags (Unleash), 3 intentos fallidos de bajar
Playwright, userinfo, foto de perfil, `loadCodeAssist` ×3, `fetchUserInfo`, `retrieveUserQuotaSummary` ×3,
`fetchAvailableModels`, `listExperiments`, `writeTrajectoryAcls`, **`streamGenerateContent` ×2**,
`recordTrajectoryAnalytics`, telemetría. De un vistazo sabes qué es ruido y cuál es la llamada que importa.

Luego, zoom a un endpoint con headers y body completos:

```bash
mitmdump -nr /tmp/agy.flows --flow-detail 4 '~u retrieveUserQuotaSummary' | head -80
mitmdump -nr /tmp/agy.flows --flow-detail 4 '~u streamGenerateContent' > /tmp/model-call.txt
```

`~u` filtra por URL. El body del modelo es enorme (system prompt de 13k tokens + 50 tools) — **no lo leas
entero, grepéalo**:

```bash
grep -nE '"(model|requestType|requestId|userAgent|sessionId)"|thinkingBudget|model_enum|"maxOutputTokens"' /tmp/model-call.txt
sed -n '900,940p' /tmp/model-call.txt     # el final del body, donde están los campos top-level
```

### 2.4 Capturar variaciones

Un run te da el shape base. Para ver cómo cambia por modelo/effort/tools, un loop:

```bash
for m in $(HOME=~/.agy-test agy models 2>/dev/null | grep -vi fetching | awk '{print $1}'); do
  echo '{"event":"user","message":{"content":"responde unicamente con la palabra: OK"}}' \
   | HTTPS_PROXY=http://127.0.0.1:8080 SSL_CERT_FILE=/root/.mitmproxy/mitmproxy-ca-cert.pem HOME=~/.agy-test \
     agy --model $m --input-format stream-json --output-format stream-json --disable-slash-commands >/dev/null 2>&1
done
```

Y uno que fuerce una tool (`--dangerously-skip-permissions` + "lista los archivos") para ver `toolConfig` y
`functionResponse`. Al final aprendimos que **el catálogo (`fetchAvailableModels`) ya traía el
`thinkingBudget` y el enum por modelo**, así que ni hizo falta el loop — pero eso solo lo sabes después de
leer el catálogo. Regla: antes de capturar 14 runs, mira si el backend ya te lo da en un endpoint de metadata.

---

## Fase 3 — Reproducir a mano con curl (la prueba de fuego)

Hasta aquí sabes qué manda el CLI. Ahora verificas que **tú** puedes mandar lo mismo y Google acepta. Esto
separa "capturé bien" de "entendí bien".

### 3.1 Un token válido

```bash
TOKEN=$(jq -r '.token.access_token' ~/.agy-test/.gemini/antigravity-cli/antigravity-oauth-token)
curl -s "https://oauth2.googleapis.com/tokeninfo?access_token=$TOKEN" | jq '{email, expires_in, scope}'
```

`tokeninfo` es el árbitro: si dice `expires_in` positivo y tu email, el token sirve. Nos pasó que el token del
home real estaba viejo (agy refresca en memoria y no siempre reescribe el archivo) — sin `tokeninfo` habríamos
culpado al protocolo. **Además** `tokeninfo` te enseña los **scopes reales** — así descubrimos que faltaba
`https://www.googleapis.com/auth/aicode`, que era la causa raíz del 401 del bridge.

### 3.2 Replicar el request mínimo

Tomas el body capturado y le quitas todo lo que sospechas opcional (tools, labels raros, system prompt):

```bash
UA='antigravity/cli/1.1.28 (aidev_client; os_type=linux; arch=amd64; cl=978129418; auth_method=consumer)'
H=https://daily-cloudcode-pa.googleapis.com/v1internal
cat > /tmp/req.json << EOF
{"project":"aicode-consumers","requestId":"agent/<uuid>/$(date +%s%3N)/<uuid>/1","model":"gemini-3.8-flash-low",
 "userAgent":"antigravity","requestType":"agent",
 "request":{"contents":[{"role":"user","parts":[{"text":"responde unicamente con la palabra: OK"}]}],
            "generationConfig":{"maxOutputTokens":256,"thinkingConfig":{"includeThoughts":false,"thinkingBudget":1000}},
            "sessionId":"-1234567890123456789",
            "labels":{"last_step_index":"0","request_id":"x-0","trajectory_id":"x","used_claude":"false","used_claude_conservative":"false","used_non_gemini_model":"false"}}}
EOF
curl -s -N -X POST "${H}:streamGenerateContent?alt=sse" -H "Authorization: Bearer $TOKEN" -H "User-Agent: $UA" \
  -H 'Content-Type: application/json' -d @/tmp/req.json
```

Si responde `"text": "OK"` → el body mínimo es válido → **eso es lo que el bridge debe generar**, ni más ni
menos. Si da 400, el mensaje te dice el campo. Aquí confirmamos que `model_enum` y los params
`toolAction/toolSummary` de las tools son opcionales, y que el prompt de agy cuesta 13k tokens vs 10 del
mínimo.

(Gotcha de zsh: `$H:stream…` se interpreta como modificador de variable y te come el path → siempre `${H}:…`.)

### 3.3 Sondear features que el CLI no usa

El backend puede aceptar más de lo que el CLI expone. Sondeas con la misma plantilla cambiando `request`:

```bash
probe() { ...arma el body con $2 como "request"...; curl ... "${H}:generateContent" -d @/tmp/probe.json | head -c 900; }
probe googleSearch  '{"contents":[...],"tools":[{"googleSearch":{}}]}'
probe urlContext    '{"contents":[...],"tools":[{"urlContext":{}}]}'
probe codeExecution '{"contents":[...],"tools":[{"codeExecution":{}}]}'
curl ... "${H}:countTokens" ...
curl ... "${H}:embedContent" ...
```

Lectura de resultados: `candidates` con contenido = existe (grounding, urlContext, codeExecution → los tres
entraron al bridge); `400` con nombre de campo = existe pero con otro shape (`countTokens`); `404` HTML = no
existe (`embedContent`). Así se descubre qué "regalar" en el bridge que el CLI oficial ni ofrece.

---

## Fase 4 — Diff contra el bridge y arreglar

Ya con la verdad capturada, comparas contra lo que tu bridge manda. Para ver lo tuyo: `BRIDGE_DEBUG=1` (loguea
el body exacto). Luego recorres el checklist fila por fila:

| Qué comparar | Dónde lo ves (captura) | Dónde vive en el bridge |
|---|---|---|
| Host | línea del request | `ASSIST_URL` |
| `User-Agent` y demás headers | headers del request | función `headers()` |
| Scopes OAuth | `tokeninfo` | `SCOPES` + login |
| client_id / redirect | URL de `accounts.google.com/o/oauth2/v2/auth?…` que abre el CLI | `.env` |
| Body top-level (`project`, `requestId`, `userAgent`, `requestType`) | body del modelo | `build_request()` |
| `request.sessionId`, `labels` | idem | idem |
| `generationConfig` (thinking, maxOutput, temperature…) | por modelo | catálogo + builder |
| Shape de tools | run con tool | conversor de tools |
| Shape de respuesta (`response.candidates`, `usageMetadata`, `thoughtSignature`) | respuesta SSE | parser |

Cada diferencia es un fix concreto. En sep 2026 fueron 8 (host, UA, header de más, project, 3 campos top-level,
sessionId/labels, thinkingConfig, role del system, scope `aicode`). Todo eso son cambios de constantes o de
diccionario — la ingeniería difícil fue **saber qué cambiar**, no cambiarlo.

---

## Fase 5 — Dejarlo reproducible

Lo último y lo que más se agradece en seis meses:

1. **Documentar el protocolo capturado** tal cual (`docs/agy-cli-protocol.md`): hosts, secuencia de boot,
   headers, OAuth, body de request y respuesta, con fecha y versión del CLI.
2. **Documentar el método** (`docs/reverse-engineering.md`): lo que estás leyendo, en versión corta.
3. **Parametrizar el fingerprint** en `.env` (`AGY_CLI_VERSION`, `AGY_CLI_CL`, `ANTIGRAVITY_ASSIST_URL`…) para
   que un cambio de Google sea editar variables, no código.
4. **Un smoke test** (`install.sh`) que te diga en 30 segundos si el protocolo sigue vivo.

---

## Resumen del plan en una hoja

```
0. Aislar:      home falso con solo token+config  →  cero MCPs/skills/hooks en la captura
   Invocar:     encontrar el modo no-interactivo (stream-json) usando los errores como doc
1. Reconocer:   ss -tlnp (puertos locales → RPC gratis) · ss -tnp + dig (hosts) · strings (env vars)
2. Descifrar:   mitmdump -w  +  HTTPS_PROXY/SSL_CERT_FILE  →  --flow-detail 1 (lista) / 4 + ~u (detalle) / grep
3. Reproducir:  tokeninfo (token y SCOPES) → curl con el body mínimo → 200 = entendido
   Sondear:     tools/endpoints que el CLI no usa → features extra para el bridge
4. Diffear:     BRIDGE_DEBUG=1 vs captura, fila por fila del checklist → fixes de constantes
5. Documentar:  protocolo + método + fingerprint en .env + smoke test
```

Tiempo real que nos tomó la primera vez, con todos los tropiezos: ~4 horas. Con este plan y sin tropiezos: ~1
hora. Y el 80% del tiempo perdido fue por cosas que no eran el protocolo (token stale, zsh, `-p` roto) — por eso
la lista de gotchas vale tanto como el método.
