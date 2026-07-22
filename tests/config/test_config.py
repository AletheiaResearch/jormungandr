from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from jormungandr.config import (
    ConfigError,
    JormConfig,
    ProviderSpec,
    compile_image_spec,
    load_config,
    load_prompts,
    resolve_prompts,
)
from jormungandr.config.prompts import PromptRecord
from jormungandr.config.providers import parse_model_ref
from jormungandr.runtime.compose import compose

BASE_CONFIG = """
version: 1
providers:
  openrouter:
    kind: openai-compatible
    base_url: https://openrouter.ai/api/v1
    api_key: ${OPENROUTER_API_KEY}
    context_window: 128000
    models:
      deepseek: deepseek/deepseek-v4-flash
harness:
  name: droid
  model: openrouter/deepseek
  droid:
    airgap: true
prompts:
  file: ./prompts.jsonl
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "jorm.yaml").write_text(BASE_CONFIG)
    (tmp_path / "prompts.jsonl").write_text('{"id": "a", "prompt": "hello"}\n')
    return tmp_path


class TestProviderSpec:
    def test_api_key_must_be_an_env_reference(self) -> None:
        with pytest.raises(ValidationError, match="environment reference"):
            ProviderSpec(base_url="https://x/v1", api_key="sk-ant-real", models={"a": "b"})

    def test_literal_credential_is_called_out(self) -> None:
        with pytest.raises(ValidationError, match="literal credential"):
            ProviderSpec(base_url="https://x/v1", api_key="sk-or-abc", models={"a": "b"})

    def test_env_reference_is_accepted(self) -> None:
        spec = ProviderSpec(
            base_url="https://x/v1", api_key="${MY_KEY}", models={"a": "b"}
        )
        assert spec.env_var == "MY_KEY"

    def test_models_cannot_be_empty(self) -> None:
        with pytest.raises(ValidationError):
            ProviderSpec(base_url="https://x/v1", api_key="${K}", models={})

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProviderSpec(
                base_url="https://x/v1", api_key="${K}", models={"a": "b"}, typo=1
            )


class TestModelRef:
    def test_splits_on_first_slash_only(self) -> None:
        # A model alias may itself contain slashes.
        assert parse_model_ref("openrouter/deepseek/v4") == ("openrouter", "deepseek/v4")

    def test_missing_slash_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected '<provider>/<model>'"):
            parse_model_ref("justamodel")


class TestJormConfig:
    def test_loads(self, project: Path) -> None:
        config = load_config(project / "jorm.yaml", apply_env=False)
        assert config.harness.name == "droid"
        assert config.harness.settings == {"airgap": True}

    def test_paths_resolve_against_the_config_file(self, project: Path, tmp_path) -> None:
        # Not the process CWD — running with a config from elsewhere must not
        # read and write in the wrong place.
        config = load_config(project / "jorm.yaml", apply_env=False)
        assert config.prompts.file == (project / "prompts.jsonl").resolve()

    def test_unknown_model_reference_is_rejected(self, project: Path) -> None:
        (project / "jorm.yaml").write_text(
            BASE_CONFIG.replace("model: openrouter/deepseek", "model: openrouter/ghost")
        )
        with pytest.raises(ConfigError, match="which declares"):
            load_config(project / "jorm.yaml", apply_env=False)

    def test_unknown_provider_reference_is_rejected(self, project: Path) -> None:
        (project / "jorm.yaml").write_text(
            BASE_CONFIG.replace("model: openrouter/deepseek", "model: ghost/deepseek")
        )
        with pytest.raises(ConfigError, match="not declared"):
            load_config(project / "jorm.yaml", apply_env=False)

    def test_foreign_harness_block_is_an_error(self, project: Path) -> None:
        # A stale block for another harness looks configured and does nothing.
        (project / "jorm.yaml").write_text(
            BASE_CONFIG.replace(
                "  droid:\n    airgap: true\n",
                "  droid:\n    airgap: true\n  opencode:\n    agent: build\n",
            )
        )
        with pytest.raises(ConfigError, match="harness.opencode is set"):
            load_config(project / "jorm.yaml", apply_env=False)

    def test_unknown_top_level_key_rejected(self, project: Path) -> None:
        (project / "jorm.yaml").write_text(BASE_CONFIG + "\ntypo: true\n")
        with pytest.raises(ConfigError):
            load_config(project / "jorm.yaml", apply_env=False)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nope.yaml")

    def test_required_env_is_reported(self, project: Path) -> None:
        config = load_config(project / "jorm.yaml", apply_env=False)
        assert config.required_env == {"OPENROUTER_API_KEY"}
        assert config.missing_env(set()) == {"OPENROUTER_API_KEY"}
        assert config.missing_env({"OPENROUTER_API_KEY"}) == set()

    def test_config_load_does_not_require_the_prompt_file(self, project: Path) -> None:
        # A command that never reads prompts should not fail because the file
        # is missing.
        (project / "prompts.jsonl").unlink()
        load_config(project / "jorm.yaml", apply_env=False)

    def test_env_override_applies_and_is_logged(self, project: Path, caplog) -> None:
        import logging

        with caplog.at_level(logging.INFO):
            config = load_config(
                project / "jorm.yaml",
                environ={"JORM_MODEL": "openrouter/deepseek", "JORM_CONCURRENCY": "8"},
            )
        assert config.run.concurrency == 8
        assert any("JORM_CONCURRENCY" in r.getMessage() for r in caplog.records)

    def test_env_override_can_be_disabled(self, project: Path) -> None:
        config = load_config(
            project / "jorm.yaml", environ={"JORM_CONCURRENCY": "8"}, apply_env=False
        )
        assert config.run.concurrency == 1


class TestPromptRecords:
    """Teich's prompts.jsonl format — an existing file must load unchanged."""

    def test_a_teich_record_loads(self) -> None:
        record = PromptRecord(
            prompt="Do the thing",
            follow_up_prompts=["And this"],
            system="Be terse.",
            github_repo="acme/app",
        )
        assert record.user_turns == ("Do the thing", "And this")
        assert record.system == "Be terse."

    def test_prompt_is_required(self) -> None:
        with pytest.raises(ValidationError):
            PromptRecord(follow_up_prompts=["x"])

    def test_id_is_optional(self) -> None:
        # Teich files have no id; one is derived positionally at load.
        assert PromptRecord(prompt="x").id is None

    def test_crlf_is_normalized(self) -> None:
        assert PromptRecord(prompt="a\r\nb").prompt == "a\nb"

    def test_literal_none_system_becomes_none(self) -> None:
        # Teich treats the string "none" as unset.
        assert PromptRecord(prompt="x", system="none").system is None

    def test_empty_follow_up_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot be empty"):
            PromptRecord(prompt="x", follow_up_prompts=["ok", "  "])

    def test_follow_ups_must_be_a_list(self) -> None:
        with pytest.raises(ValidationError, match="must be a list"):
            PromptRecord(prompt="x", follow_up_prompts="not a list")

    def test_system_becomes_the_first_turn(self) -> None:
        turns = PromptRecord(prompt="go", system="be terse").turns
        assert turns[0].role == "system" and turns[0].content == "be terse"
        assert turns[1].role == "user"

    def test_github_repo_becomes_a_git_workspace(self) -> None:
        record = PromptRecord(prompt="x", github_repo="acme/app")
        assert record.workspace.type == "git"
        assert record.workspace.git.clone_url == "https://github.com/acme/app"
        # Teich has no ref, so the default branch is used.
        assert record.workspace.git.ref is None

    def test_malformed_github_repo_rejected(self) -> None:
        with pytest.raises(ValidationError, match="owner/repo"):
            PromptRecord(prompt="x", github_repo="not-a-repo")

    def test_no_github_repo_means_no_workspace(self) -> None:
        assert PromptRecord(prompt="x").workspace.type == "none"

    def test_per_record_image_is_rejected_at_parse_time(self) -> None:
        # Teich models this field and then refuses it at use time, after the
        # banner has printed and directories exist.
        with pytest.raises(ValidationError, match="one run builds one image"):
            PromptRecord(prompt="x", image="other:tag")

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PromptRecord(prompt="x", promt="typo")

    def test_a_local_directory_is_not_a_workspace_source(self) -> None:
        # There is no host-side workspace: a repository is materialized as an
        # image tier, so a path on the host has nowhere to go.
        with pytest.raises(ValidationError):
            PromptRecord(prompt="x", workspace={"type": "local", "path": "/src"})

    def test_overrides_are_namespaced(self) -> None:
        assert PromptRecord(prompt="x", overrides={"timeout": 60}).overrides.timeout == 60

    def test_no_per_record_model_override(self) -> None:
        with pytest.raises(ValidationError):
            PromptRecord(prompt="x", overrides={"model": "a/b"})


