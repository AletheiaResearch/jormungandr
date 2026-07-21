"""Content-addressed image identity.

The tag *is* the cache key: if the inputs are unchanged the tag is unchanged and
the build is skipped; if anything changes the tag changes and a rebuild happens
automatically. There is no timestamp comparison and no manual cache-bust flag.

Two mistakes in the prior art are deliberately not repeated:

* SWE-bench hashes ``str(dict)``, which is insertion-order- and repr-sensitive:
  reordering keys in a constants file, or ``"20"`` vs ``20``, silently changes
  the image name. We hash canonical JSON with sorted keys.

* SWE-bench's base-image hash does not cover the template body, which is why its
  templates carry comments telling you to remember ``--force_rebuild`` after
  editing them. We hash the rendered Dockerfile text and every build-context
  file, so editing a fragment busts the cache on its own.

Teich's staleness check — Dockerfile mtime vs image creation time — is worse
still: ``git checkout`` rewrites mtimes and forces spurious rebuilds, while a
changed build-arg never triggers one.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "DIGEST_LENGTH",
    "LABEL_NAMESPACE",
    "canonical_json",
    "content_digest",
    "image_labels",
    "image_reference",
    "is_valid_tag",
]

DIGEST_LENGTH = 16
"""Truncated hex digits kept in a tag. 16 hex chars = 64 bits: collision-free
in practice for this population, and short enough to read in `docker images`."""

LABEL_NAMESPACE = "dev.jormungandr"

_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
_NAME_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")


def _canonicalize(value: Any) -> Any:
    """Reduce a value to something JSON can serialize deterministically.

    Sets are sorted (their iteration order varies with PYTHONHASHSEED), and
    anything else unserializable is rejected rather than coerced with ``str()``.
    A ``str()`` fallback looks harmless but is a correctness hole: ``str(set)``
    varies per interpreter run and ``str(object)`` embeds a memory address, so a
    module returning either from ``identity()`` would get a fresh digest — and a
    full rebuild — on every invocation, silently.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _canonicalize(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_canonicalize(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    raise TypeError(
        f"cannot hash {type(value).__name__} deterministically: {value!r}. "
        "Return only JSON-native types (or sets) from Module.identity()."
    )


def canonical_json(value: Any) -> str:
    """Serialize deterministically: sorted keys, no insignificant whitespace."""
    return json.dumps(
        _canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def content_digest(
    *,
    dockerfile: str,
    context_files: Mapping[str, str] | None = None,
    context_modes: Mapping[str, int] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """Hash everything that determines the built image.

    Included: the rendered Dockerfile text, every build-context file (path and
    content), and ``extra`` (base image reference, target platform, build args,
    module identities).

    Deliberately excluded: build-time secrets, log paths, run identifiers, and
    anything else that varies per attempt without changing the image. Including
    those would make the cache useless; excluding something that *does* affect
    the image would make it wrong.
    """
    hasher = hashlib.sha256()

    def feed(section: str, payload: str) -> None:
        # Length-prefix each section so no concatenation of one field can be
        # confused with a different split across two.
        encoded = payload.encode("utf-8")
        hasher.update(f"{section}:{len(encoded)}:".encode("ascii"))
        hasher.update(encoded)

    files = context_files or {}
    modes = context_modes or {}
    feed("dockerfile", dockerfile)
    for path in sorted(files):
        feed(f"file:{path}", files[path])
        # Mode is part of the image: a script that goes from 0644 to 0755
        # changes what the build produces without changing a byte of content.
        feed(f"mode:{path}", oct(modes.get(path, 0o644)))
    feed("extra", canonical_json(dict(extra or {})))

    return hasher.hexdigest()[:DIGEST_LENGTH]


def is_valid_tag(tag: str) -> bool:
    return bool(_TAG_RE.match(tag))


def image_reference(repository: str, digest: str, *, prefix: str | None = None) -> str:
    """Build ``repository:<prefix->digest``.

    Separators are chosen to be legal in a Docker reference from the start, so
    no escaping is ever needed. SWE-bench mangles ``__`` into the magic string
    ``_1776_`` with no inverse function anywhere in its codebase — a naming
    scheme that needs escaping is the wrong naming scheme.
    """
    if not _NAME_RE.match(repository):
        raise ValueError(
            f"invalid image repository {repository!r}: must be lowercase "
            "alphanumerics separated by . _ - or /"
        )
    tag = f"{prefix}-{digest}" if prefix else digest
    if not is_valid_tag(tag):
        raise ValueError(f"invalid image tag {tag!r}")
    return f"{repository}:{tag}"


def image_labels(
    *,
    tier: str,
    digest: str,
    modules: tuple[str, ...] = (),
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """OCI labels stamped into every image we build.

    Discovery and garbage collection filter on these labels, never on name
    prefixes. SWE-bench classifies images by string-prefix matching on their
    names, so any unrelated user image sharing that prefix gets collected.
    """
    labels = {
        f"{LABEL_NAMESPACE}.tier": tier,
        f"{LABEL_NAMESPACE}.digest": digest,
        f"{LABEL_NAMESPACE}.managed": "true",
    }
    if modules:
        labels[f"{LABEL_NAMESPACE}.modules"] = ",".join(modules)
    labels.update(extra or {})
    return labels
