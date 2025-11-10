from __future__ import annotations

import sys
import textwrap
import types
from pathlib import Path
from typing import Any

import pytest

# Provide lightweight stand-ins for optional provider packages used in production.
_fake_gigachat = types.SimpleNamespace(GigaChat=type("GigaChat", (), {}))
sys.modules.setdefault("langchain_gigachat", _fake_gigachat)
sys.modules.setdefault(
    "langchain_google_genai",
    types.SimpleNamespace(ChatGoogleGenerativeAI=type("ChatGoogleGenerativeAI", (), {})),
)

from llm import factory
from llm.config import loader
from llm.factory import create_llm_from_config


class _DummyLLM:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _write_config(path: Path, contents: str) -> Path:
    path.write_text(textwrap.dedent(contents), encoding="utf-8")
    return path


def test_create_llm_from_config_uses_gigachat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "gigachat_auth_key").write_text("super-secret", encoding="utf-8")

    bundle_path = tmp_path / "mincifry_bundle.pem"
    bundle_path.write_text(
        """-----BEGIN CERTIFICATE-----
MIIBszCCAVmgAwIBAgIUQplaceholderexamplecertificateroot
-----END CERTIFICATE-----
""",
        encoding="utf-8",
    )

    config_path = _write_config(
        tmp_path / "llm.yaml",
        f"""
        llm:
          active: gigachat
          defaults:
            temperature: 0.7
            max_tokens: 4096
            timeout: 120
            cache:
              enabled: true
              path: /tmp/cache/llm.sqlite
              ttl: 10
              max_concurrent: 2
          providers:
            gigachat:
              type: gigachat
              url: https://gigachat.example/api/v1
              auth_url: https://auth.example/token
              scope: env:GIGACHAT_SCOPE
              credentials: secret:gigachat_auth_key
              model: custom-model
              verify_ssl: true
              ca_bundle: {bundle_path}
        """,
    )

    monkeypatch.setenv("GIGACHAT_SCOPE", "custom-scope")
    monkeypatch.setattr(loader, "SECRETS_DIR", str(secrets_dir))

    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> _DummyLLM:
        captured.update(kwargs)
        return _DummyLLM(**kwargs)

    monkeypatch.setattr(factory, "CachedQueuedGigaChat", _capture)

    llm = create_llm_from_config(config_path=str(config_path))

    assert isinstance(llm, _DummyLLM)
    assert captured["temperature"] == 0.7
    assert captured["max_tokens"] == 4096
    assert captured["timeout"] == 120
    assert captured["cache_enabled"] is True
    assert captured["cache_path"] == "/tmp/cache/llm.sqlite"
    assert captured["cache_ttl"] == 10
    assert captured["max_concurrent"] == 2
    assert captured["base_url"] == "https://gigachat.example/api/v1"
    assert captured["auth_url"] == "https://auth.example/token"
    assert captured["scope"] == "custom-scope"
    assert captured["credentials"] == "super-secret"
    assert captured["verify_ssl_certs"] is True
    assert captured["ca_bundle_file"] == str(bundle_path)
    assert captured["model"] == "custom-model"


def test_create_llm_from_config_honors_active_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config_path = _write_config(
        tmp_path / "providers.yaml",
        """
        llm:
          active: gigachat
          defaults:
            temperature: 0.2
            max_tokens: 2048
            timeout: 60
          providers:
            gigachat:
              type: gigachat
              url: https://gigachat.example/api/v1
              auth_url: https://auth.example/token
              scope: scope
              credentials: literal-secret
            openrouter:
              type: openai_compatible
              base_url: https://openrouter.ai/api/v1
              api_key: env:OPENROUTER_API_KEY
              model: llama-3
        """,
    )

    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")

    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> _DummyLLM:
        captured.update(kwargs)
        return _DummyLLM(**kwargs)

    monkeypatch.setattr(factory, "CachedQueuedChatOpenAI", _capture)

    llm = create_llm_from_config(config_path=str(config_path))

    assert isinstance(llm, _DummyLLM)
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
    assert captured["api_key"] == "router-key"
    assert captured["model"] == "llama-3"
    assert captured["temperature"] == 0.2
    assert captured["timeout"] == 60
    assert captured["cache_enabled"] is True
    assert captured["verbose"] is False

    monkeypatch.delenv("LLM_PROVIDER", raising=False)