class TestLoadPrompts:
    def write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "p.jsonl"
        path.write_text(body)
        return path

    def test_reads_a_teich_file(self, tmp_path: Path) -> None:
        path = self.write(
            tmp_path,
            '{"prompt":"x"}\n'
            '{"prompt":"y","follow_up_prompts":["z"]}\n',
        )
        records = load_prompts(path)
        assert [r.prompt for r in records] == ["x", "y"]
        assert records[1].user_turns == ("y", "z")

    def test_ids_are_derived_positionally(self, tmp_path: Path) -> None:
        # Not from the prompt text: Teich hashes it, so identical prompts
        # collide and an edit silently creates a new run.
        path = self.write(tmp_path, '{"prompt":"same"}\n{"prompt":"same"}\n')
        assert [r.id for r in load_prompts(path)] == ["prompt-0000", "prompt-0001"]

    def test_explicit_ids_are_honoured(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, '{"id":"mine","prompt":"x"}\n')
        assert load_prompts(path)[0].id == "mine"

    def test_a_bare_string_is_a_prompt(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, '"just a prompt"\n')
        assert load_prompts(path)[0].prompt == "just a prompt"

    def test_blank_and_comment_lines_skipped(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, '\n# a note\n{"prompt":"x"}\n\n')
        assert len(load_prompts(path)) == 1

    def test_bom_is_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "p.jsonl"
        path.write_bytes(b'\xef\xbb\xbf{"prompt":"x"}\n')
        assert load_prompts(path)[0].prompt == "x"

    def test_errors_report_the_line_number(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, '{"prompt":"x"}\nnot json\n')
        with pytest.raises(ValueError, match="line 2"):
            load_prompts(path)

    def test_duplicate_ids_rejected(self, tmp_path: Path) -> None:
        # Ids name output directories, so a collision would overwrite results.
        path = self.write(tmp_path, '{"id":"a","prompt":"x"}\n{"id":"a","prompt":"y"}\n')
        with pytest.raises(ValueError, match="duplicate id"):
            load_prompts(path)

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="no prompt records"):
            load_prompts(self.write(tmp_path, "\n\n"))

    def test_limit_is_applied(self, project: Path) -> None:
        (project / "prompts.jsonl").write_text(
            '{"prompt":"x"}\n{"prompt":"y"}\n'
        )
        (project / "jorm.yaml").write_text(BASE_CONFIG + "  limit: 1\n")
        config = load_config(project / "jorm.yaml", apply_env=False)
        assert len(resolve_prompts(config)) == 1


