# jormungandr

Compose Docker images and run containers for agent harnesses.

This is the runtime layer: it builds images that can run agent CLIs (Claude
Code, Codex, Gemini, opencode) and manages the containers they run in. It does
not read, parse, or convert traces — that lives elsewhere.

## Install

```sh
uv sync
```

Requires the `docker` CLI and a running daemon. No Python Docker SDK is used;
see [Why the docker CLI](#why-the-docker-cli).

## Quick start

Describe an image:

```yaml
# harness.yaml
base_image: node:22-bookworm-slim
repository: my-harness
modules:
  - name: apt
    packages: [git, curl, ca-certificates]
  - name: node
    preinstalled: true          # the base image already has it
  - name: agent
    harness: claude-code
    version: "2.0.1"
  - name: workspace
```

Inspect, build, run:

```sh
jormungandr image render harness.yaml     # see the Dockerfile, build nothing
jormungandr image build  harness.yaml     # build (skipped if unchanged)
jormungandr image modules                 # list available modules

jormungandr container run my-harness:<digest> --name work sleep infinity
jormungandr container exec work claude --version
jormungandr container list
jormungandr container reap                # clean up after crashed runs
```

## How it works

### Modules

An image is a base plus an ordered list of modules. A module contributes
Dockerfile instructions and, optionally, files baked into the build context.

Built-ins: `apt`, `node`, `python`, `agent`, `langfuse`, `script`, `workspace`.

Modules are ordered by a topological sort over their `requires`, with ties
broken by `(stage, name)` — never by the order you happened to list them in,
because that order feeds the image hash.

Stages run in rate-of-change order, so the layers that change most often sit
on top and invalidate the least below them:

| Stage | Purpose |
|---|---|
| `SYSTEM` | OS packages, users |
| `TOOLCHAIN` | language runtimes (node, python, uv) |
| `HARNESS` | agent CLIs |
| `INTEGRATION` | tracing, proxies, plugins |
| `USER` | caller scripts, workspace |

Adding a capability is one class, one registry line, one test:

```python
from jormungandr.runtime.layers import Run
from jormungandr.runtime.modules import REGISTRY, Stage

class Ripgrep:
    name = "ripgrep"
    stage = Stage.SYSTEM
    requires = ()

    def instructions(self, context):
        return [Run("apt-get update && apt-get install -y ripgrep")]

    def identity(self):
        return {}

REGISTRY.register("ripgrep", Ripgrep)
```

Third parties can ship modules without forking, via entry points:

```toml
[project.entry-points."jormungandr.modules"]
ripgrep = "my_package.modules:Ripgrep"
```

### Image identity

The tag *is* the cache key. It is a SHA-256 over the rendered Dockerfile, every
build-context file, and the resolved module configuration:

```
my-harness:4c6616efbec2d6c0
```

Change a package, a script, a pinned version, or the base image, and the tag
changes and a rebuild happens. Change nothing and the build is skipped. There
is no mtime check and no force-rebuild flag to remember.

Every image is stamped with OCI labels (`dev.jormungandr.*`). Discovery and
pruning filter on those labels, never on name prefixes, so `image prune` can
never touch an unrelated image of yours.

### Containers

Containers are long-lived and driven by repeated `exec`, which suits an agent
loop: many commands, shared filesystem state, host-side inspection in between.

Defaults are conservative — CPU, memory, and PID limits; `--cap-drop ALL`;
`no-new-privileges`; `--init` to reap zombies. Relax them explicitly via
`ResourceLimits`.

Secrets go in `--env-file`, never `-e`. `ContainerSpec` rejects env keys that
look like credentials, because `-e KEY=value` is visible in host `ps` and is
recorded permanently in `docker inspect`.

Crash recovery works because every container is labelled at creation. `atexit`
and signal handlers cover the ordinary cases; `jormungandr container reap`
covers SIGKILL, which no in-process handler can.

## Why the docker CLI

Not docker-py. It drives the legacy build endpoint, which Docker has
deprecated ("the legacy builder is deprecated and will be removed in a future
release") and which cannot reach BuildKit — so `RUN --mount=type=cache` is
unavailable. Cache mounts are the largest available build-speed win for
repeated apt/npm installs.

`docker exec` also hands back the exit status as a subprocess return code with
already-separated stdout/stderr, where the HTTP API needs a follow-up
`exec_inspect` and manual stream demultiplexing.

The cost is parsing `docker inspect` JSON instead of getting typed objects.
That is confined to `runtime/docker.py`.

## Development

```sh
uv run pytest              # fast suite, no daemon needed
uv run pytest -m docker    # integration tests, requires Docker
uv run pytest -m ""        # everything
```

Composition, ordering, and hashing are pure functions, so they test without a
daemon. Anything that touches Docker is behind the `docker` marker.

## Layout

```
src/jormungandr/
  cli.py                 declarations only; heavy imports live in commands/
  commands/              command implementations
  runtime/
    layers.py            typed Dockerfile instructions + renderer
    modules/             the composable module contract, registry, built-ins
    spec.py              ImageSpec / ContainerSpec / ResourceLimits
    compose.py           spec -> Dockerfile + context + digest (pure)
    identity.py          content hashing, tags, labels
    docker.py            thin typed wrapper over the docker CLI
    build.py             context assembly, BuildKit invocation, logs
    container.py         lifecycle, labelling, reaping
```
