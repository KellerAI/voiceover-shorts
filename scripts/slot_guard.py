#!/usr/bin/env python3
"""slot_guard.py - is a render slot held for this session? (Shorts Studio kit, read-only helper).

CONTRACT
  Usage:
    python3 slot_guard.py [--holder NAME]

  What it does:
    Reads the render-slot file named by SHORTS_SLOT_PATH (READ ONLY, never created or written)
    and says whether a slot is held for the caller. A render is allowed only while a slot is held.
    The shorts-studio plugin test suite calls slot_held() to skip real-render tests otherwise.

  Slot file schema:
    {"holder": str|null, "kind": str|null, "granted_at": ISO-Z|null, "est_min": num|null,
     "expires_at": ISO-Z|null, "queue": [...]}

  Held means ALL of:
    - SHORTS_SLOT_PATH is set and non-empty (or an explicit path= is passed); there is no default
    - the file reads as valid JSON and the top level is an object
    - "holder" is a non-empty string
    - the caller's holder name (--holder / holder=, else env SHORTS_RENDER_SLOT) equals it exactly
    - "expires_at" is null or strictly later than now (UTC)
  The caller's holder name is mandatory: with neither --holder nor SHORTS_RENDER_SLOT set
  (an empty value counts as unset) the answer is "not held", so a slot granted to an unrelated
  session never authorizes a run. Naive timestamps are read as UTC.

  Environment:
    SHORTS_RENDER_SLOT  holder name used when --holder / holder= is not given
    SHORTS_SLOT_PATH    slot file path, read at call time. Unset or empty: not held (fail closed)

  Outputs: one stdout line, the reason. No files.

  Exit codes:
    0  slot held for the holder
    1  not held (unset holder, unset path, missing file, bad JSON, bad timestamp, empty,
       mismatched, expired)
    2  bad command-line usage (argparse)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HOLDER_ENV = "SHORTS_RENDER_SLOT"
PATH_ENV = "SHORTS_SLOT_PATH"


def slot_path() -> Path | None:
    """SHORTS_SLOT_PATH if set and non-empty, else None (no default location)."""
    value = os.environ.get(PATH_ENV)
    return Path(value) if value else None


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _parse_iso(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    return _as_utc(datetime.fromisoformat(text))


def slot_held(holder: str | None = None, path: Path | str | None = None,
              now: datetime | None = None) -> tuple[bool, str]:
    """(True, reason) only if a live slot is held by exactly `holder`; else (False, reason). Never raises."""
    want = holder if holder is not None else os.environ.get(HOLDER_ENV)
    if not want:
        return False, f"{HOLDER_ENV} not set"

    file = Path(path) if path is not None else slot_path()
    if file is None:
        return False, f"{PATH_ENV} not set"
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, f"slot file missing: {file}"
    except (OSError, ValueError, TypeError) as exc:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return False, f"slot file unreadable or not JSON: {file}: {exc}"
    if not isinstance(data, dict):
        return False, f"slot file is not a JSON object: {file}"

    have = data.get("holder")
    if not isinstance(have, str) or not have:
        return False, "no slot held (holder is null or empty)"
    if have != want:
        return False, f"slot held by {have!r}, not {want!r}"

    expires = data.get("expires_at")
    if expires is not None:
        if not isinstance(expires, str):
            return False, f"bad expires_at (not a string): {expires!r}"
        try:
            expires_dt = _parse_iso(expires)
        except ValueError:
            return False, f"bad expires_at timestamp: {expires!r}"
        current = _as_utc(now) if now is not None else datetime.now(timezone.utc)
        if expires_dt <= current:
            return False, f"slot held by {have!r} expired at {expires}"
        return True, f"slot held by {have!r} until {expires}"
    return True, f"slot held by {have!r} (no expiry)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Exit 0 if a render slot is held for the holder, else 1.")
    ap.add_argument("--holder", help=f"holder name to match (default: env {HOLDER_ENV})")
    args = ap.parse_args(argv)
    held, reason = slot_held(holder=args.holder)
    print(reason)
    return 0 if held else 1


if __name__ == "__main__":
    sys.exit(main())
