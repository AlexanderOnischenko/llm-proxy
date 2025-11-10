"""Expose implementation package located under :mod:`llm.src.llm`."""

from pathlib import Path
import pkgutil

__path__ = pkgutil.extend_path(__path__, __name__)  # type: ignore[name-defined]

_impl_path = Path(__file__).resolve().parent / "src" / "llm"
if _impl_path.exists():
    __path__.append(str(_impl_path))  # type: ignore[name-defined]

__all__: list[str] = []
