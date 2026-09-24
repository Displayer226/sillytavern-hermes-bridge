from pathlib import Path

import pytest
from fastapi import HTTPException

from proxy_st import request_transform, workspace


def test_proxy_workspace_override_only_accepts_safe_relative_paths():
    assert request_transform.proxy_workspace_override({"st_proxy": {"workspace": "projects/demo"}}) == "projects/demo"
    assert request_transform.proxy_workspace_override({"st_proxy": {"workspace": "/etc"}}) is None
    assert request_transform.proxy_workspace_override({"st_proxy": {"workspace": "../etc"}}) is None
    assert request_transform.proxy_workspace_override({"st_proxy": {"workspace": "."}}) is None


def test_proxy_profile_override_only_accepts_safe_profile_names():
    assert request_transform.proxy_profile_override({"st_proxy": {"profile": "Local"}}) == "local"
    assert request_transform.proxy_profile_override({"st_proxy": {"profile": "admin-tools"}}) == "admin-tools"
    assert request_transform.proxy_profile_override({"st_proxy": {"profile": "../admin"}}) is None
    assert request_transform.proxy_profile_override({"st_proxy": {"profile": "bad profile"}}) is None


def test_workspace_selection_maps_validated_proxy_path_back_to_host(monkeypatch, tmp_path: Path):
    container_root = tmp_path / "container-workspaces"
    selected = container_root / "projects" / "demo"
    selected.mkdir(parents=True)
    host_root = Path("/srv/hermes-workspaces")

    monkeypatch.setattr(workspace, "WORKSPACE_EXPLORER_ENABLED", True)
    monkeypatch.setattr(workspace, "WORKSPACE_ROOT", container_root)
    monkeypatch.setattr(workspace, "WORKSPACE_HOST_ROOT", host_root)

    assert workspace.workspace_cwd_for_selection("projects/demo") == "/srv/hermes-workspaces/projects/demo"


def test_default_workspace_selection_maps_to_configured_host_root(monkeypatch, tmp_path: Path):
    container_root = tmp_path / "container-workspaces"
    container_root.mkdir()

    monkeypatch.setattr(workspace, "WORKSPACE_EXPLORER_ENABLED", True)
    monkeypatch.setattr(workspace, "WORKSPACE_ROOT", container_root)
    monkeypatch.setattr(workspace, "WORKSPACE_HOST_ROOT", Path("/"))

    assert workspace.workspace_cwd_for_selection(None) == "/"


def test_unconfigured_workspace_allows_only_no_selection(monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACE_ROOT", None)
    monkeypatch.setattr(workspace, "WORKSPACE_HOST_ROOT", None)

    assert workspace.workspace_cwd_for_selection(None) is None
    assert workspace.workspace_cwd_for_selection("") is None

    with pytest.raises(HTTPException) as exc_info:
        workspace.workspace_cwd_for_selection("projects/demo")

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Workspace selection requires WORKSPACE_ROOT"
