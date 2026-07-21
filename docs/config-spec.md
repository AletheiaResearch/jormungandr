# Configuration specification

**Status:** proposal. Nothing here is implemented — this is the design to argue
with before writing code.

This covers **jormungandr's own config**: the file you write to say which
harness to run, where the prompts are, which providers to use, and the
per-harness specifics. It is not about hand-editing a harness's own config
files — those are an output of this, not an input.

## What the config compiles to

One file in, four existing objects out. The config is a thin envelope over
models that already exist, not a parallel universe:

| Config section | Compiles to | Already exists |
|---|---|---|
| `image` + `harness` | `ImageSpec` (base/runtime tiers, modules) | yes |
| `providers` + `harness.<name>` | the harness's own config file, baked into the image | no — this is the new work |
| `run` | `ContainerSpec` (limits, network, mounts, env) | yes |
| `prompts` + `run` | `PromptRunner.run(...)` calls | yes |

## The load-bearing idea: providers are declared once

A provider is a base URL, a credential, and some models. Every harness needs
that same information and every harness wants it in a different shape. Teich
declares exactly one provider (`api:`) and cannot express a second. That is the
first thing to fix.

Declare providers once as a named map; reference them as `<provider>/<model>`.
jormungandr translates each declaration into whatever the chosen harness
actually reads.

**The translation is not cosmetic.** Both of these were verified end to end
against a real container, with a local stub endpoint and no vendor account:

*One logical provider →*

```jsonc
// droid  ->  ~/.factory/settings.json
{
  "customModels": [{
    "model": "deepseek/deepseek-v4-flash",
    "displayName": "openrouter-deepseek",
    "baseUrl": "https://openrouter.ai/api/v1",
    "apiKey": "${OPENROUTER_API_KEY}",
    "provider": "generic-chat-completion-api",
    "maxOutputTokens": 16384
  }],
  "sessionDefaultSettings": { "model": "custom:openrouter-deepseek-0" }
}
```

```jsonc
// opencode  ->  ~/.config/opencode/opencode.json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "openrouter": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "OpenRouter",
      "options": { "baseURL": "https://openrouter.ai/api/v1",
                   "apiKey": "{env:OPENROUTER_API_KEY}" },
      "models": {
        "deepseek/deepseek-v4-flash": {
          "name": "DeepSeek v4 Flash",
          "limit": { "context": 128000, "output": 16384 }
        }
      }
    }
  },
  "model": "openrouter/deepseek/deepseek-v4-flash"
}
```

Different file, different path, different field names, different nesting — and
four substantive differences a naive mapping would get wrong:

- **Model naming.** OpenCode uses `<providerId>/<modelId>`, splitting on the
  *first* `/` so the remainder can itself contain slashes, and accepts it on
  `--model`. Droid derives `custom:<displayName>-<index>` and *rejects* it on
  `--model` ([Factory-AI/factory#787](https://github.com/Factory-AI/factory/issues/787)),
  so it must be set through `sessionDefaultSettings.model`.
- **Secret interpolation syntax differs.** Droid expands `${VAR}`; OpenCode
  expands `{env:VAR}` (and `{file:path}`). `${VAR}` does *not* work for
  OpenCode's `apiKey`.
- **OpenCode model keys must match the upstream `GET /v1/models` id**, or be
  aliased via `models.<key>.id`.
- **OpenCode needs `limit.context` and `limit.output`** for custom models —
  without them it cannot compute remaining context, because it normally gets
  those from a registry that knows nothing about your endpoint.

If the config exposed the harness's native model string, every prompt file
would be harness-specific. It exposes ours instead, and jormungandr computes
the native one.

## The file

```yaml
version: 1

# ---------------------------------------------------------------- providers
# Declared once. Referenced as <provider>/<model> anywhere a model is named.
providers:
  openrouter:
    kind: openai-compatible          # openai-compatible | anthropic | openai
    base_url: https://openrouter.ai/api/v1
    api_key: ${OPENROUTER_API_KEY}   # env reference only — never a literal
    models:
      deepseek: deepseek/deepseek-v4-flash
      sonnet: anthropic/claude-sonnet-4-5

  local:
    kind: openai-compatible
    base_url: http://host.docker.internal:1234/v1
    api_key: ${LOCAL_API_KEY}
    models:
      qwen: qwen3-coder

# ------------------------------------------------------------------ harness
harness:
  name: droid                        # a registered harness module
  version: "0.176.0"                 # pinned; enters the image digest
  model: openrouter/deepseek         # <provider>/<model> from above

  # Harness-specific settings live ONLY under a block named for the harness.
  droid:
    autonomy: low                    # low | medium | high
    output_format: json              # text | json | stream-json | stream-jsonrpc
    airgap: true                     # skip Factory cloud; required for BYOK
    auto_update: false

  # opencode:
  #   agent: build

# ------------------------------------------------------------------ prompts
prompts:
  file: ./prompts.jsonl              # relative to this config file
  limit: null                        # take the first N, for smoke runs
  shuffle: false

# -------------------------------------------------------------------- image
image:
  base_image: node:22-bookworm-slim
  repository: my-harness
  modules:                           # extra modules; the harness adds its own
    - name: apt
      packages: [git, curl, ca-certificates, ripgrep]
    - name: node
      preinstalled: true
    - name: workspace

# ---------------------------------------------------------------------- run
run:
  concurrency: 4
  timeout: 900
  network: bridge                    # none | bridge | host
  limits: { cpus: 2, memory: 4g, pids: 512 }
  env_files: [./secrets.env]         # where credentials actually come from
  env: { LANGFUSE_HOST: https://cloud.langfuse.com }   # non-secret only
  mounts: []

# ------------------------------------------------------------------- output
output:
  dir: ./runs
  collect_state: true                # copy the harness session record out
```

## Prompt records

`prompts.jsonl`, one JSON object per line:

```jsonl
{"schema_version": "1", "id": "plan-001", "prompt": "Draft a compact project plan"}
{"schema_version": "1", "id": "ui-002", "prompt": "Build a landing page", "follow_up_prompts": ["Now make it responsive", "Add dark mode"]}
{"schema_version": "1", "id": "fix-003", "turns": [{"role": "system", "content": "You are terse."}, {"role": "user", "content": "Fix the failing test"}], "workspace": {"type": "git", "repo": "https://github.com/acme/app", "ref": "a1b2c3d"}, "overrides": {"model": "local/qwen", "timeout": 1800}, "tags": ["regression"], "metadata": {"difficulty": "hard"}}
```

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string, required | Lets the format migrate instead of being sniffed. |
| `id` | string, required | Stable, caller-supplied. Names the output and makes runs resumable. |
| `prompt` / `turns` | string / `[{role, content}]` | Exactly one. `turns` is canonical; `prompt` (+ `follow_up_prompts`) is sugar normalized into it at load. |
| `workspace` | tagged union | `{"type":"none"}` \| `{"type":"local","path":…}` \| `{"type":"git","repo":…,"ref":…}` |
| `overrides` | object | Per-record `{model, timeout, max_turns}`. Run config is the base; these win per key. |
| `tags` | string[] | Curation/selection. |
| `metadata` | object | Free-form. Benchmark-specific data lives here, not in the run fields. |

Deliberate choices, against Teich's equivalent (`PromptInput`,
`src/teich/config.py:244`) and the benchmark formats:

- **`id` is required and never derived from the prompt text.** Teich hashes the
  prompt to identify a run (`runner.py:582`), so two identical prompts collide
  and editing a prompt silently creates a new task rather than showing a diff
  on an existing one. SWE-agent makes the same mistake.
- **`turns` is a role list, not a list of strings.** Teich's
  `follow_up_prompts: list[str]` cannot express a leading system turn or an
  assistant prefill. The role form is strictly more expressive and matches
  Inspect AI's `input: str | list[ChatMessage]`. Teich's spelling is accepted
  as sugar and normalized, so existing files still load.
- **Overrides are namespaced.** Sprinkling `model`/`timeout` at the top level
  blurs "what to run" with "how to run it"; one `overrides` object makes the
  merge rule statable in one sentence.
- **`workspace` is a tagged union, not an overloaded string.** It covers
  Teich's `github_repo`, SWE-bench's `repo` + `base_commit`, and a plain local
  directory without inheriting any of their assumptions.
- **Grading data is not a run input.** SWE-bench's `FAIL_TO_PASS`,
  `PASS_TO_PASS`, `patch`, `test_patch` are curation and scoring concerns; they
  belong in `metadata`, not in the schema every record must satisfy.
- **No `image` per record.** Teich models it, then raises "not supported yet" if
  used (`config.py:544-548`) — dead schema surface that looks supported. It
  also implies one run could span several environments; if two records need
  different images, that is two runs.
- **Unknown keys are an error**, not silently dropped. Silent drops are the
  worst failure mode for a format people hand-author.

## Design rules

### 1. Harness-specific settings never leak into shared blocks

Teich's `model:` block carries `approval_policy`, `sandbox`, `service_tier`
(all Codex-only), `pi_model_overrides` (Pi-only), and `context_length`
(Hermes-ish) alongside genuinely shared `model:` — so a reader cannot tell
which keys apply to the harness they picked, and adding a harness means adding
more keys nobody else uses.

Here, anything harness-specific goes under a block named for that harness, and
a validator rejects a block whose name is not the selected harness — so a stale
`codex:` block left behind after switching to `droid` is an error, not a
silently ignored no-op.

### 2. `harness` and `provider` are different words

Teich calls both "provider": `agent.provider: pi` selects the harness while
`api.provider: openrouter` selects the model vendor. Two unrelated things, one
name, in one file.

### 3. Secrets are env references, never literals

The config names an environment variable; jormungandr writes the *harness's own*
reference syntax into the baked file, so the variable is expanded by the harness
at run time and never by us at build time. One image then works for every user
with their own key, and no credential enters an image layer (layers are readable
by anyone who can pull the image, and `docker history` preserves them).

The syntax is per-harness, which is exactly why the config should not expose it:

| Harness | Written into its config |
|---|---|
| droid | `"apiKey": "${OPENROUTER_API_KEY}"` |
| opencode | `"apiKey": "{env:OPENROUTER_API_KEY}"` |

A literal-looking credential in config is a hard error, reusing the check
`ContainerSpec.env` already performs:

```
error: providers.openrouter.api_key looks like a literal credential ("sk-or-…").
       Baked config is readable by anyone who can pull the image.
       Use ${OPENROUTER_API_KEY} and supply it via run.env_files.
```

Teich permits inline `api.api_key`, `publish.hf_token`, `langfuse.secret_key`
and a legacy top-level `openai_api_key`, all committed to a YAML file people
keep in git.

**Referenced variables are checked before the run starts.** OpenCode replaces an
unset `{env:VAR}` with the empty string rather than failing, so a missing
variable surfaces as a confusing 401 from the provider instead of a config
error. jormungandr resolves every referenced name against the merged
`run.env` + `run.env_files` and refuses to start if one is missing.

### 4. Never fall back to another provider's credential

Teich's key resolution appends `OPENAI_API_KEY` to the fallback list for
*every* provider (`config.py:35`). An ambient `OPENAI_API_KEY` in the shell is
therefore sent to whatever `base_url` is configured — a local endpoint, a
custom vendor, anyone's. That is a credential leak, not a convenience.

Each provider names its own key source and nothing else is consulted. An
unresolved key is a hard failure at load time, not a silent fallback.

### 5. One path-resolution rule

**Every relative path resolves against the config file's directory.** Teich
does this for `prompts_file` alone; `output.traces_dir`, `sandbox_dir`,
`failures_dir` and `codex.auth_dir` resolve against the process CWD instead, so
`teich generate -c ../other/config.yaml` writes its output into the wrong
place. One rule, applied everywhere, no exceptions.

### 6. Layering, and saying so out loud

Later wins: **built-in defaults → config file → `JORM_*` environment →
explicit call arguments.** Env overrides are limited to scalars; anything
structured belongs in the file where it can be reviewed.

Every override is **logged at startup** with its source:

```
config: harness.model = local/qwen  (JORM_MODEL, overriding config value openrouter/deepseek)
```

Teich applies `TEICH_MODEL`/`TEICH_BASE_URL`/`TEICH_API_KEY`/`TEICH_PROVIDER`
to the raw dict *before* validation, so they unconditionally beat explicit
YAML, with no logging and no opt-out. Silent env-beats-file is a debugging
trap: the file says one thing, the run does another, and nothing explains why.

### 7. No legacy aliases with truthy defaults

Teich's `model.approval_mode` is a legacy alias defaulting to the **string**
`"none"` — which is truthy, so its normalizer always fires and always rewrites
`approval_policy`. Writing `approval_policy: on-request` in YAML silently
yields `"never"` (`config.py:202, 205-214`). A deprecated field with a
non-`None` default silently clobbers its replacement.

If a field must be kept for compatibility it defaults to `None` and warns when
actually set.

### 8. Validation is total, and at the right time

- Prompt records are typed as the validated model at load, not
  `list[str | dict[str, Any]]`. Teich defers inline-prompt validation to call
  time, so malformed prompts surface after the banner has printed and
  directories have been created.
- Unknown keys are rejected (`extra="forbid"`), and a cross-field validator
  rejects a harness block that is not the selected harness. Teich silently
  ignores Codex-only keys under a Pi run.
- Multi-turn works identically in every input format. Teich's CSV loader drops
  `follow_up_prompts` entirely, so CSV is silently single-turn while JSONL is
  not.
- Config load does not require files a given command will never read. Teich's
  `prompts_file` existence validator fires on *any* config load, so its own
  Studio has to special-case around it.

## Open questions

1. **Does a run pin one harness, or is a matrix in scope?** The file above runs
   one harness against one model, so comparing two harnesses — or one harness
   across three models — means three config files. That is precisely Teich's
   limitation: one config is one harness × one provider × one model, and a
   sweep needs N files.

   The alternative is a `runs:` list, each entry naming a harness, a provider
   reference and a model, with the rest of the file as shared defaults. That
   makes a sweep expressible in one place at the cost of a second level of
   nesting. Given that comparing harnesses is an obvious near-term want, I now
   lean toward the list — but it is a real fork and worth deciding
   deliberately rather than discovering later.

2. **The stage-ordering hazard blocks the baked-config work.** Harness modules
   run at `Stage.HARNESS` (30); `workspace` creates the user and `HOME` at
   `Stage.USER` (50). A config written to `$HOME` at stage 30 has no home
   directory to land in. The fix is splitting `workspace` into a SYSTEM-stage
   `user` module and a USER-stage `workdir` module — a breaking change to the
   module set, which is why it is called out rather than done.

3. **Should `airgap` default on?** For a container harness using BYOK it is
   almost always right, and without it droid needs a Factory account. But
   defaulting it on silently disables cloud sync for someone who has one.
   Currently opt-in.

4. **Where does baked harness config live in the tier split?** It is
   `Stage.HARNESS`, so the runtime tier — a config edit rebuilds only the
   runtime image. That seems right; if config churns much faster than the
   harness version it might want its own tier.

5. **Provider `kind` vs harness capability.** `kind: anthropic` maps to droid's
   `provider: "anthropic"` and to OpenCode's `@ai-sdk/anthropic` — but the
   OpenCode path is currently broken for a *custom* `baseURL`: the API key is
   dropped and the request 401s
   ([anomalyco/opencode#21737](https://github.com/anomalyco/opencode/issues/21737)),
   while the identical config via `@ai-sdk/openai-compatible` works.

   So `kind` cannot map one-to-one onto a harness's provider type; it needs a
   small per-harness capability table, with a documented fallback (route an
   Anthropic-compatible gateway through the OpenAI-compatible path) and a clear
   error when no route exists. OpenCode additionally needs
   `@ai-sdk/openai-compatible` for `/v1/chat/completions` versus `@ai-sdk/openai`
   for `/v1/responses`, which is a wire-format distinction the config should
   capture rather than make users guess — Teich has the same idea as
   `api.wire_api`.

6. **Config precedence inside OpenCode is counterintuitive** and worth pinning
   down before relying on it: `OPENCODE_CONFIG_CONTENT` (inline JSON) *beats*
   the project `opencode.json`, while `OPENCODE_CONFIG` (a path) *loses* to it.
   Configs are also deep-merged rather than replaced. If we ever inject config
   at run time rather than baking it, the inline-content variable is the only
   one that reliably wins.

## Out of scope

Reading, parsing, or converting harness output. The runner reports where each
session record landed; interpreting it is a separate contract.