class TestCompileToImageSpec:
    def compiled(self, project: Path):
        return compose(compile_image_spec(load_config(project / "jorm.yaml", apply_env=False)))

    def test_user_module_precedes_the_harness(self, project: Path) -> None:
        # HOME must exist before the harness bakes config into it.
        spec = compile_image_spec(load_config(project / "jorm.yaml", apply_env=False))
        names = [m.name for m in spec.modules]
        assert names.index("user") < names.index("droid")
        assert names[-1] == "workdir"
        # the harness's own prerequisite is added rather than demanded
        assert "node" in names

    def test_user_lands_in_the_base_tier(self, project: Path) -> None:
        result = self.compiled(project)
        assert "user" in result.base.module_names
        assert "droid" in result.runtime.module_names

    def test_droid_config_is_baked(self, project: Path) -> None:
        result = self.compiled(project)
        document = json.loads(result.runtime.context_files["droid.config.json"])
        model = document["customModels"][0]
        assert model["baseUrl"] == "https://openrouter.ai/api/v1"
        assert model["model"] == "deepseek/deepseek-v4-flash"
        # droid's own syntax
        assert model["apiKey"] == "${OPENROUTER_API_KEY}"
        # --model rejects custom ids, so selection goes through the defaults
        assert document["sessionDefaultSettings"]["model"] == "custom:openrouter-deepseek-0"

    def test_opencode_config_is_baked_in_its_own_shape(self, project: Path) -> None:
        (project / "jorm.yaml").write_text(
            BASE_CONFIG.replace("name: droid", "name: opencode")
            .replace("  droid:\n    airgap: true\n", "")
        )
        result = self.compiled(project)
        document = json.loads(result.runtime.context_files["opencode.config.json"])
        provider = document["provider"]["openrouter"]
        assert provider["npm"] == "@ai-sdk/openai-compatible"
        assert provider["options"]["baseURL"] == "https://openrouter.ai/api/v1"
        # opencode's own syntax — ${VAR} does not work for apiKey here
        assert provider["options"]["apiKey"] == "{env:OPENROUTER_API_KEY}"
        # custom models need explicit limits or context accounting breaks
        assert provider["models"]["deepseek/deepseek-v4-flash"]["limit"]["context"] == 128000
        assert document["model"] == "openrouter/deepseek/deepseek-v4-flash"

    def test_the_same_config_yields_different_native_shapes(self, project: Path) -> None:
        droid = self.compiled(project).runtime.context_files["droid.config.json"]
        (project / "jorm.yaml").write_text(
            BASE_CONFIG.replace("name: droid", "name: opencode")
            .replace("  droid:\n    airgap: true\n", "")
        )
        opencode = self.compiled(project).runtime.context_files["opencode.config.json"]
        assert droid != opencode
        assert "${OPENROUTER_API_KEY}" in droid
        assert "{env:OPENROUTER_API_KEY}" in opencode

    def test_provider_change_is_part_of_the_digest(self, project: Path) -> None:
        before = self.compiled(project).runtime.digest
        (project / "jorm.yaml").write_text(
            BASE_CONFIG.replace("https://openrouter.ai/api/v1", "https://other.example/v1")
        )
        assert self.compiled(project).runtime.digest != before

    def test_a_field_the_harness_ignores_does_not_rebuild_it(self, project: Path) -> None:
        # context_window feeds OpenCode's limit.context; droid has no
        # equivalent, so a droid image legitimately does not change. The digest
        # tracks what is in the image, not what is in the config file.
        before = self.compiled(project).runtime.digest
        (project / "jorm.yaml").write_text(
            BASE_CONFIG.replace("context_window: 128000", "context_window: 64000")
        )
        assert self.compiled(project).runtime.digest == before

    def test_but_it_does_rebuild_opencode(self, project: Path) -> None:
        opencode_config = BASE_CONFIG.replace("name: droid", "name: opencode").replace(
            "  droid:\n    airgap: true\n", ""
        )
        (project / "jorm.yaml").write_text(opencode_config)
        before = self.compiled(project).runtime.digest
        (project / "jorm.yaml").write_text(
            opencode_config.replace("context_window: 128000", "context_window: 64000")
        )
        assert self.compiled(project).runtime.digest != before

    def test_harness_settings_reach_the_image(self, project: Path) -> None:
        # Asserting the default proves nothing about whether harness.<name>
        # settings are wired through at all — airgap defaults to true, so the
        # image would look identical with the block ignored. Set a
        # non-default and check the image changes.
        default = self.compiled(project).runtime.dockerfile
        assert "FACTORY_AIRGAP_ENABLED=true" in default

        (project / "jorm.yaml").write_text(
            BASE_CONFIG.replace("    airgap: true", "    airgap: false")
        )
        overridden = self.compiled(project).runtime.dockerfile
        assert "FACTORY_AIRGAP_ENABLED" not in overridden

    def test_an_unknown_harness_setting_is_rejected(self, project: Path) -> None:
        # Otherwise a typo in the harness block is silently ignored.
        (project / "jorm.yaml").write_text(
            BASE_CONFIG.replace("    airgap: true", "    airgapp: true")
        )
        with pytest.raises(Exception):
            self.compiled(project)


