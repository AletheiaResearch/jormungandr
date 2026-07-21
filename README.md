# jormungandr

Compose Docker images and run containers for agent harnesses.

This is the runtime layer: it builds images that can run agent CLIs and manages
the containers they run in. It does not read, parse, or convert traces.

**Status: internals only.** There is no CLI surface yet, and no logic for
actually *running* a harness — only the mechanism for building the image and
managing containers. OpenCode is the first supported harness; others follow.

## Install

```sh
uv sync
```

Requires the `docker` CLI and a running daemon. No Python Docker SDK is used;
see [Why the docker CLI](#why-the-docker-cli).

## Usage

```python
from pathlib import Path

from jormungandr.runtime.build import ImageBuilder
from jormungandr.runtime.spec import ImageSpec
import jormungandr.runtime.modules.builtin  # registers the built-in modules

spec = ImageSpec(
    base_image="node:22-bookworm-slim",
    repository="my-harness",
    modules=[
        {"name": "apt", "packages": ["git", "curl", "ca-certificates"]},
        {"name": "node", "preinstalled": True},   # base image already has it
        {"name": "opencode"},
        {"name": "workspace"},
    ],
)

result = ImageBuilder().build(spec)
for layer in result.layers:
    print(layer.tier, "cached" if layer.cached else "built", layer.reference)
# base     built  my-harness:base-9b643a15e3ad0303
# runtime  built  my-harness:runtime-f725284cd47b8563
```

## How it works

### Tiers

An image is built as two independently tagged, independently cached tiers:

| Tier | Contents | Changes |
|---|---|---|
| `base` | OS packages, language toolchains | rarely; shared by every runtime on it |
| `runtime` | harnesses, integrations, user scripts | often |

The runtime's `FROM` names the base by its content-addressed tag, so a base
change propagates into the runtime digest automatically — there is no separate
bookkeeping to forget.

Bumping a harness version rebuilds only the runtime; the base build is skipped
outright, not merely layer-cached:

```
bumped opencode 1.18.4 -> 1.18.3 in 9.6s
  base     CACHED  my-harness:base-9b643a15e3ad0303
  runtime  BUILT   my-harness:runtime-9edc625adab4415d
```

The split point is `ImageSpec.tier_split` (default `Stage.TOOLCHAIN`). Raise it
above `USER` to put everything in the base tier; lower it below `SYSTEM` to put
everything in the runtime tier.

Why tiers at all, when stage ordering already lets BuildKit's layer cache skip
the expensive prefix: that cache is local and evictable. A fresh CI runner or a
`docker builder prune` rebuilds everything, whereas a separately tagged base
image can be pulled.

### Modules

An image is a base plus an ordered list of modules. A module contributes
Dockerfile instructions and, optionally, files baked into the build context.

Built-ins: `apt`, `node`, `python`, `opencode`, `langfuse`, `script`,
`workspace`. Only `opencode` is a harness; see below.

Modules are ordered by a topological sort over their `requires`, with ties
broken by `(stage, name)` — never by the order you happened to list them in,
because that order feeds the image hash.

Stages run in rate-of-change order, and also determine the tier split:

| Stage | Purpose | Default tier |
|---|---|---|
| `SYSTEM` (10) | OS packages, users | base |
| `TOOLCHAIN` (20) | language runtimes (node, python, uv) | base |
| `HARNESS` (30) | agent CLIs | runtime |
| `INTEGRATION` (40) | tracing, proxies, plugins | runtime |
| `USER` (50) | caller scripts, workspace | runtime |

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

### Harnesses and installers

Harnesses have no common install mechanism, so the install method is a
*strategy* a harness composes rather than a fixed part of what a harness is:

| Installer | Shape | Used by |
|---|---|---|
| `NpmGlobal` | `npm install -g pkg@version` | opencode, pi |
| `ShellInstall` | download, checksum, run | droid |
| `GitPythonApp` | clone → `uv venv` → editable install → PATH shim | hermes |

`GitPythonApp` is the one that rules out a package-manager-shaped base class:
it needs git and Python as prerequisites, installs into a library directory,
and has to emit a shell shim. Adding a fourth shape (release tarball, distro
package, prebuilt binary) is one class in `installers.py` — no harness module
changes.

What harnesses *do* share lives in `Harness`: the `HARNESS` stage, version
identity, and a post-install verification step. That step matters because
installers lie — npm exits 0 even when no platform-specific optional binary
matched, so running the program is the only proof the install is usable.

```python
Harness(
    name="hermes",
    installer=GitPythonApp(repo, ref="v1.0.0", binary="hermes"),
    binary="hermes",
)
```

`ShellInstall` warns in the generated Dockerfile when given no `sha256`, since
an unpinned remote installer makes the image contents depend on whatever the
URL served at build time.

**Only OpenCode is registered as a built-in.** The other shapes are exercised
in tests but not shipped as modules, because their package names and versions
have not been verified by an actual build.

OpenCode ships as an npm package whose real payload is a set of
platform-specific prebuilt binaries published as optional dependencies
(`opencode-linux-arm64`, `opencode-linux-x64`, plus musl and baseline
variants). npm resolves the right one for the platform it installs on, so the
ordinary global install works on both glibc and musl bases — verified against
`node:22-bookworm-slim` and `node:22-alpine`. The version is pinned by default;
an unpinned `@latest` would make the digest lie.

### Image identity

The tag *is* the cache key: a SHA-256 over the rendered Dockerfile, every
build-context file and its mode, the parent reference, and the resolved module
configuration.

```
my-harness:runtime-f725284cd47b8563
```

Change a package, a script, a pinned version, or the base image, and the tag
changes and a rebuild happens. Change nothing and the build is skipped. There is
no mtime check and no force-rebuild flag to remember.

Every image is stamped with OCI labels (`dev.jormungandr.*`). Discovery and
pruning filter on those labels, never on name prefixes, so `prune` can never
touch an unrelated image of yours.

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
and signal handlers cover the ordinary cases; `ContainerRuntime.reap_orphans()`
covers SIGKILL, which no in-process handler can. It removes only containers
whose owning process is gone, so a concurrent session's containers survive.

## Why the docker CLI

Not docker-py. It drives the legacy build endpoint, which Docker has deprecated
("the legacy builder is deprecated and will be removed in a future release") and
which cannot reach BuildKit — so `RUN --mount=type=cache` is unavailable. Cache
mounts are the largest available build-speed win for repeated apt/npm installs.

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
  cli.py                 launcher only; no commands wired up yet
  runtime/
    layers.py            typed Dockerfile instructions + renderer
    modules/             module contract, registry, installers, built-ins
    spec.py              ImageSpec / ContainerSpec / ResourceLimits
    compose.py           spec -> tiered Dockerfiles + contexts + digests (pure)
    identity.py          content hashing, tags, labels
    docker.py            thin typed wrapper over the docker CLI
    build.py             context assembly, BuildKit invocation, logs
    container.py         lifecycle, labelling, reaping
```
