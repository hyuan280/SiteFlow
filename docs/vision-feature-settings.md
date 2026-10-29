# Vision and Feature Settings

All endpoints below use the existing authenticated `/api/v1` API and CSRF protection
for mutations and integration tests.

## Vision Integration

`GET /integrations` includes `vision` with `base_url: ValueView`, `model: ValueView`,
`api_key: TokenView`, `supports_vision: BooleanView`, and `available: boolean`.
Available means configured and locally valid, not a successful provider probe. API
keys are never returned in plaintext.

`PUT /integrations/vision` accepts `{base_url?, model?, api_key?, supports_vision?}`.
Omitted or null fields are unchanged; empty strings are rejected. The base URL is an
HTTP(S) OpenAI-compatible `/v1` root, optionally under a path prefix, without URL
credentials, query, or fragment. Values are encrypted using the existing integration
key storage. `DELETE /integrations/vision` clears the DB configuration.
Environment-controlled fields reject edits, and any environment-controlled field
prevents deletion (409).

Environment overrides DB independently per field:
`SITEFLOW_VISION_BASE_URL`, `SITEFLOW_VISION_API_KEY`, `SITEFLOW_VISION_MODEL`,
`SITEFLOW_VISION_SUPPORTS_VISION`.
`SITEFLOW_VISION_TIMEOUT` controls request timeout in seconds (default 30, maximum 300).

`supports_vision` defaults to true for existing deployments. When false, an image
captcha step configured to use the `vision` method fails with
`reason=vision_not_supported`; steps using `ddddocr` or `nexusphp_gd5` are
unaffected. `sign.image_quiz` always requires vision and returns `waiting_human`.

`POST /integrations/vision/test` uses the saved `supports_vision` flag. When enabled,
it submits an actual tiny PNG to `/chat/completions` and requires a nonempty textual
response. When disabled, it sends a text-only `/chat/completions` request instead of
an image. This checks provider connectivity, not recognition accuracy.
Provider errors and response bodies are not exposed. Provider redirects are not
followed, so API keys cannot be forwarded to a redirect destination; configure the
final API root directly.

The image-captcha features expose `captcha_method` as a normal step config field
whose default is `ddddocr`. It is also a global step default in Features management,
so each step can override the method. The available values come from the built-in
method registry; adding another method there automatically adds it to the schema,
step dropdowns, and Features management without changing the feature card. Current
methods:

- `ddddocr`: universal OCR, simple but less accurate. Only this method uses
  `min_confidence`.
- `vision`: OpenAI-compatible visual model. The step fails clearly when the model is
  not configured or `supports_vision` is disabled. It never silently falls back.
- `nexusphp_gd5`: exact template matching for NexusPHP GD font-5 captchas. It supports
  standard 150×40 images containing six characters, automatically rescales differently sized images, and reports `match_margin`.

The image-captcha features also expose management-only `execution_settings` containing
`llm_prompt` (default asks for captcha characters only, preserving case). The prompt
is used only when the selected method is `vision`. `sign.image_quiz` exposes the same
`llm_prompt` field as a custom quiz instruction with an image-quiz-specific default
and always uses vision. Prompts are not step config fields or defaults. Runtime
integration secrets are sent separately from resolved workflow configs and are not
included in persisted workflow snapshots or recognition logs.

Worker requests include `feature_execution_settings: {"<feature key>":
{"llm_prompt": string}}`, containing only enabled, supported features present in
that execution. Provider credentials remain in the separate `vision` runtime field.
They are sent only when an enabled image-captcha step selects `vision`, or when an
enabled `sign.image_quiz` step needs vision. Neither runtime field is added to
workflow snapshots.

`sign.image_quiz` always requires vision and has no ddddocr fallback. Without a
configured model or with `supports_vision` disabled it returns `waiting_human`. It
sends the cropped quiz image,
question text, and numbered visible options. The model must return a JSON object
with `is_clear`, `selected_index`, `need_refresh`, and optional `reason`; reasoning
content remains ignored. Unclear answers refresh the question up to `max_refresh`
times, then return `waiting_human`. Quiz requests use `max_tokens: 2048` to leave
space for reasoning models to emit the final JSON.

Recognition reads only the response message's `content`, never `reasoning_content`.
String content and arrays of `{type: "text", text: string}` are supported. Balanced
leading `<think>...</think>` blocks are removed; nested, incomplete, misplaced, or
reasoning-only wrappers are rejected. The remaining answer must be a single ASCII
alphanumeric token, then pass the step's charset/length constraints. Prose, Markdown,
and other ambiguous output fail the vision step rather than extracting a guessed
answer or falling back to another method. Responses marked `finish_reason: "length"`
or `"content_filter"` are rejected. The request currently uses `max_tokens: 128`;
some reasoning providers consume that budget without producing final content.
Parsing cannot recover an answer that was never emitted, and provider-specific
reasoning budgets/token parameters are not automatically negotiated.

## Feature Policies

`GET /features` adds `defaults`, `required_fields`, `base_required_fields`,
`customized`, and `execution_settings`. Defaults include code default factories.
For image-captcha features, defaults include `captcha_method` (default `ddddocr`).
`execution_settings` is an object with `llm_prompt` for
`challenge.image_captcha`, `sign.iframe.image_captcha`, and `sign.image_quiz`, and
null for all other features. `config_schema` reflects effective defaults and
required fields; types and constraints remain code-defined.

`PUT /features/{key}/settings` accepts:

```json
{
  "defaults": {"captcha_method": "nexusphp_gd5", "length": 6},
  "required_fields": [],
  "execution_settings": {
    "llm_prompt": "Read the captcha. Return only its characters."
  }
}
```

`defaults` and `required_fields` are required. `execution_settings` is optional;
omitted or null preserves existing behavior. A supplied object replaces behavior
settings, with omitted members taking code defaults. `llm_prompt` is a string of
1-4000 characters. Unknown execution-setting members are rejected. Non-null
execution settings for unsupported features return 422. Behavior fields in
`defaults` or `required_fields` return 422.

The endpoint returns the updated FeatureView. This replaces the stored customization;
unspecified defaults still inherit code defaults. Unknown fields and invalid defaults
are rejected (422). A code-required field can be relaxed only if the supplied policy
provides a valid default. Complete default sets also run model-level validation.
`DELETE /features/{key}/settings` resets to code defaults and returns FeatureView.
Unknown feature keys return 404. Policies use AppSetting JSON at `feature.<key>`;
`execution_settings` is stored separately from `defaults` and `required_fields`.

Workflow resolution merges policy defaults before template/site configuration and
runs the original full model validation. Existing explicit step values take priority;
policy edits do not rewrite saved steps or historical snapshots.

`captcha_method` selects the recognition method, defaulting to `ddddocr`.
The prompt belongs only in `execution_settings.llm_prompt`, not in step config
or policy defaults. Unknown step config fields are rejected during validation.
DELETE resets both config customization and execution behavior to code defaults.
