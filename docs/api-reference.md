# Anthropic Bridge — API & parameter reference

Branch `dev` · Sep 2026. Interactive docs: `GET /docs` · spec: `GET /api/spec.yml` (`openapi.yaml`).

## 1. Authentication

| Header | Use |
|---|---|
| `Authorization: Bearer <api_key>` (or `x-api-key`) | Selects the account in `accounts.json` (multi-account) or must equal `BRIDGE_API_KEY` (single-account). Optional only when single-account **and** `BRIDGE_API_KEY` is empty. |
| `Authorization: Bearer <admin_key>` | `/admin/*` when `BRIDGE_ADMIN_KEY` is set (constant-time compare). |
| `X-Session-Id` | Same as body `user`. |

Upstream credentials per account: `backend: anthropic` (Console API key, `x-api-key`) or `backend: bedrock` (AWS credentials via boto3, SigV4).
**Not supported**: Claude Pro/Max subscription OAuth — Anthropic's terms (Feb 19 2026) prohibit it in third-party tools and it is enforced server-side (Apr 4 2026).

## 2. `POST /v1/chat/completions`

### 2.1 messages → Anthropic

| OpenAI | Anthropic |
|---|---|
| `system` / `developer` (all concatenated) | `system: [{text, cache_control: ephemeral}]` (prompt caching on by default; `prompt_caching: false` to disable) |
| `user` string / parts | `user` message; parts → `text`, `image` (base64, URL downloaded with SSRF guard), `document` (PDF base64 or text) |
| `assistant` with `tool_calls` | `assistant` with `tool_use` blocks; **thinking block replayed** (with its signature) when the bridge saw it — required by Anthropic for tool use with thinking |
| `tool` results | `user` with `tool_result` blocks; consecutive same-role messages are merged (strict alternation) |
| `input_audio` | not supported by the Messages API → placeholder text |

Orphan `tool_use`/`tool_result` pairs are repaired; JSON schemas are sanitized (`$ref` resolution, `const`→`enum`, `anyOf` flattening, unsupported keywords stripped).

### 2.2 Generation parameters

| OpenAI | Anthropic | Notes |
|---|---|---|
| `max_tokens` / `max_completion_tokens` | `max_tokens` | default 8192, capped to the model's `max_output_tokens`; raised when a thinking budget requires `max_tokens > budget` |
| `temperature`, `top_p`, `top_k` | same | dropped/forced to 1 when thinking is enabled (API rule) |
| `stop` | `stop_sequences` | |
| `reasoning_effort` `low\|medium\|high` | `thinking: {enabled, budget_tokens: 2048/8192/32768}` + `output_config.effort` (Opus/Sonnet 4.x) | thinking text returned as `reasoning_content` (message or stream deltas) |
| `thinking_budget` | exact `budget_tokens` (≥1024) | |
| `thinking` (object) | passed through | |
| `response_format` json_object/json_schema | forced tool `json_response` | JSON returned as `content` |
| `tools` / `tool_choice` | `tools` / `tool_choice` (`auto`/`any`/`tool`) | |
| `user` | `metadata.user_id` (sha256 of `api_key:user`) | Anthropic's official per-end-user field (abuse detection); no memory |
| `stream`, `stream_options.include_usage` | SSE | final chunk carries `usage` |
| `logprobs`, `frequency_penalty`, `presence_penalty`, `logit_bias`, `n` | — | ignored with a warning |

### 2.3 Native server tools

| Enable with | Anthropic tool | Response |
|---|---|---|
| `tools:[{"type":"web_search"}]` · `"web_search": true` · `web_search_options: {max_uses, user_location}` | `web_search_20250305` | `citations:[{url,title,cited_text}]` |
| `tools:[{"type":"code_execution"}]` · `{"type":"code_interpreter"}` · `"code_execution": true` | `code_execution_20250522` (beta) | code + stdout rendered as markdown in `content`; raw results in `code_execution_results` |

Betas added automatically: `interleaved-thinking-2025-05-14` (thinking), `effort-2025-11-24`, `code-execution-2025-08-25`, `pdfs-2024-09-25` (documents), `context-1m-2025-08-07` (`BRIDGE_CONTEXT_1M=1`).

### 2.4 Response

OpenAI `chat.completion` / `chat.completion.chunk`. Extensions in the message: `reasoning_content`, `citations`, `code_execution_results`. `usage` adds `cache_read_tokens` / `cache_creation_tokens`.

## 3. `POST /v1/messages` — passthrough

Native Anthropic body and response, routed to the account of the bearer key. `anthropic-beta` request header is forwarded. Useful for clients that speak Anthropic format and want the bridge's multi-account/quota layer.

## 4. Quota

`GET /v1/quota` (own account) · `GET /admin/accounts/<key>/quota` · `GET /admin/accounts?quota=1`.
Source: `anthropic-ratelimit-{requests,tokens,input-tokens,output-tokens}-{limit,remaining,reset}` headers captured on every response; `?refresh=1` (or when older than `BRIDGE_QUOTA_TTL`) sends a 1-token probe (`BRIDGE_QUOTA_PROBE_MODEL`). Bedrock accounts have no such headers (see AWS Service Quotas).

## 5. Endpoints

| Method | Path | Auth | What |
|---|---|---|---|
| GET | `/`, `/health`, `/docs`, `/api/spec.yml` | — | info, status (never 500), Swagger, spec |
| GET | `/v1/models`, `/v1/models/<id>` | key | catalog (Anthropic `GET /v1/models` + per-family metadata: context, max output, thinking, pdf, tools) |
| POST | `/v1/chat/completions` | key | OpenAI chat |
| POST | `/v1/messages` | key | Anthropic passthrough |
| GET | `/v1/usage` | — | local counters |
| GET | `/v1/quota` | key | rate-limit quota |
| GET/POST/DELETE | `/admin/accounts[/<key>]` | admin | manage accounts (`{api_key,label,backend,anthropic_key|aws_region,aws_profile}`) |
| GET | `/admin/accounts/<key>/quota` | admin | quota |
| POST | `/admin/accounts/<key>/test` | admin | 1-request smoke test (latency, headers) |

## 6. Environment (`.env`, loaded by server.py)

See `.env.example`: `ANTHROPIC_API_KEY` or `BRIDGE_BACKEND=bedrock`+`AWS_REGION`/`AWS_PROFILE`, `BRIDGE_API_KEY`, `BRIDGE_ADMIN_KEY`, `BRIDGE_ACCOUNTS_FILE`, `BRIDGE_DEBUG`, `BRIDGE_DEFAULT_MAX_TOKENS`, `BRIDGE_UPSTREAM_TIMEOUT`, `BRIDGE_CONTEXT_1M`, `BRIDGE_QUOTA_TTL`, `BRIDGE_QUOTA_PROBE_MODEL`, `BRIDGE_ALLOW_PRIVATE_URLS`, `BRIDGE_MAX_IMAGE_BYTES`, `BRIDGE_MAX_FILE_BYTES`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_VERSION`.

## 7. Bedrock notes

`BEDROCK_MODEL_MAP` in server.py maps Anthropic ids to Bedrock ids; override per model with `BEDROCK_MODEL_CLAUDE_SONNET_4_6=<bedrock id or inference profile ARN>`. Streaming uses `invoke_model_with_response_stream` and is adapted to the same SSE parser. Betas go in the body as `anthropic_beta`.
