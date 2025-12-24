from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from typing import Any, cast

from collections.abc import Generator
import pytest

from llm.config.settings import reset_settings_overrides


def _ensure_package(name: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    mod.__path__ = []
    sys.modules[name] = mod
    return mod


def _install_shared_pkg_aliases_if_missing() -> None:
    """
    Transition helper:
    if shared_pkg is not installed in this repo, expose legacy import paths
    as aliases pointing at llm equivalents.
    """
    try:
        import shared_pkg  # noqa: F401
        return
    except Exception:
        pass

    _ensure_package("shared_pkg")
    _ensure_package("shared_pkg.config")
    _ensure_package("shared_pkg.utils")

    llm_settings = importlib.import_module("llm.config.settings")

    # Logging: prefer project-local module if present; otherwise provide a tiny stub
    # that matches the legacy interface (get_logger/configure_logging).
    try:
        llm_logging = importlib.import_module("llm.utils.logging_config")
    except Exception:
        llm_logging = types.ModuleType("llm.utils.logging_config")

        import logging

        def get_logger(name: str):
            return logging.getLogger(name)

        def configure_logging(*args: Any, **kwargs: Any) -> None:
            return None

        llm_logging.get_logger = get_logger  # type: ignore[attr-defined]
        llm_logging.configure_logging = configure_logging  # type: ignore[attr-defined]

    sys.modules["shared_pkg.config.settings"] = llm_settings
    sys.modules["shared_pkg.utils.logging_config"] = llm_logging


def _install_openai_stub_if_needed() -> None:
    try:
        import openai  # noqa: F401
        import openai as _openai
        if hasattr(_openai, "AsyncOpenAI") and hasattr(_openai, "OpenAIError"):
            return
    except Exception:
        pass

    mod = types.ModuleType("openai")

    class OpenAIError(Exception):
        pass

    class _Completions:
        def __init__(self) -> None:
            self.create = None

    class _Chat:
        def __init__(self) -> None:
            self.completions = _Completions()

    class AsyncOpenAI:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.chat = _Chat()

    mod_any = cast(Any, mod)
    mod_any.AsyncOpenAI = AsyncOpenAI
    mod_any.OpenAIError = OpenAIError
    sys.modules["openai"] = mod


_install_shared_pkg_aliases_if_missing()
_install_openai_stub_if_needed()


@pytest.fixture(autouse=True)
def _reset_settings_overrides() -> Generator[None, None, None]:
    reset_settings_overrides()
    yield
    reset_settings_overrides()
