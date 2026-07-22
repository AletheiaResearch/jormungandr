# Working in this repo

## Editing files

**Use Read + Edit. Do not edit files by running `python -c`, `sed`, `perl`, or a
heredoc'd script.**

Scripted replacement fails silently and in ways that are hard to see:

- A `str.replace` whose pattern no longer matches does nothing and reports
  success. A call site was left pointing at a renamed function this way, and
  only a test caught it.
- Slicing between two anchors assumes the second appears after the first. When
  it did not, ~160 lines of tests were silently duplicated.
- Repeated scripted edits to a file already restructured earlier compound:
  each one is written against a mental model of the file rather than its
  actual current contents.

`Edit` fails loudly when its `old_string` does not match, and requires the file
to have been read first. That is the whole point — it forces the edit to be
written against what the file actually contains.

Acceptable uses of a shell for file content: creating a new file with `Write`,
generating fixture data, and reading (`grep`, `sed -n` to view). Not mutation.

## Verification

Claims about behaviour need evidence, not reasoning.

- Run the thing. `uv run pytest` for the fast suite, `uv run pytest -m docker`
  for the ones that need a daemon, `uv run pytest -m ""` for everything.
- `tox` runs every gate — tests on 3.14, `ruff check`, `ruff format --check`
  and `mypy` — and is what CI runs, so a green `tox` locally means a green CI.
  `tox -e docker` for the daemon-backed tests; they are deliberately outside
  the default env list so a bare `tox` never needs Docker.
- Do **not** use `uv check` to type check. It runs `ty`, not mypy, and gives
  different answers. The gate is `uv run mypy`, or `tox -e type`.
- A test that would still pass with the implementation removed proves nothing.
  Several in this repo were rewritten after mutation testing showed exactly
  that.
- When fixing a defect found by review, reproduce it first. Two "fixes" here
  were themselves broken (a Go template quoted with `%r`, a chown resolving to
  root) and only a test that genuinely exercised the path revealed it.

## Docker hygiene

Every image and container this tool creates carries `dev.jormungandr.*`
labels. After any run that touches Docker, check nothing leaked:

```sh
docker images --filter 'label=dev.jormungandr.managed=true' -q | wc -l
docker ps -a --filter 'label=dev.jormungandr.session' -q | wc -l
```

Note that Docker propagates a parent image's labels to any child, so the label
alone does not identify what this tool built — see `ImageBuilder.managed_images`.

## Commits

- Author is Nejc Drobnic <nejc@nejc.dev>. No co-author trailers.
- Logical commits, one concern each. Stage explicitly; `git add -A` has twice
  swept unrelated work into the wrong commit here.
- The message should say why, and name the failure mode a change prevents.

## Scope

Do not add capability that was not asked for. A local-directory workspace was
invented, specified, implemented and tested before anyone wanted it, and had
to be removed along with the copy-in/copy-out machinery it dragged behind it.
If something seems missing, say so and ask rather than building it.
