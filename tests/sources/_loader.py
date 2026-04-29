"""Shared test loader for the ``scripts/sources`` modules.

The ``tests.sources`` package shadows the bare name ``sources`` when
``scripts/`` is added to ``sys.path``, which means we can't simply do
``from sources import github``. We work around this by registering a
synthetic package ``aiqs_sources`` whose ``__path__`` points at
``scripts/sources/``, then loading each module under that synthetic
namespace. Relative imports inside the source modules
(``from . import cache``) resolve against that synthetic package.

Tests import the modules they need by calling :func:`load`, which is
idempotent. Each test module re-uses the same loaded modules (loading
once per test session is the cache.py pattern, too).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PACKAGE_NAME = "aiqs_sources"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCES_DIR = _REPO_ROOT / "scripts" / "sources"

_loaded: dict = {}


def _ensure_package() -> types.ModuleType:
    pkg = sys.modules.get(PACKAGE_NAME)
    if pkg is None:
        pkg = types.ModuleType(PACKAGE_NAME)
        pkg.__path__ = [str(_SOURCES_DIR)]  # type: ignore[attr-defined]
        sys.modules[PACKAGE_NAME] = pkg
    return pkg


def load(module_name: str):
    """Load (or return cached) source module under the synthetic package.

    A module loaded transitively via ``from . import cache`` from another
    source module will already be present in ``sys.modules``; we honor that
    so all callers share a single instance (critical for in-memory state
    like the mcpmarket throttle cache).
    """
    fq = f"{PACKAGE_NAME}.{module_name}"
    if fq in _loaded:
        return _loaded[fq]
    if fq in sys.modules:
        _loaded[fq] = sys.modules[fq]
        return _loaded[fq]
    _ensure_package()
    spec = importlib.util.spec_from_file_location(
        fq, _SOURCES_DIR / f"{module_name}.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build spec for {module_name}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[fq] = mod
    spec.loader.exec_module(mod)
    _loaded[fq] = mod
    return mod