class TestTeichFormatCompatibility:
    """A real Teich prompts.jsonl must load without modification.

    Fixture mirrors the field combinations found in teich/examples/prompts.jsonl
    (verified against the real file: 196 records, 143 multi-turn, 47 with a
    system prompt, 14 with a github_repo).
    """

    @property
    def fixture(self) -> Path:
        return Path(__file__).parent.parent / "fixtures" / "teich_prompts.jsonl"

    def test_loads_unmodified(self) -> None:
        records = load_prompts(self.fixture)
        assert len(records) == 5

    def test_ids_are_derived_and_unique(self) -> None:
        ids = [r.id for r in load_prompts(self.fixture)]
        assert ids == [f"prompt-{i:04d}" for i in range(5)]

    def test_multi_turn_is_preserved_in_order(self) -> None:
        record = load_prompts(self.fixture)[1]
        assert record.user_turns == (
            "Draft a project plan for a data pipeline.",
            "Can you draw some mermaid diagrams to illustrate it?",
            "Now add a risk checklist",
        )

    def test_github_repo_becomes_a_clonable_workspace(self) -> None:
        record = load_prompts(self.fixture)[2]
        assert record.workspace.type == "git"
        assert record.workspace.git.clone_url.endswith(
            "fastapi/full-stack-fastapi-template"
        )

    def test_system_prompt_is_kept(self) -> None:
        record = load_prompts(self.fixture)[3]
        assert record.system.startswith("You are a terse frontend engineer")

    def test_literal_none_system_is_treated_as_unset(self) -> None:
        # Teich's own normalization: the string "none" means no system prompt.
        assert load_prompts(self.fixture)[4].system is None

    def test_system_reaches_droid_as_a_flag(self) -> None:
        from jormungandr.runtime.invocation import invocation_for

        record = load_prompts(self.fixture)[3]
        call = invocation_for("droid").build(record.prompt, system=record.system)
        assert "--append-system-prompt" in call.argv
        assert record.system in call.argv

    def test_opencode_has_no_system_flag_so_uses_agents_md(self) -> None:
        # `opencode run --help` lists no system-prompt option, so the file
        # convention is the only route. Silently dropping it would be worse.
        from jormungandr.runtime.invocation import invocation_for

        assert invocation_for("opencode").system_via == "agents_md"


