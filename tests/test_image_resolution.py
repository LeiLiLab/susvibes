"""Offline tests for the opt-in ACR registry override.

``resolve_image_name`` (ported from the Endor fork) rewrites Docker image
references to a registry given by ``ACR_REGISTRY_URL``.  Unset -> no-op
(Docker Hub, upstream behavior).  These tests need no Docker or network.
"""

from __future__ import annotations

import pytest

from susvibes.core.utils import ENV_REGISTRY_OVERRIDE, resolve_image_name
from evaluation_harness.base import DockerHarnessBase

HUB_IMAGE = "songwen6968/susvibes.x86_64.eval_foo_deadbeef:latest"
ACR = "endorsecurityresearch.azurecr.io"


def test_noop_when_unset(monkeypatch):
    monkeypatch.delenv(ENV_REGISTRY_OVERRIDE, raising=False)
    assert resolve_image_name(HUB_IMAGE) == HUB_IMAGE


def test_rewrites_hub_image_when_set(monkeypatch):
    monkeypatch.setenv(ENV_REGISTRY_OVERRIDE, ACR)
    assert resolve_image_name(HUB_IMAGE) == f"{ACR}/{HUB_IMAGE}"


def test_already_prefixed_passthrough(monkeypatch):
    monkeypatch.setenv(ENV_REGISTRY_OVERRIDE, ACR)
    already = f"{ACR}/{HUB_IMAGE}"
    assert resolve_image_name(already) == already


def test_strips_existing_registry_before_rewrite(monkeypatch):
    monkeypatch.setenv(ENV_REGISTRY_OVERRIDE, ACR)
    other = f"otherregistry.io/{HUB_IMAGE}"
    assert resolve_image_name(other) == f"{ACR}/{HUB_IMAGE}"


def test_normalizes_scheme_and_trailing_slash(monkeypatch):
    monkeypatch.setenv(ENV_REGISTRY_OVERRIDE, f"https://{ACR}/")
    assert resolve_image_name(HUB_IMAGE) == f"{ACR}/{HUB_IMAGE}"


def test_empty_env_is_noop(monkeypatch):
    monkeypatch.setenv(ENV_REGISTRY_OVERRIDE, "   ")
    assert resolve_image_name(HUB_IMAGE) == HUB_IMAGE


class _Harness(DockerHarnessBase):
    name = "foo"
    env_source_files = ["/root/.bashrc"]


def test_base_init_applies_override(monkeypatch):
    monkeypatch.setenv(ENV_REGISTRY_OVERRIDE, ACR)
    harness = _Harness(HUB_IMAGE)
    assert harness.docker_image == f"{ACR}/{HUB_IMAGE}"


def test_base_init_noop_when_unset(monkeypatch):
    monkeypatch.delenv(ENV_REGISTRY_OVERRIDE, raising=False)
    harness = _Harness(HUB_IMAGE)
    assert harness.docker_image == HUB_IMAGE
