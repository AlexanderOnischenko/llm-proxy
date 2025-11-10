from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from llm.config import loader
from llm.config.loader import load_llm_config, pick_active


def _write(path: Path, contents: str) -> Path:
    path.write_text(textwrap.dedent(contents), encoding="utf-8")
    return path


def test_load_llm_config_resolves_env_and_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "api_key").write_text("from-secret", encoding="utf-8")

    config_path = _write(
        tmp_path / "providers.yaml",
        """
        llm:
          active: openrouter
          providers:
            openrouter:
              type: openai_compatible
              base_url: https://openrouter.ai/api/v1
              api_key: secret:api_key
              model: env:MODEL_NAME
        """,
    )

    monkeypatch.setenv("MODEL_NAME", "env-model")
    monkeypatch.setattr(loader, "SECRETS_DIR", str(secrets_dir))

    cfg = load_llm_config(str(config_path))

    provider = cfg.llm.providers["openrouter"]
    assert provider.api_key == "from-secret"
    assert provider.model == "env-model"


def test_load_llm_config_reports_missing_fields(tmp_path: Path):
    config_path = _write(
        tmp_path / "providers.yaml",
        """
        llm:
          active: openrouter
          providers:
            openrouter:
              type: openai_compatible
        """,
    )

    with pytest.raises(ValueError) as excinfo:
        load_llm_config(str(config_path))

    message = str(excinfo.value)
    assert "base_url: Field required" in message


def test_load_llm_config_rejects_missing_ca_bundle(tmp_path: Path):
    bundle_path = tmp_path / "missing.pem"
    # Intentionally do not create the file to trigger validation
    config_path = _write(
        tmp_path / "providers.yaml",
        f"""
        llm:
          active: gigachat
          providers:
            gigachat:
              type: gigachat
              url: https://gigachat.example/api/v1
              auth_url: https://auth.example/token
              scope: scope
              credentials: literal
              ca_bundle: {bundle_path}
        """,
    )

    with pytest.raises(ValueError) as excinfo:
        load_llm_config(str(config_path))

    assert "missing CA bundle" in str(excinfo.value)


def test_load_llm_config_rejects_placeholder_ca_bundle(tmp_path: Path):
    bundle_path = tmp_path / "placeholder.pem"
    bundle_path.write_text("not-a-certificate", encoding="utf-8")

    config_path = _write(
        tmp_path / "providers.yaml",
        f"""
        llm:
          active: gigachat
          providers:
            gigachat:
              type: gigachat
              url: https://gigachat.example/api/v1
              auth_url: https://auth.example/token
              scope: scope
              credentials: literal
              verify_ssl: true
              ca_bundle: {bundle_path}
        """,
    )

    with pytest.raises(ValueError) as excinfo:
        load_llm_config(str(config_path))

    assert "does not contain a PEM certificate" in str(excinfo.value)


def test_pick_active_rejects_unknown_provider(tmp_path: Path):
    config_path = _write(
        tmp_path / "providers.yaml",
        """
        llm:
          active: gigachat
          providers:
            gigachat:
              type: gigachat
              url: https://gigachat.example/api/v1
              auth_url: https://auth.example/token
              scope: scope
              credentials: literal
        """,
    )

    cfg = load_llm_config(str(config_path))

    os.environ["LLM_PROVIDER"] = "unknown"
    with pytest.raises(ValueError) as excinfo:
        pick_active(cfg)
    assert "unknown" in str(excinfo.value)
    os.environ.pop("LLM_PROVIDER", None)


def test_pick_active_prefers_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config_path = _write(
        tmp_path / "providers.yaml",
        """
        llm:
          active: gigachat
          providers:
            gigachat:
              type: gigachat
              url: https://gigachat.example/api/v1
              auth_url: https://auth.example/token
              scope: scope
              credentials: literal
            openrouter:
              type: openai_compatible
              base_url: https://openrouter.ai/api/v1
              api_key: literal
        """,
    )

    cfg = load_llm_config(str(config_path))

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    name, provider = pick_active(cfg)
    assert name == "openrouter"
    assert provider.type == "openai_compatible"
    monkeypatch.delenv("LLM_PROVIDER", raising=False)


def test_load_llm_config_normalizes_provider_keys(tmp_path: Path):
    config_path = _write(
        tmp_path / "providers.yaml",
        """
        llm:
          active: GigaChat
          providers:
            GigaChat:
              type: gigachat
              url: https://gigachat.example/api/v1
              auth_url: https://auth.example/token
              scope: scope
              credentials: literal
        """,
    )

    cfg = load_llm_config(str(config_path))
    assert "gigachat" in cfg.llm.providers
    assert cfg.llm.active == "gigachat"

    name, provider = pick_active(cfg)
    assert name == "gigachat"
    assert provider.type == "gigachat"