# Real urls, not placeholders: clone_url is validated, because it reaches
# `git ls-remote` as an argument vector and a `RUN` line as shell text.
URL = "https://git.example.com/team/mono.git"
OTHER_URL = "https://git.example.com/team/other.git"


class TestGitSource:
    """The richer form: everything github_repo cannot express."""

    def test_all_four_fields(self) -> None:
        record = PromptRecord(
            prompt="x",
            git={
                "clone_url": "git@git.example.com:team/mono.git",
                "ref": "v2.1.0",
                "subdirectory": "services/api",
                "clone_as": "api",
            },
        )
        source = record.workspace.git
        assert source.clone_url == "git@git.example.com:team/mono.git"
        assert source.ref == "v2.1.0"
        assert source.subdirectory == "services/api"
        assert source.clone_as == "api"

    def test_non_github_hosts_work(self) -> None:
        record = PromptRecord(prompt="x", git={"clone_url": "https://gitlab.com/a/b.git"})
        assert record.workspace.git.clone_url == "https://gitlab.com/a/b.git"

    def test_clone_url_is_required(self) -> None:
        with pytest.raises(ValidationError):
            PromptRecord(prompt="x", git={"ref": "main"})

    def test_a_subtree_has_no_history(self) -> None:
        # Inherent to taking a subtree, not an implementation limit.
        whole = PromptRecord(prompt="x", git={"clone_url": URL}).workspace.git
        part = PromptRecord(
            prompt="x", git={"clone_url": URL, "subdirectory": "pkg"}
        ).workspace.git
        assert whole.has_history and not part.has_history

    def test_github_repo_and_git_are_exclusive(self) -> None:
        # They describe the same thing at different detail levels; accepting
        # both would mean silently picking a winner.
        with pytest.raises(ValidationError, match="only one workspace source"):
            PromptRecord(prompt="x", github_repo="a/b", git={"clone_url": URL})

    def test_git_and_an_explicit_workspace_are_exclusive(self) -> None:
        with pytest.raises(ValidationError, match="only one workspace source"):
            PromptRecord(
                prompt="x",
                git={"clone_url": URL},
                workspace={"type": "git", "git": {"clone_url": OTHER_URL}},
            )

    def test_github_repo_and_an_explicit_workspace_are_exclusive(self) -> None:
        with pytest.raises(ValidationError, match="only one workspace source"):
            PromptRecord(
                prompt="x",
                github_repo="a/b",
                workspace={"type": "git", "git": {"clone_url": OTHER_URL}},
            )

    def test_an_explicit_none_workspace_is_not_a_conflict(self) -> None:
        record = PromptRecord(prompt="x", github_repo="a/b", workspace={"type": "none"})
        assert record.workspace.type == "git"

    @pytest.mark.parametrize("bad", ["../escape", "a/../../b", "pkg/.."])
    def test_parent_traversal_is_rejected(self, bad: str) -> None:
        # A prompt file is data; it must not be able to write outside the run's
        # output directory. `..` is the only segment that can actually escape.
        with pytest.raises(ValidationError, match=r"\.\."):
            PromptRecord(prompt="x", git={"clone_url": URL, "subdirectory": bad})
        with pytest.raises(ValidationError, match=r"\.\."):
            PromptRecord(prompt="x", git={"clone_url": URL, "clone_as": bad})

    def test_surrounding_slashes_are_trimmed_not_rejected(self) -> None:
        # These are always joined below the workspace root, so a leading slash
        # is sloppiness rather than an absolute path.
        record = PromptRecord(
            prompt="x", git={"clone_url": URL, "subdirectory": "/pkg/api/"}
        )
        assert record.workspace.git.subdirectory == "pkg/api"

    def test_unknown_git_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PromptRecord(prompt="x", git={"clone_url": URL, "branch": "main"})

    @pytest.mark.parametrize(
        "good",
        [
            "https://github.com/acme/app",
            "https://github.com/acme/app.git",
            "http://git.internal/acme/app.git",
            "git://git.kernel.org/pub/scm/git/git.git",
            "ssh://git@git.example.com:2222/team/mono.git",
            "git@git.example.com:team/mono.git",
            "https://user:token@git.example.com/team/mono.git",
        ],
    )
    def test_real_git_urls_are_accepted(self, good: str) -> None:
        record = PromptRecord(prompt="x", git={"clone_url": good})
        assert record.workspace.git.clone_url == good

    @pytest.mark.parametrize(
        "bad",
        [
            # An argument beginning with a dash is an *option* to every git
            # subcommand, and `--upload-pack=<cmd>` executes <cmd>.
            "--upload-pack=touch /tmp/pwned",
            "-u/bin/sh",
            # `ext::` is a transport that runs a command by design.
            "ext::sh -c 'touch /tmp/pwned'",
            # clone_url is interpolated unquoted into a `RUN git remote add
            # origin <url>` line in the workspace Dockerfile.
            "https://h/r; touch /tmp/pwned",
            "https://h/r && touch /tmp/pwned",
            "https://h/r$(touch /tmp/pwned)",
            "https://h/r`touch /tmp/pwned`",
            # No scheme at all: not a git url, and previously accepted.
            "u",
            "/etc/passwd",
            "file:///etc",
        ],
    )
    def test_a_clone_url_that_is_not_a_git_url_is_rejected(self, bad: str) -> None:
        # A prompts.jsonl is data. Rejecting here names the offending line at
        # load time instead of handing the string to `git` inside a worker.
        with pytest.raises(ValidationError, match="clone_url"):
            PromptRecord(prompt="x", git={"clone_url": bad})


