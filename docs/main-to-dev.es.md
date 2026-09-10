# anthropic-oauth-bridge: de `main` a `dev` — qué cambió y por qué

`main` (jul 2026) → `dev` (sep 2026). 18 archivos, +2266/−2640 líneas. `server.py` pasó de 2361 a 1525 líneas.
Este documento explica **el porqué** de cada decisión, empezando por la grande: quitar OAuth.

---

## 1. Por qué se quitó el OAuth de suscripción (la decisión de fondo)

### 1.1 Qué hacía `main`

`main` autenticaba con el **token OAuth de una suscripción Claude Pro/Max** (el mismo que usa Claude Code) y,
para que Anthropic aceptara las requests, **se hacía pasar por Claude Code**. Eso no era un detalle: era una
capa entera del código —

- `_build_billing_header` + `_compute_cch`: un header de "billing" con un hash calculado con un salt sacado
  del código de Claude Code, para que el backend "validara" que la request venía del CLI oficial.
- `SYSTEM_IDENTITY` = "You are Claude Code, Anthropic's official CLI…" inyectado como primer bloque del
  system prompt, porque el backend lo espera.
- User-Agent `claude-code/2.1.202`, headers `x-stainless-*`, `X-Claude-Code-Session-Id`.
- Betas exclusivas de OAuth (`oauth-2025-04-20`, `claude-code-20250219`) con lógica para excluirlas una por
  una cuando el backend las rechazaba (`_get_next_beta_to_exclude`, `_add_excluded_beta`).
- `_pascal_case_tool_name` / `_unprefix_tool_name`: renombrar tus tools con prefijo `mcp__` porque Claude
  Code las manda así.
- Todo el flujo PKCE (`_generate_pkce`, `/auth/login`, `/auth/exchange`, `/auth/save-tokens`), lectura de
  `~/.claude/.credentials.json`, refresh "optimista" (usar el token hasta el 401), cuenta "activa" en admin.

Es decir: ~40% del código existía para **parecer otra cosa**, no para traducir OpenAI → Anthropic.

### 1.2 Qué cambió afuera

| Fecha | Hecho |
|---|---|
| 9 ene 2026 | Anthropic activa protecciones server-side contra herramientas de terceros usando OAuth |
| 19–20 feb 2026 | Cambian los términos: los tokens OAuth de Free/Pro/Max **no pueden usarse en herramientas de terceros ni con el Agent SDK**. OpenCode borra su código de OAuth el mismo día |
| 4 abr 2026 | Enforcement general: las suscripciones dejan de cubrir cualquier harness externo (OpenClaw, OpenCode, NanoClaw…). Email a usuarios, crédito de compensación. Solo Claude Code y claude.ai |

`main` es de **julio**, posterior a todo eso. Funcionaba porque el disfraz evadía la detección — y ese es
exactamente el problema.

### 1.3 Por qué no era sostenible ni aunque funcionara

1. **Es una prohibición explícita, no una zona gris.** A diferencia de Antigravity (Google no lo prohíbe por
   escrito ni tiene enforcement), aquí hay términos claros y bloqueos activos. El riesgo real es la cuenta
   Max, no un 401.
2. **Es una carrera armamentista perdida.** Cada release de Claude Code cambia UA, betas o el mecanismo del
   billing header; el bridge tiene que re-capturar y re-clonar. `main` ya traía código para "ir excluyendo
   betas hasta que pase" — síntoma de que el backend ya lo estaba rechazando a ratos.
3. **Contamina el diseño.** Inyectar "You are Claude Code" en el system prompt de wzapi, renombrar tools con
   `mcp__`, forzar headers falsos: todo eso altera el comportamiento del modelo respecto a lo que tú pediste.
4. **Hay alternativas legítimas que hacen lo mismo o más**: API key (pay-as-you-go, con "extra usage"
   descontado para suscriptores), Bedrock/Vertex (tu AWS), y para Claude "por suscripción" el canal de
   Antigravity (Opus/Sonnet 4.6 vía Google, con tu Ultra).

Conclusión: se quitó el OAuth y con él toda la capa de suplantación. El bridge ahora habla el **Messages API
tal como está documentado**, con API key o Bedrock. Nada que esconder, nada que se rompa cuando Claude Code
cambie de versión.

