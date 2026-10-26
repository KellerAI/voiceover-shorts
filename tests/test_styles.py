"""Schema and existence tests for styles/styles.json."""
import copy
import json
import os
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from feedback import feedback  # noqa: E402

REGISTRY = Path(__file__).resolve().parents[1] / "styles" / "styles.json"
KEYS = list(string.ascii_uppercase[:20])  # A..T
REQUIRED = {
    "key": str, "name": str, "canonical_dir": str, "source_files": list,
    "variants": list, "content_fit": list, "owner_rank": (int, type(None)),
    "notes": list, "fonts": dict, "font_license": str, "publishable": bool,
}
D4_UNLICENSED = {"Comic Sans MS", "Marker Felt", "Chalkduster", "SF Pro", "SF Mono"}


def validate(doc: dict, root: Path | None = None) -> list[str]:
    """Return a list of validation errors; empty means valid.

    root is the resolved lookdev_root; when None the file-existence checks are skipped.
    """
    errs: list[str] = []
    styles = doc.get("styles")
    if not isinstance(styles, list):
        return ["styles is not a list"]
    keys = [s.get("key") for s in styles]
    if keys != KEYS:
        errs.append(f"keys must be exactly A-T in order, got {keys}")
    for s in styles:
        k = s.get("key")
        for f, t in REQUIRED.items():
            if f not in s:
                errs.append(f"{k}: missing field {f}")
            elif not isinstance(s[f], t):
                errs.append(f"{k}: field {f} has wrong type")
        if errs and any(e.startswith(f"{k}: missing") for e in errs):
            continue
        if root is not None:
            if not (root / s["canonical_dir"]).is_dir():
                errs.append(f"{k}: canonical_dir missing: {s['canonical_dir']}")
            for rel in s["source_files"]:
                if not (root / rel).exists():
                    errs.append(f"{k}: source file missing: {rel}")
            for v in s["variants"]:
                if not (root / v["dir"]).is_dir():
                    errs.append(f"{k}: variant dir missing: {v['dir']}")
        unl = s["fonts"].get("unlicensed", [])
        if set(unl) - D4_UNLICENSED:
            errs.append(f"{k}: fonts.unlicensed names a font outside D4 list")
        want = "uncleared" if unl else "cleared"
        if s["font_license"] != want:
            errs.append(f"{k}: font_license {s['font_license']} but fonts imply {want}")
        if s["publishable"] != (s["font_license"] == "cleared"):
            errs.append(f"{k}: publishable must be false iff font_license is uncleared")
        if not s["notes"]:
            errs.append(f"{k}: notes empty")
    return errs


@pytest.fixture(scope="module")
def doc():
    return json.loads(REGISTRY.read_text())


@pytest.fixture(scope="module")
def root(doc):
    """Resolved lookdev_root; the existence tests skip when $SHORTS_STUDIO_DIR is unset."""
    if not os.environ.get(feedback.STUDIO_ENV, "").strip():
        pytest.skip(f"{feedback.STUDIO_ENV} unset; style directories unavailable")
    return feedback.studio_path(doc, "lookdev_root")


def test_registry_schema_valid(doc):
    assert validate(doc) == []


def test_registry_files_exist(doc, root):
    assert validate(doc, root) == []


def test_registry_paths_are_studio_relative(doc):
    for field in ("lookdev_root", "feedback_file"):
        assert isinstance(doc[field], str) and not Path(doc[field]).is_absolute(), field


def test_exactly_20_keys_a_to_t(doc):
    assert [s["key"] for s in doc["styles"]] == KEYS


def test_canonical_dirs_match_spec(doc):
    want = dict(A="dir-A", B="dir-B", C="dir-C", D="dir-D", E="r2-15s-E", F="r2-15s-F",
                G="r2-15s-G", H="r2-15s-H", I="r4-S49-I", J="r4-S49-J", K="r4-S49-K",
                L="r5L-S49-L", M="r5M-S49-M", N="r5N-S49-N", O="r5O-S49-O",
                P="r7-S49-P", Q="r7-S49-Q", R="r7-S49-R", S="r7-S49-S", T="r7-S49-T")
    assert {s["key"]: s["canonical_dir"] for s in doc["styles"]} == want


def test_k16_preferred_variants(doc):
    k = next(s for s in doc["styles"] if s["key"] == "K")
    pref = {v["dir"] for v in k["variants"] if v.get("preferred")}
    assert pref == {"r6-S49-K16", "r6-S49-K16-cut15"}


def test_known_rankings(doc):
    by = {s["key"]: s for s in doc["styles"]}
    assert [by[k]["owner_rank"] for k in "DCE"] == [1, 2, 3]
    assert "symbolic" in by["D"]["content_fit"] and "ui" in by["C"]["content_fit"]
    assert "workflow" in by["B"]["content_fit"] and "press-data" in by["A"]["content_fit"]


def test_unlicensed_fonts_block_publishing(doc):
    by = {s["key"]: s for s in doc["styles"]}
    for k in "CEFGHIPR":  # use D4 fonts per inspection
        assert by[k]["font_license"] == "uncleared" and by[k]["publishable"] is False, k
    for k in "ADKLMNOQST":
        assert by[k]["font_license"] == "cleared" and by[k]["publishable"] is True, k


# --- planted negatives: the validator must reject broken registries ---

def test_negative_19_keys_fails(doc):
    bad = copy.deepcopy(doc)
    bad["styles"].pop()  # drop T
    errs = validate(bad)
    assert any("keys must be exactly A-T" in e for e in errs)


def test_negative_missing_dir_fails(doc, root):
    bad = copy.deepcopy(doc)
    bad["styles"][3]["canonical_dir"] = "no-such-dir"
    assert any("canonical_dir missing" in e for e in validate(bad, root))


def test_negative_uncleared_but_publishable_fails(doc):
    bad = copy.deepcopy(doc)
    c = next(s for s in bad["styles"] if s["key"] == "C")
    c["publishable"] = True
    assert any("publishable must be false" in e for e in validate(bad))


def test_negative_unlicensed_font_marked_cleared_fails(doc):
    bad = copy.deepcopy(doc)
    c = next(s for s in bad["styles"] if s["key"] == "C")
    c["font_license"] = "cleared"
    c["publishable"] = True
    assert any("fonts imply uncleared" in e for e in validate(bad))


def test_negative_missing_field_fails(doc):
    bad = copy.deepcopy(doc)
    del bad["styles"][0]["notes"]
    assert any("A: missing field notes" in e for e in validate(bad))