class TestRecordIdSafety:
    """Ids become directory names, so they must be one safe path component.

    `output_dir / record.id` with an absolute id discards output_dir entirely,
    and `..` escapes upward — into the rmtree that clears a record's workspace
    before each run. That is arbitrary deletion driven by a data file.
    """

    @pytest.mark.parametrize(
        "bad", ["../../escape", "/abs/path", "a/b", ".", "..", "", "   ", "-leading"]
    )
    def test_unsafe_ids_are_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            PromptRecord(prompt="x", id=bad)

    @pytest.mark.parametrize("good", ["alpha", "a-1", "a_1.v2", "A9", "0001"])
    def test_ordinary_ids_are_accepted(self, good: str) -> None:
        assert PromptRecord(prompt="x", id=good).id == good

    def test_an_unsafe_id_in_a_file_is_reported_with_its_line(self, tmp_path: Path) -> None:
        path = tmp_path / "p.jsonl"
        path.write_text('{"prompt":"ok"}\n{"id":"../../boom","prompt":"x"}\n')
        with pytest.raises(ValueError, match="line 2"):
            load_prompts(path)

    def test_derived_ids_are_always_safe(self, tmp_path: Path) -> None:
        path = tmp_path / "p.jsonl"
        path.write_text('{"prompt":"a"}\n{"prompt":"b"}\n')
        for record in load_prompts(path):
            assert PromptRecord(prompt="x", id=record.id).id == record.id


