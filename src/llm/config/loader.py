from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .schema import GigaChatCfg, ProviderCfg, Root

SECRETS_DIR = os.getenv("SECRETS_DIR", "/run/secrets")


def _resolve_value(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:], "")
    if isinstance(value, str) and value.startswith("secret:"):
        name = value[7:]
        path = os.path.join(SECRETS_DIR, name)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    return value


logger = logging.getLogger(__name__)


def _validate_ca_bundle(provider_name: str, bundle_path: str) -> None:
    path = Path(bundle_path)
    if not path.exists():
        raise ValueError(
            f"GigaChat provider '{provider_name}' references missing CA bundle: {bundle_path}"
        )

    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - filesystem specific
        raise ValueError(
            f"Unable to read CA bundle for GigaChat provider '{provider_name}': {bundle_path}"
        ) from exc

    if "BEGIN CERTIFICATE" not in contents:
        raise ValueError(
            f"CA bundle for GigaChat provider '{provider_name}' does not contain a PEM certificate: {bundle_path}"
        )


def load_llm_config(path: str) -> Root:
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    try:
        cfg = Root.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        message = f"Invalid LLM configuration: {details}"
        logger.error(message)
        raise ValueError(message) from exc

    normalized_providers: dict[str, ProviderCfg] = {}
    for name, provider in cfg.llm.providers.items():
        dumped = provider.model_dump()
        for key, value in list(dumped.items()):
            if isinstance(value, str):
                dumped[key] = _resolve_value(value)

        resolved = provider.__class__.model_validate(dumped)
        lowered = name.lower()
        if lowered in normalized_providers:
            raise ValueError(
                f"Duplicate LLM provider key detected (case-insensitive clash): '{name}'"
            )

        if isinstance(resolved, GigaChatCfg) and resolved.verify_ssl and resolved.ca_bundle:
            _validate_ca_bundle(lowered, resolved.ca_bundle)

        normalized_providers[lowered] = resolved

    active_lower = cfg.llm.active.lower()
    llm_cfg = cfg.llm.model_copy(update={"providers": normalized_providers, "active": active_lower})
    return cfg.model_copy(update={"llm": llm_cfg})


def pick_active(cfg: Root) -> tuple[str, ProviderCfg]:
    active = os.getenv("LLM_PROVIDER", cfg.llm.active).lower()
    if active not in cfg.llm.providers:
        raise ValueError(f"Unknown LLM provider '{active}'")
    return active, cfg.llm.providers[active]
