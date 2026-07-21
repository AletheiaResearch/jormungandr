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
    def test_id_is_required(self) -> None:
        with pytest.raises(ValidationError):
            PromptRecord(prompt="x")

    def test_prompt_becomes_a_user_turn(self) -> None:
        record = PromptRecord(id="a", prompt="hello")
        assert record.user_turns == ("hello",)

    def test_follow_ups_are_normalized_into_turns(self) -> None:
        record = PromptRecord(id="a", prompt="one", follow_up_prompts=["two", "three"])
        assert record.user_turns == ("one", "two", "three")

    def test_explicit_turns_support_a_system_message(self) -> None:
        record = PromptRecord(
            id="a",
            turns=[{"role": "system", "content": "be terse"}, {"role": "user", "content": "go"}],
        )
        assert record.system == "be terse"
        assert record.user_turns == ("go",)

    def test_both_spellings_at_once_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not both"):
            PromptRecord(id="a", prompt="x", turns=[{"role": "user", "content": "y"}])

    def test_turns_need_a_user_turn(self) -> None:
        with pytest.raises(ValidationError, match="at least one user turn"):
            PromptRecord(id="a", turns=[{"role": "system", "content": "only system"}])

    def test_empty_record_rejected(self) -> None:
        with pytest.raises(ValidationError, match="needs 'prompt' or 'turns'"):
            PromptRecord(id="a")

    def test_unknown_key_rejected(self) -> None:
        # Silent drops are the worst failure mode for a hand-authored format.
        with pytest.raises(ValidationError):
            PromptRecord(id="a", prompt="x", promt="typo")

    def test_git_workspace_requires_a_pinned_ref(self) -> None:
        with pytest.raises(ValidationError, match="irreproducible"):
            PromptRecord(
                id="a", prompt="x", workspace={"type": "git", "repo": "https://h/r"}
            )

    def test_local_workspace_requires_a_path(self) -> None:
        with pytest.raises(ValidationError, match="requires 'path'"):
            PromptRecord(id="a", prompt="x", workspace={"type": "local"})

    def test_overrides_are_namespaced(self) -> None:
        record = PromptRecord(id="a", prompt="x", overrides={"timeout": 60})
        assert record.overrides.timeout == 60


class TestLoadPrompts:
    def write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "p.jsonl"
        path.write_text(body)
        return path

    def test_reads_records(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, '{"id":"a","prompt":"x"}\n{"id":"b","prompt":"y"}\n')
        assert [r.id for r in load_prompts(path)] == ["a", "b"]

    def test_blank_and_comment_lines_skipped(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, '\n# a note\n{"id":"a","prompt":"x"}\n\n')
        assert len(load_prompts(path)) == 1

    def test_bom_is_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "p.jsonl"
        path.write_bytes(b'\xef\xbb\xbf{"id":"a","prompt":"x"}\n')
        assert load_prompts(path)[0].id == "a"

    def test_errors_report_the_line_number(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, '{"id":"a","prompt":"x"}\nnot json\n')
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
            '{"id":"a","prompt":"x"}\n{"id":"b","prompt":"y"}\n'
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

    def test_airgap_setting_reaches_the_image(self, project: Path) -> None:
        assert "FACTORY_AIRGAP_ENABLED=true" in self.compiled(project).runtime.dockerfile