class TestReviewRegressions:
    def test_output_dir_resolves_even_when_the_block_is_omitted(
        self, tmp_path: Path
    ) -> None:
        # An omitted block still has a default path; leaving it unresolved
        # makes it relative to the process CWD, so the same config writes
        # somewhere else depending on where it was invoked.
        (tmp_path / "jorm.yaml").write_text(
            BASE_CONFIG.replace("prompts:\n  file: ./prompts.jsonl\n",
                                "prompts:\n  file: ./prompts.jsonl\n")
        )
        (tmp_path / "prompts.jsonl").write_text('{"prompt":"x"}\n')
        config = load_config(tmp_path / "jorm.yaml", apply_env=False)
        assert config.output.dir.is_absolute()
        assert config.output.dir == (tmp_path / "runs").resolve()

    def test_a_custom_user_module_reaches_the_workdir(self, project: Path) -> None:
        # Defaulting workdir to "agent" while the user module created someone
        # else produces a Dockerfile whose chown and USER name an account that
        # does not exist.
        from jormungandr.config.loading import compile_image_spec

        (project / "jorm.yaml").write_text(
            BASE_CONFIG.replace(
                "prompts:",
                "image:\n  modules:\n    - {name: user, user: runner}\nprompts:",
            )
        )
        spec = compile_image_spec(load_config(project / "jorm.yaml", apply_env=False))
        workdir = next(m for m in spec.modules if m.name == "workdir")
        assert workdir.config()["user"] == "runner"
        rendered = compose(spec).runtime.dockerfile
        assert "USER runner" in rendered

    def test_a_literal_credential_is_not_echoed_in_the_message(self) -> None:
        # Reporting a leaked key must not print the key.
        secret = "sk-or-v1-abcdefghijklmnopqrstuvwxyz0123456789"
        with pytest.raises(ValidationError) as excinfo:
            ProviderSpec(base_url="https://h/v1", api_key=secret, models={"a": "b"})
        message = str(excinfo.value)
        # pydantic echoes the input separately; our own text must not add it.
        assert "abcdefghijklmnop" not in message.split("input_value")[0]

    def test_opencode_limits_are_emitted_without_context_window(self) -> None:
        # The schema requires both keys, so a partial limit is invalid — but
        # emitting none leaves context accounting broken with no hint why.
        from jormungandr.runtime.modules.builtin import OpenCode

        providers = {
            "p": ProviderSpec(
                base_url="https://h/v1", api_key="${K}", models={"m": "up"}
            )
        }
        document = OpenCode.translate_providers(providers, "p/m")
        limit = document["provider"]["p"]["models"]["up"]["limit"]
        assert limit["context"] and limit["output"]