---

## 2. Lo que se quitó (inventario)

| Quitado | Era para… |
|---|---|
| `class Auth` (OAuth, refresh, PKCE, credentials.json), `_generate_pkce`, `_get_access_token_for_request`, `_load_active_account`/`_save_active_account` | autenticar con la suscripción |
| `auth_login_start`, `auth_login_exchange`, `auth_login_status`, `auth_save_tokens`, `admin_set_active_account`, `admin_get_active_account`, `auth-login.py` | el flujo de login OAuth y la "cuenta activa" |
| `_build_billing_header`, `_compute_cch`, `_compute_version_suffix` | el hash que hacía pasar la request por Claude Code |
| `SYSTEM_IDENTITY` + `_transform_anthropic_request` | inyectar la identidad de Claude Code en el system |
| `_pascal_case_tool_name`, `_unprefix_tool_name`, `_strip_tool_prefix_from_response` | renombrar tools con `mcp__` como Claude Code |
| `_get_model_betas`, `_get_next_beta_to_exclude`, `_add_excluded_beta`, `_supports_fast_mode`, `_supports_effort`, `_forbids_sampling_params` | betas exclusivas de OAuth y su lógica de "prueba y excluye" |
| `_get_model_override`, `_extract_first_user_message_text`, `_debug_log`, `_check_api_key`, `_admin_auth` | reemplazados por versiones más simples |
| `anthropic-oauth-bridge.service`, `accounts.example.json` (formato OAuth) | reemplazados |

---

## 3. Lo que se conservó (lo bueno de `main`)

Estas partes eran traducción OpenAI ↔ Anthropic pura y estaban bien hechas; se copiaron casi tal cual:

- **`_sanitize_schema_for_claude`** y helpers (`_resolve_ref`, `_coerce_type`, `_flatten_anyof`,
  `_strip_unsupported`): limpieza de JSON Schema para que las tools de cualquier cliente pasen la validación
  de Anthropic. Mejor que la del bridge de Gemini.
- **`_repair_orphan_tool_pairs`**: repara historiales con `tool_use` sin `tool_result` o viceversa.
- **`_oai_content_to_anthropic`** (texto/imagen), **`_oai_tool_choice_to_anthropic`**, **`_oai_tools_to_anthropic`**,
  **`_tool_call_id_to_name`**, **`_anthropic_content_to_openai_message`**, **`_anthropic_usage_to_openai`**.
- La idea de multi-cuenta con `accounts.json` y `/admin/accounts`.

---

## 4. Un bug de `main` que salió a la luz

`_oai_messages_to_anthropic` **ignoraba los `tool_calls` estándar de OpenAI** (`msg["tool_calls"]`) y tiraba
los mensajes de assistant con `content: null`. Solo entendía un formato inline no estándar
(`content: [{"type": "tool_calls", ...}]`). Consecuencia: el multi-turno con tools desde cualquier cliente
normal (LiteLLM, Open WebUI, Hermes, el SDK de OpenAI) se rompía en el segundo turno. Se detectó con el test
offline al portar la función; el fix va en la sección siguiente.

---

## 5. Lo que se agregó

### 5.1 Auth nueva
- `class Account` / `AccountManager`: cada key de cliente → `backend: anthropic` (x-api-key) o `backend: bedrock`
  (boto3, región/perfil). Single-account con `ANTHROPIC_API_KEY` en `.env`.
- `_require_account`, `_resolve_account`, `_bearer`, `_check_admin` (compare_digest).
- `upstream_messages`: una sola función para Anthropic o Bedrock (streaming incluido, `_BedrockStream` adapta el
  event stream de AWS al mismo parser SSE).

### 5.2 Conversión de mensajes reescrita
- `tool_calls` estándar, `content: null`, merge de mensajes consecutivos del mismo rol (Anthropic exige alternancia).
- **`_remember_thinking`** + `_THINKING_CACHE`: cuando thinking está activo y el modelo llama una tool,
  Anthropic exige que el siguiente turno replaye el bloque `thinking` con su `signature`. Los clientes OpenAI
  no pueden transportarlo, así que el bridge lo guarda por `tool_use_id` y lo reinyecta (mismo truco que el
  `thoughtSignature` del bridge de Gemini).

