# Configuration specification

**Status:** proposal. Nothing here is implemented yet — this is the design to
argue with before writing code.

## The problem

Today a harness image is described entirely by `ImageSpec`, and harness config
has no home at all. `OpenCode` accepts `version`/`package`/`name`/`requires`;
`Droid` adds `auto_update`/`airgap`. Anything else — a model choice, a provider
endpoint, an MCP server, a system-prompt override — has nowhere to go except
the `script` module, which bakes a shell script and is the wrong tool for a
JSON config file.

Meanwhile the two supported harnesses want genuinely different things:

| | OpenCode | Droid |
|---|---|---|
| Global config | `~/.config/opencode/opencode.json` | `~/.factory/settings.json` |
| Project config | `opencode.json` in project root | — |
| Path override | `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR` | — |
| Inline override | `OPENCODE_CONFIG_CONTENT` (no file needed) | — |
| Credentials | provider env vars | `FACTORY_API_KEY`, or BYOK `apiKey` |
| BYOK | provider blocks in config | `customModels[]` + `FACTORY_AIRGAP_ENABLED` |
| State | `~/.local/share/opencode/` | `~/.factory/{sessions,logs}` |
| Model selection | `--model provider/model` | `sessionDefaultSettings.model` (not `--model`) |

A single flat "harness config" field would flatten those differences and lose.

## The central constraint: build time vs run time

This is the line everything else follows from, and it is the same line
`ImageSpec` / `ContainerSpec` already draw.

**Config that is part of the image** is hashed, cached, and shared. Baking it
means every container from that digest behaves identically — which is the point
of a content-addressed image. Model choice, MCP server definitions, tool
allowlists, system-prompt additions all belong here.

**Config that varies per run must never be baked.** Anything per-attempt
destroys the cache if it enters the digest, and anything secret must not enter
an image layer at all — layers are readable by anyone who can pull the image,
and `docker history` preserves them. Credentials, session ids, and per-task
model overrides belong here.

The rule: *if two runs could legitimately differ on it, it is run-time config.*

## Proposed shape

```yaml
# ---- build time: enters the image digest ----
base_image: node:22-bookworm-slim
repository: my-harness
modules:
  - name: apt
    packages: [git, curl, ca-certificates]
  - name: node
    preinstalled: true
  - name: droid
    version: "0.176.0"
    airgap: true
    config:                       # NEW — baked, hashed
      customModels:
        - model: claude-sonnet-4-5-20250929
          displayName: Sonnet
          baseUrl: https://api.anthropic.com/v1
          apiKey: ${ANTHROPIC_API_KEY}      # resolved in-container, not now
          provider: anthropic
      sessionDefaultSettings:
        model: custom:Sonnet-0
  - name: workspace

# ---- run time: never baked ----
run:
  env_files: [./secrets.env]      # ANTHROPIC_API_KEY, FACTORY_API_KEY
  env:
    LANGFUSE_HOST: https://cloud.langfuse.com   # non-secret only
  network: bridge
  limits: {cpus: 4, memory: 8g}
  mounts:
    - ./repo:/workspace
```

### 1. `config` on harness modules

Each harness module gains an optional `config` mapping which it serializes to
its own config file at its own path, in its own format. The module owns that
knowledge — the caller does not write `~/.factory/settings.json` by hand.

```python
class Droid(Harness):
    CONFIG_PATH = ".factory/settings.json"    # relative to HOME

    def config_document(self) -> str:
        return json.dumps(self.config, sort_keys=True, indent=2)
```

Serialization must be canonical (`sort_keys=True`), for the same reason
`canonical_json` exists in `identity.py`: an unstable byte representation means
an unstable digest and a rebuild on every invocation.

The file lands via `BuildContext.add_file` + `COPY`, so it is a real reviewable
artifact in the build context and part of the digest — not a `printf` inside a
`RUN`.

**Ordering problem to solve.** Harness modules run at `Stage.HARNESS` (30) but
`workspace` creates the user and sets `HOME` at `Stage.USER` (50). A config
written to `$HOME` at stage 30 has no home directory to land in yet. Two
options:

- **(a)** Harness config is `COPY --chown`'d to an absolute path, and the
  harness is pointed at it by env var (`OPENCODE_CONFIG`) where supported.
- **(b)** Split `workspace` into a `user` module at `Stage.SYSTEM` (creates the
  user and `HOME`) and a `workdir` module at `Stage.USER` (final `WORKDIR`/
  `USER`), so `HOME` exists before any harness writes to it.

**(b) is the better shape** — it puts user creation where it belongs by rate of
change, and removes a real ordering hazard rather than working around it. It is
also a breaking change to the module set, so it should land deliberately.

### 2. Secrets never enter the image

`apiKey: ${ANTHROPIC_API_KEY}` is written into the baked config **literally** —
the `${...}` is expanded by the harness at run time, not by us at build time.
Droid documents this for `settings.json`, and it means the same image works for
every user with their own key.

Where a harness has no such indirection, the config field must be omitted from
the baked file and supplied by env var at run time instead. A validator should
reject anything that looks like a literal credential in build-time config:

```
error: modules.droid.config.customModels[0].apiKey looks like a literal
       credential ("sk-ant-…"). Baked config is readable by anyone who can
       pull the image. Use ${VAR} and supply it via run.env_files.
```

This mirrors the check `ContainerSpec.env` already performs, and should reuse it.

### 3. Run-time config is `ContainerSpec`, not a new thing

The `run:` block deserializes to the existing `ContainerSpec`. No new model, and
it inherits the secret-refusing validator and the conservative resource
defaults. `PromptRunner` already accepts a `ContainerSpec`.

### 4. What a full config file looks like

```yaml
version: 1                        # explicit, so the schema can evolve
image:                            # -> ImageSpec
  base_image: node:22-bookworm-slim
  repository: my-harness
  tier_split: 20
  modules: [...]
run:                              # -> ContainerSpec
  network: none
  env_files: [./secrets.env]
prompts:                          # -> PromptRunner
  harness: droid
  model: null
  timeout: 900
  collect_state_to: ./artifacts
```

Three top-level keys, each deserializing to a model that already exists. The
config file is a thin envelope, not a parallel universe.

## Open questions

1. **Should `airgap` default to true?** For a container harness with BYOK it is
   almost always right, and it is what makes running without a Factory account
   possible. But defaulting it on silently disables cloud sync for someone who
   does have an account. Currently opt-in.

2. **Project-level config.** OpenCode reads `opencode.json` from the project
   root — i.e. the mounted workspace, which is run-time data we do not control.
   Do we leave that entirely to the user, or offer to write it into the mount?

3. **`OPENCODE_CONFIG_CONTENT`** lets config be passed with no file at all. That
   is attractive (nothing baked, nothing mounted) but it is per-harness and
   makes config invisible to the digest. Probably a run-time-override escape
   hatch rather than the primary mechanism.

4. **Config validation against harness schemas.** We could validate
   `customModels[].provider` against droid's three accepted values and catch
   errors at compose time instead of at run time. That means encoding
   per-harness schemas we do not own and that drift. Worth it for the fields
   that are cheap and stable; not worth chasing completeness.

5. **Does baked config belong in the base or runtime tier?** It is at
   `Stage.HARNESS`, so runtime — meaning a config edit rebuilds only the runtime
   image. That seems right, but if config churns much more than the harness
   version it might deserve its own tier above it.

## What this does not cover

Reading, parsing, or converting harness output. `HarnessRun` reports where the
session record is; interpreting it is a separate contract, deliberately not
specified here.
