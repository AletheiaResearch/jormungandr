from __future__ import annotations

import pytest

from jormungandr.runtime.identity import (
    DIGEST_LENGTH,
    canonical_json,
    content_digest,
    image_labels,
    image_reference,
    is_valid_tag,
)


class TestCanonicalJson:
    def test_key_order_does_not_matter(self) -> None:
        # SWE-bench hashes str(dict), so this exact case changes its image name.
        assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})

    def test_types_are_distinguished(self) -> None:
        assert canonical_json({"v": "20"}) != canonical_json({"v": 20})

    def test_no_insignificant_whitespace(self) -> None:
        assert canonical_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'

    def test_unknown_types_do_not_raise(self) -> None:
        assert canonical_json({"p": object()}).startswith('{"p":"<object')


class TestContentDigest:
    def test_stable(self) -> None:
        a = content_digest(dockerfile="FROM alpine\n")
        b = content_digest(dockerfile="FROM alpine\n")
        assert a == b
        assert len(a) == DIGEST_LENGTH

    def test_dockerfile_change_busts_cache(self) -> None:
        # The bug SWE-bench documents but does not fix: editing a template must
        # change the tag on its own, with no --force-rebuild.
        a = content_digest(dockerfile="FROM alpine:3.20\n")
        b = content_digest(dockerfile="FROM alpine:3.21\n")
        assert a != b

    def test_context_file_content_change_busts_cache(self) -> None:
        a = content_digest(dockerfile="FROM alpine\n", context_files={"s.sh": "echo 1"})
        b = content_digest(dockerfile="FROM alpine\n", context_files={"s.sh": "echo 2"})
        assert a != b

    def test_context_file_rename_busts_cache(self) -> None:
        a = content_digest(dockerfile="FROM alpine\n", context_files={"a.sh": "x"})
        b = content_digest(dockerfile="FROM alpine\n", context_files={"b.sh": "x"})
        assert a != b

    def test_context_file_order_does_not_matter(self) -> None:
        a = content_digest(dockerfile="F", context_files={"a": "1", "b": "2"})
        b = content_digest(dockerfile="F", context_files={"b": "2", "a": "1"})
        assert a == b

    def test_extra_change_busts_cache(self) -> None:
        a = content_digest(dockerfile="F", extra={"platform": "linux/arm64"})
        b = content_digest(dockerfile="F", extra={"platform": "linux/amd64"})
        assert a != b

    def test_extra_key_order_does_not_matter(self) -> None:
        a = content_digest(dockerfile="F", extra={"x": 1, "y": 2})
        b = content_digest(dockerfile="F", extra={"y": 2, "x": 1})
        assert a == b

    def test_field_boundaries_are_unambiguous(self) -> None:
        # Without length-prefixing, these two could hash identically by
        # concatenating differently across the dockerfile/extra boundary.
        a = content_digest(dockerfile="AB", extra={})
        b = content_digest(dockerfile="A", extra={})
        assert a != b

    def test_split_between_files_is_unambiguous(self) -> None:
        a = content_digest(dockerfile="F", context_files={"a": "xy", "b": "z"})
        b = content_digest(dockerfile="F", context_files={"a": "x", "b": "yz"})
        assert a != b


class TestImageReference:
    def test_basic(self) -> None:
        assert image_reference("jormungandr", "deadbeef") == "jormungandr:deadbeef"

    def test_with_prefix(self) -> None:
        ref = image_reference("jormungandr", "deadbeef", prefix="base")
        assert ref == "jormungandr:base-deadbeef"

    def test_namespaced_repository(self) -> None:
        assert image_reference("acme/jorm", "abc").startswith("acme/jorm:")

    def test_uppercase_repository_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid image repository"):
            image_reference("Jormungandr", "abc")

    def test_underscore_separator_repository_rejected(self) -> None:
        # A naming scheme that needs escaping is the wrong naming scheme.
        with pytest.raises(ValueError, match="invalid image repository"):
            image_reference("acme__jorm", "abc")

    def test_tag_validation(self) -> None:
        assert is_valid_tag("base-deadbeef")
        assert not is_valid_tag("-leading-dash")
        assert not is_valid_tag("has space")
        assert not is_valid_tag("")


class TestImageLabels:
    def test_core_labels(self) -> None:
        labels = image_labels(tier="runtime", digest="abc123")
        assert labels["dev.jormungandr.tier"] == "runtime"
        assert labels["dev.jormungandr.digest"] == "abc123"
        assert labels["dev.jormungandr.managed"] == "true"

    def test_modules_recorded(self) -> None:
        labels = image_labels(tier="runtime", digest="a", modules=("apt", "node"))
        assert labels["dev.jormungandr.modules"] == "apt,node"

    def test_no_module_label_when_empty(self) -> None:
        assert "dev.jormungandr.modules" not in image_labels(tier="base", digest="a")

    def test_extra_labels_merge(self) -> None:
        labels = image_labels(tier="base", digest="a", extra={"custom": "v"})
        assert labels["custom"] == "v"