def test_create_llm_from_config_does_not_pass_ca_bundle_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "gigachat_auth_key").write_text("secret", encoding="utf-8")

    config_path = _write_config(
        tmp_path / "llm.yaml",
        """
        llm:
          active: gigachat
          providers:
            gigachat:
              type: gigachat
              url: https://gigachat.example/api/v1
              auth_url: https://auth.example/token
              scope: scope
              credentials: secret:gigachat_auth_key
              verify_ssl: false
              ca_bundle: /etc/ssl/certs/mincifry_bundle.pem
        """,
    )

    monkeypatch.setattr(factory, "CachedQueuedGigaChat", _DummyLLM)
    monkeypatch.setattr(loader, "SECRETS_DIR", str(secrets_dir))

    llm = create_llm_from_config(config_path=str(config_path))

    assert isinstance(llm, _DummyLLM)
    assert "ca_bundle_file" not in llm.kwargs
    assert llm.kwargs["verify_ssl_certs"] is False


def test_create_llm_from_config_openai_has_no_ssl_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    config_path = _write_config(
        tmp_path / "providers.yaml",
        """
        llm:
          active: openrouter
          providers:
            openrouter:
              type: openai_compatible
              base_url: https://openrouter.ai/api/v1
              api_key: literal-key
        """,
    )

    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> _DummyLLM:
        captured.update(kwargs)
        return _DummyLLM(**kwargs)

    monkeypatch.setattr(factory, "CachedQueuedChatOpenAI", _capture)

    llm = create_llm_from_config(config_path=str(config_path))

    assert isinstance(llm, _DummyLLM)
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
    assert "ca_bundle_file" not in captured
    assert "verify_ssl_certs" not in captured
    assert captured["verbose"] is False


def test_create_llm_from_config_google_has_no_ssl_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    config_path = _write_config(
        tmp_path / "providers.yaml",
        """
        llm:
          active: google
          providers:
            google:
              type: google_gemini
              api_key: literal
              model: gemini-1.5-pro
        """,
    )

    captured: dict[str, Any] = {}

    class _FakeGoogle:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        sys.modules["langchain_google_genai"],
        "ChatGoogleGenerativeAI",
        _FakeGoogle,
    )

    llm = create_llm_from_config(config_path=str(config_path))

    assert isinstance(llm, _FakeGoogle)
    assert "ca_bundle_file" not in captured
    assert "verify_ssl_certs" not in captured


def test_create_llm_from_config_prefers_env_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config_path = _write_config(
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

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")

    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> _DummyLLM:
        captured.update(kwargs)
        return _DummyLLM(**kwargs)

    monkeypatch.setattr(factory, "CachedQueuedChatOpenAI", _capture)

    llm = create_llm_from_config(config_path=str(config_path))

    assert isinstance(llm, _DummyLLM)
    assert captured["base_url"] == "https://openrouter.ai/api/v1"

    monkeypatch.delenv("LLM_PROVIDER", raising=False)


def test_create_llm_from_config_falls_back_to_repo_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    fallback_config = _write_config(
        tmp_path / "fallback.yaml",
        """
        llm:
          active: openrouter
          providers:
            openrouter:
              type: openai_compatible
              base_url: https://openrouter.ai/api/v1
              api_key: literal
        """,
    )

    monkeypatch.setattr(factory, "DEFAULT_CONFIG_PATH", tmp_path / "missing.yaml")
    monkeypatch.setattr(factory, "REPO_CONFIG_PATH", fallback_config)
    monkeypatch.delenv("LLM_PROVIDERS_CONFIG", raising=False)

    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> _DummyLLM:
        captured.update(kwargs)
        return _DummyLLM(**kwargs)

    monkeypatch.setattr(factory, "CachedQueuedChatOpenAI", _capture)

    llm = create_llm_from_config()

    assert isinstance(llm, _DummyLLM)
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
