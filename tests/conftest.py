"""Suite-wide gates: real renders need a held render slot; studio tests need SHORTS_STUDIO_DIR.

Render gate: the suite must not launch hyperframes or build.py without a held render slot. The
check is scripts/slot_guard.py (vendored in this plugin). It fails closed: if the guard cannot be
loaded, or SHORTS_SLOT_PATH / SHORTS_RENDER_SLOT are unset, every real_render test skips.

Studio gate: some tests read external studio data (lookdev inputs, canonical look dirs, the v1
workflow) from the directory named by SHORTS_STUDIO_DIR. A test opts in with @pytest.mark.studio
(optionally passing paths relative to SHORTS_STUDIO_DIR). Real-render tests in a module that
defines STUDIO_REQUIRED are studio tests too. When SHORTS_STUDIO_DIR is unset, or a required path
(the module's STUDIO_REQUIRED plus the marker's paths) is missing, the test skips with the reason.
"""
from __future__ import annotations

import importlib.util
import inspect
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SLOT_GUARD = ROOT / "scripts" / "slot_guard.py"
STUDIO_ENV = "SHORTS_STUDIO_DIR"
RENDER_FIXTURES = {"built", "rendered", "render_hold2", "render_hold08"}
RENDER_SOURCE_TOKENS = ("BUILD_PY",)


def _slot_state() -> tuple[bool, str]:
    try:
        spec = importlib.util.spec_from_file_location("shorts_slot_guard", SLOT_GUARD)
        if spec is None or spec.loader is None:
            return False, f"cannot load {SLOT_GUARD}"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        held, reason = mod.slot_held()
    except Exception as exc:  # fail closed on any import or call error
        return False, f"slot_guard unavailable ({SLOT_GUARD}): {exc!r}"
    return bool(held), str(reason)


def _is_render_item(item: pytest.Item) -> bool:
    if RENDER_FIXTURES & set(getattr(item, "fixturenames", ())):
        return True
    func = getattr(item, "function", None)
    if func is None:
        return False
    try:
        src = inspect.getsource(func)
    except (OSError, TypeError):
        return False
    return any(tok in src for tok in RENDER_SOURCE_TOKENS)


def _studio_skip_reason(item: pytest.Item) -> str | None:
    """None if the item may run; else why it skips. Only called for studio items."""
    value = os.environ.get(STUDIO_ENV)
    if not value:
        return f"{STUDIO_ENV} not set: this test reads external studio data"
    studio = Path(value)
    paths = [Path(p) for p in getattr(item.module, "STUDIO_REQUIRED", ()) if p is not None]
    marker = item.get_closest_marker("studio")
    if marker is not None:
        paths += [studio / rel for rel in marker.args]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        return f"studio data missing under {STUDIO_ENV}: {', '.join(missing)}"
    return None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "real_render: launches build.py / hyperframes; skipped unless a render slot is held"
    )
    config.addinivalue_line(
        "markers", f"studio(*relpaths): reads data under ${STUDIO_ENV}; skipped when it is unset or a path is missing"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if _is_render_item(item):
            item.add_marker(pytest.mark.real_render)
    # Slot gate first, so a render item's reported skip reason is the slot when both gates apply.
    render_items = [i for i in items if i.get_closest_marker("real_render")]
    if render_items:
        held, reason = _slot_state()
        if not held:
            skip = pytest.mark.skip(reason=f"real_render skipped, no render slot held: {reason}")
            for item in render_items:
                item.add_marker(skip)
    for item in items:
        module = getattr(item, "module", None)
        is_studio = item.get_closest_marker("studio") is not None or (
            item.get_closest_marker("real_render") is not None and hasattr(module, "STUDIO_REQUIRED"))
        if is_studio:
            reason = _studio_skip_reason(item)
            if reason is not None:
                item.add_marker(pytest.mark.skip(reason=reason))
