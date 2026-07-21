# Notes and deferred work

Things deliberately not built, with enough context to pick them up.

## `opencode serve` — server mode

OpenCode exposes a first-class headless server:

```
opencode serve --hostname 0.0.0.0 --port 4096
opencode attach <url>
```

It publishes an OpenAPI 3.1 spec at `/doc` with REST endpoints for sessions,
messages, files, config, providers and agents — a better automation surface
than repeated `opencode run` invocations, because one server can serve many
turns (and many clients) without re-entering the CLI each time.

**Not used, deliberately.** Today every turn is a fresh `exec` into a
long-lived container, which is uniform across harnesses: droid has `droid
exec`, opencode has `opencode run`, and the runner does not have to know which
of them can hold a process open. Adopting `serve` for opencode alone would put
two execution models in one runner.

Worth revisiting if turn latency matters, or if a harness appears that *only*
offers a server.

Two things to know before wiring it up:

- `--hostname` defaults to `127.0.0.1`, which inside a container is reachable
  only from that container. Server mode needs `0.0.0.0` plus a published port.
- The lifecycle changes shape: the server must be started, waited for, driven,
  and shut down, where `exec` is a single call that either finishes or does
  not. `ContainerSession` has the primitives, but `PromptRunner` assumes a
  turn is one exec.

## Cloning is public-repo only

`resolve_commit` runs `git ls-remote` on the host with
`GIT_TERMINAL_PROMPT=0`, and the clone itself happens inside the image with no
credentials present. Private repositories are therefore out of scope. Adding
them means getting a credential into the build — a BuildKit secret mount is
the right mechanism, and `installers.ShellInstall` already shows the shape.

## Harness output is not read

`HarnessRun` reports where each session record landed; interpreting it is a
separate contract. That is the boundary this whole runtime exists to feed, and
it is deliberately not crossed here.
