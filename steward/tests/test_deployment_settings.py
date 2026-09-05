"""Deployment settings resolve explicitly, independently of the import environment."""

import pytest

from conftest import REPO_ROOT, ResidentWriter, valid_manifest
from steward.deploy import target_for
from steward.deployment_rules import DeploymentSettings, DeploymentSettingsError
from steward.manifest import validate_paths
from steward.manifest_models import ResidentManifest


def test_deploy_target_uses_current_environment_and_manifest_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = ResidentManifest.model_validate(valid_manifest())
    monkeypatch.setenv("STEWARD_DEPLOY_HOST", "first.example")
    monkeypatch.setenv("STEWARD_DEPLOY_USER", "alice")
    first = target_for(manifest)
    assert (first.host, first.user) == ("first.example", "alice")
    monkeypatch.setenv("STEWARD_DEPLOY_HOST", "second.example")
    monkeypatch.setenv("STEWARD_DEPLOY_USER", "bob")
    second = target_for(manifest)
    assert (second.host, second.user) == ("second.example", "bob")
    explicit = manifest.model_copy(
        update={
            "deploy": manifest.deploy.model_copy(
                update={"host": "explicit.example", "user": "carol"}
            )
        }
    )
    assert (target_for(explicit).host, target_for(explicit).user) == ("explicit.example", "carol")


@pytest.mark.parametrize("host", [None, "", " ", "-unsafe", "host;command"])
def test_missing_or_unsafe_defaults_refuse_before_targeting(
    host: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.delenv("STEWARD_DEPLOY_HOST", raising=False)
    if host is not None:
        monkeypatch.setenv("STEWARD_DEPLOY_HOST", host)
    manifest = ResidentManifest.model_validate(valid_manifest())
    with pytest.raises(DeploymentSettingsError, match="STEWARD_DEPLOY_HOST"):
        target_for(manifest)


def test_missing_settings_are_a_validation_diagnostic(
    monkeypatch: pytest.MonkeyPatch, write_resident: ResidentWriter
) -> None:

    monkeypatch.delenv("STEWARD_DEPLOY_HOST")
    result = validate_paths([write_resident()])
    assert not result.ok
    assert any("STEWARD_DEPLOY_HOST" in d.problem for d in result.diagnostics)


def test_injected_installations_do_not_leak_into_each_other() -> None:

    manifest = ResidentManifest.model_validate(valid_manifest())
    first = DeploymentSettings.from_env(
        {"STEWARD_DEPLOY_HOST": "one", "STEWARD_DEPLOY_USER": "alice"}
    )
    second = DeploymentSettings.from_env(
        {"STEWARD_DEPLOY_HOST": "two", "STEWARD_DEPLOY_USER": "bob"}
    )
    assert target_for(manifest, first).host == "one"
    assert target_for(manifest, second).user == "bob"
    assert target_for(manifest, first).user == "alice"


def test_shipped_targets_do_not_need_personal_library_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    monkeypatch.delenv("STEWARD_DEPLOY_HOST")
    monkeypatch.delenv("STEWARD_DEPLOY_USER")
    result = validate_paths([REPO_ROOT / "residents"])
    assert result.ok
    assert {r.id for r in result.residents} == {"hob", "pip"}
    for resident in result.residents:
        target = target_for(resident.manifest)
        assert (target.host, target.user) == ("dxp2800", "Miha")
