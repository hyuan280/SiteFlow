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
`challenge.image_captcha`, `sign.iframe.image_captcha`, and `sign.image_quiz`.
`flow.result` uses the result-judgement contract below; other features return null.
`config_schema` reflects effective defaults and
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
Captcha and image-quiz prompts belong only in `execution_settings.llm_prompt`, not
in step config or policy defaults. Output-result steps additionally support the
optional per-step prompt described below. Unknown fields are rejected during validation.
DELETE resets both config customization and execution behavior to code defaults.

## Result Judgement

The `ResultExecutionSettings` contract controls model judgement for `flow.result`.
These controls live in Features management and provide the global gate, input mode,
scope, provider/model selection and common prompt. Each output step must also opt in.

| Field | Default | Meaning |
| --- | --- | --- |
| `llm_enabled` | `false` | Master switch. When off, all step-level model settings are ineffective. When on, only explicitly opted-in output steps can invoke the model. |
| `use_vision` | `true` | Send only the current page screenshot, not page text. Requires system `supports_vision`. When false, send page text without a screenshot. |
| `judgment_scope` | `"success_only"` | Revalidate only output steps whose effective `output_value` is `true`. `"all"` judges both True and False output steps. |
| `model` | `""` | Optional model ID, at most 256 characters. Empty uses the system default model. |
| `llm_prompt` | Chinese instruction to check login and signed-in status and output only True/False | Custom instruction, 1-4000 characters. It must explicitly request a True/False conclusion; the system enforces the final True/False result. |

The model override is a free-text ID, not a model registry entry or a provider
selector. It uses the existing system provider endpoint and API key. All dependent
controls are disabled while the master switch is off; their values are retained.
Prompts and model IDs remain subject to validation even while disabled so the saved
settings satisfy the backend contract.

Example settings payload (omit `model` or use `""` for the system default):

```json
{
  "defaults": {"output_value": true, "message": ""},
  "required_fields": [],
  "execution_settings": {
    "llm_enabled": false,
    "use_vision": true,
    "judgment_scope": "success_only",
    "model": "",
    "llm_prompt": "请判断当前页面是否已登录且已签到，仅输出 True 或 False。"
  }
}
```

The example prompt illustrates the default intent; the editor displays the default
prompt returned by the backend rather than maintaining a separate frontend copy.
The feature card always advertises LLM support for `flow.result`. A step's LLM badge
appears only when both global and step-level `llm_enabled` are on and its effective output matches the scope.
Effective output follows local configuration, inherited configuration, then feature
defaults (including global policy defaults); explicit False must not be treated as
missing. Text-only judgement still uses an LLM and receives the same badge.

Enabling judgement sends the screenshot or page text to the external provider;
this may include account details or other sensitive page content. Missing provider
configuration, unsupported vision, request errors, and invalid answers fail the
step. They must not silently fall back to the configured output value, a different
model, or the other input mode.

A valid False conclusion is a successfully evaluated step that outputs false and
fails the site task; a True conclusion outputs true. Model judgement replaces the
configured result within the selected scope. Failed model calls produce no Boolean
output and follow the step's error policy; choosing `continue` can therefore allow
later steps to run. Final conclusions accept only True/False (case-insensitive),
optionally after balanced leading reasoning blocks; prose and JSON are rejected.
The default step deadline allows the provider timeout plus screenshot overhead;
an explicit step timeout still takes precedence. Requests are cancellable and are
never automatically retried. Page text is limited to 24,000 visible body characters;
vision uses the active page viewport screenshot, not the desktop or other tabs.

### Per-Step Opt-In

In site/template output-result cards, `config.llm_enabled` defaults to false.
Existing saved steps that lack this field remain off even when the global switch
is enabled; no migration or automatic opt-in occurs. The UI disables the local
controls while the global switch is off, retaining saved choices for later use.

`config.llm_prompt` is optional (at most 4000 characters). A nonblank value overrides
the common prompt; an empty/whitespace-only value uses the global prompt. Users
must explicitly request a True/False conclusion. Both fields follow normal
template inheritance: a site can override inherited true with false, or override
a template prompt with an empty string to use the global prompt. Reset controls
remove the local override and restore inheritance. Input mode, model selection
and judgment scope remain global.