### 5.3 Builder (`build_anthropic_request`)
- `reasoning_effort` → `thinking.budget_tokens` (2048/8192/32768) + `output_config.effort`; `max_tokens` se
  sube solo cuando el budget lo exige; temperature/top_p se ajustan a las reglas de thinking.
- Prompt caching: `cache_control` en el último bloque del system (desactivable con `prompt_caching: false`).
- `user` / `X-Session-Id` → `metadata.user_id` (campo oficial de Anthropic; sin memoria server-side).
- `response_format` json_object/json_schema → tool forzada `json_response`.
- Tools nativas: `web_search` → `web_search_20250305` (con `max_uses`, `user_location`), `code_execution` →
  `code_execution_20250522` + beta.
- Content: `file` → bloques `document` (PDF base64 o texto), imágenes con SSRF guard y tope de tamaño.
- Betas solo públicas y por feature: interleaved thinking, effort, code execution, PDFs, context 1M (opt-in).

### 5.4 Respuesta
- `reasoning_content` (thinking) en el message y como deltas en streaming.
- `citations` (web_search), `code_execution_results`, código+salida renderizados en `content`.
- `usage` con `cache_read_tokens` / `cache_creation_tokens`.
- Streaming reescrito: parsea `text_delta`, `thinking_delta`, `signature_delta`, `input_json_delta`, `server_tool_use`.

### 5.5 Endpoints nuevos
- `POST /v1/messages` — passthrough en formato Anthropic (multi-cuenta, `anthropic-beta` reenviado).
- `GET /v1/quota`, `/admin/accounts/<key>/quota`, `/admin/accounts?quota=1` — desde los headers
  `anthropic-ratelimit-*` (requests/tokens restantes y reset).
- `POST /admin/accounts/<key>/test` — smoke test de una cuenta.
- `GET /docs` (Swagger UI) y `/api/spec.yml` (`openapi.yaml`, 14 rutas / 11 schemas).

### 5.6 Operación y seguridad (transplantado del bridge de Antigravity)
- Loader de `.env` propio, `BRIDGE_DEBUG=1` (tracebacks + body upstream), `/health` que nunca da 500.
- SSRF guard en URLs de cliente, topes de tamaño, `accounts.json` en 600, aviso al arrancar si `/admin` está abierto.
- `install.sh` nuevo: elige backend (API key / Bedrock), host de bind, genera client key y admin key, preserva
  `.env`, instala systemd o launchd, smoke test de 5 endpoints.
- Docs en inglés: README, `docs/api-reference.md`, `docs/security.md`; `DISCLAIMER.md` actualizado.

---

## 6. Qué pierde y qué gana el usuario

| | `main` (OAuth) | `dev` (API key / Bedrock) |
|---|---|---|
| Costo | "gratis" contra la suscripción | pay-as-you-go (o Bedrock) |
| Riesgo | bloqueo de cuenta; se rompe con cada release de Claude Code | ninguno; API pública y estable |
| Comportamiento del modelo | alterado (identidad Claude Code, tools renombradas) | exactamente lo que pides |
| Thinking | sin `reasoning_content` | `reasoning_content` + replay automático |
| Tools nativas | no | web_search con citas, code_execution |
| Documentos | solo imágenes | + PDF y texto |
| Caching | implícito | `cache_control` explícito y visible en `usage` |
| Quota | no | headers de rate limit por cuenta |
| Backends | uno | Anthropic + Bedrock |
| Formato nativo | no | `/v1/messages` passthrough |
| Docs | README | Swagger + reference + security |

Para Claude con quota "estilo suscripción", el camino que sí funciona y no viola nada es el bridge de
Antigravity (Claude Opus/Sonnet 4.6 vía Google, con la cuenta Ultra).

---

## 7. Cómo migrar

```bash
git fetch && git checkout dev
./install.sh          # pide ANTHROPIC_API_KEY (o Bedrock), genera keys, instala el daemon
```

Los clientes no cambian nada excepto la key: siguen apuntando a `/v1/chat/completions` con el mismo formato.
Si tenías `accounts.json` del formato viejo (con `refresh_token`), reemplázalo por el nuevo
(`accounts.example.json`: `anthropic_key` o `aws_region`).
