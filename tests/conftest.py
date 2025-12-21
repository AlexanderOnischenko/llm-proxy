"""Test shims for optional dependencies.

The production repo uses `shared_pkg.*` and a package layout under `llm.*`.
This patch runner sandbox may not include those packages.

We install minimal stubs/aliases only when the real modules are missing.
In the real repo, these shims should be no-ops.
"""

from __future__ import annotations

import importlib.util
import logging
import pathlib
import sys
import types
from typing import Any, cast


def _ensure_package(name: str) -> types.ModuleType:
    """Ensure a module exists and is treated as a package."""
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    mod.__path__ = []  # mark as package
    sys.modules[name] = mod
    return mod


def _install_shared_pkg_stubs() -> None:
    try:
        import shared_pkg  # noqa: F401
        return
    except Exception:
        pass

    _ensure_package("shared_pkg")
    _ensure_package("shared_pkg.config")
    _ensure_package("shared_pkg.utils")

    logging_config = types.ModuleType("shared_pkg.utils.logging_config")

    def get_logger(name: str) -> logging.Logger:
        return logging.getLogger(name)

    def configure_logging() -> None:
        # Minimal default; real project config should override.
        logging.basicConfig(level=logging.INFO)

    # `types.ModuleType` is intentionally dynamic; help static checkers.
    logging_config_any = cast(Any, logging_config)
    logging_config_any.get_logger = get_logger
    logging_config_any.configure_logging = configure_logging
    sys.modules["shared_pkg.utils.logging_config"] = logging_config

    settings_mod = types.ModuleType("shared_pkg.config.settings")

    class Settings:  # pragma: no cover
        debug: bool = False

        llm_api_key: str = "test"
        llm_api_base: str = "http://test"
        llm_timeout: float = 30.0

        llm_model_name: str = "gpt-test"
        llm_temperature: float = 0.0
        llm_top_p: float = 1.0

        llm_cache_path: str = ":memory:"
        llm_cache_ttl: int = 0
        llm_cache_enabled: bool = True

        llm_verbose: bool = False
        llm_max_tokens: int = 16
        llm_max_concurrent: int = 1
        llm_priority_total_slots: int = 1

        llm_priority_limits_map: dict = {}
        llm_queue_max_len: int = 1000
        llm_priority_default: int = 0

    def get_settings() -> Settings:  # pragma: no cover
        return Settings()

    settings_mod_any = cast(Any, settings_mod)
    settings_mod_any.Settings = Settings
    settings_mod_any.get_settings = get_settings
    sys.modules["shared_pkg.config.settings"] = settings_mod


def _load_module_as(mod_name: str, file_path: pathlib.Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to create spec for {mod_name} from {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _install_llm_aliases_if_needed() -> None:
    # If llm.services.* is already importable, do nothing.
    try:
        existing = importlib.util.find_spec("llm.services.cache")
    except ModuleNotFoundError:
        existing = None
    if existing is not None:
        return

    root = pathlib.Path(__file__).resolve().parents[1]

    _ensure_package("llm")
    _ensure_package("llm.services")
    _ensure_package("llm.models")

    flat_list = [
        ("llm.models.chat", root / "chat.py"),
        ("llm.services.cache", root / "cache.py"),
        ("llm.services.chat_provider", root / "chat_provider.py"),
    ]

    for name, path in flat_list:
        if name in sys.modules:
            continue
        if path.exists():
            _load_module_as(name, path)

    # Minimal stubs so that importing `deps` works in minimal environments.
    if "llm.services.client" not in sys.modules:
        client_stub = types.ModuleType("llm.services.client")

        class CachedQueuedChatOpenAI:  # pragma: no cover
            pass

        def create_custom_llm(*args, **kwargs):  # pragma: no cover
            raise RuntimeError("create_custom_llm stub: not available in this environment")

        client_stub_any = cast(Any, client_stub)
        client_stub_any.CachedQueuedChatOpenAI = CachedQueuedChatOpenAI
        client_stub_any.create_custom_llm = create_custom_llm
        sys.modules["llm.services.client"] = client_stub

    if "llm.services.priority_queue" not in sys.modules:
        pq_stub = types.ModuleType("llm.services.priority_queue")

        class PriorityChatQueue:  # pragma: no cover
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        pq_stub_any = cast(Any, pq_stub)
        pq_stub_any.PriorityChatQueue = PriorityChatQueue
        sys.modules["llm.services.priority_queue"] = pq_stub


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
        def __init__(self):
            self.create = None

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class AsyncOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = _Chat()

    mod_any = cast(Any, mod)
    mod_any.AsyncOpenAI = AsyncOpenAI
    mod_any.OpenAIError = OpenAIError
    sys.modules["openai"] = mod


_install_shared_pkg_stubs()
_install_openai_stub_if_needed()
_install_llm_aliases_if_needed()