"""Affiliate-link placement for the voiceover-shorts-tts add-on (blueprint W10, bead kss-aad).

Rules, from the add-on blueprint and interface contract:
- Every occurrence of the affiliate link is followed on the next line by the exact disclosure
  (interface "Affiliate and sign-up text (exact)").
- The link goes only in the README, the add-on docs and skill reply text (blueprint "Decisions already
  made"), so it never appears in store-listing metadata: any file under `.claude-plugin/`
  (plugin.json, marketplace.json) or a SKILL.md frontmatter block (name/description/when_to_use).

String matching only. Nothing here opens, fetches or requests the link.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THIS_FILE = Path(__file__).resolve()

AFFILIATE_URL = "https://try.elevenlabs.io/vbrajywhn5js"
DISCLOSURE = "Paid link: KellerAI is an independent affiliate of ElevenLabs and may earn a commission."
LISTING_DIR = ".claude-plugin"
# Matches the link by host+path, with or without http(s):// and www., case-insensitive host.
# Regex-escaped copies (`try\.elevenlabs\.io`) in eval graders are not literal links and do not match.
AFFILIATE_RE = re.compile(
    r"(?<![\w.\\/-])(?i:(?:https?://)?(?:www\.)?try\.elevenlabs\.io/vbrajywhn5js)(?![\w-])"
)
# A link must end cleanly: end of line, whitespace, or one of these. Anything else (notably `\`)
# can be swallowed into the href by a linkify renderer.
CLEAN_LINK_END = frozenset(">)`\"'")


def frontmatter_line_count(lines: list[str]) -> int:
    """Number of leading lines (fences included) that form a `---` YAML frontmatter block; 0 if none."""
    if not lines or lines[0].strip() != "---":
        return 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i + 1
    return 0


def check_text(rel: str, text: str) -> list[str]:
    """Return `rel:line: reason` strings for every placement violation in one file's text."""
    violations: list[str] = []
    lines = text.splitlines()
    rel_parts = Path(rel).parts
    in_listing_dir = LISTING_DIR in rel_parts
    fm_end = frontmatter_line_count(lines) if Path(rel).name == "SKILL.md" else 0
    for i, line in enumerate(lines):
        matches = list(AFFILIATE_RE.finditer(line))
        if not matches:
            continue
        lineno = i + 1
        for m in matches:
            after = line[m.end() : m.end() + 1]
            if after and not after.isspace() and after not in CLEAN_LINK_END:
                violations.append(
                    f"{rel}:{lineno}: affiliate link is glued to {after!r}; wrap it as <URL>"
                )
        if in_listing_dir:
            violations.append(
                f"{rel}:{lineno}: affiliate link in store-listing metadata ({LISTING_DIR}/)"
            )
        if i < fm_end:
            violations.append(
                f"{rel}:{lineno}: affiliate link in SKILL.md frontmatter (listing field)"
            )
        # Leading/trailing whitespace is ignored so a markdown list continuation ("  Paid link: ...")
        # counts as the next line; any other text on that line is a violation.
        next_line = lines[i + 1].strip() if i + 1 < len(lines) else None
        if next_line != DISCLOSURE:
            violations.append(
                f"{rel}:{lineno}: affiliate link not followed on the next line by the disclosure"
            )
    return violations


def find_violations(paths: Iterable[Path], root: Path) -> list[str]:
    """Check every UTF-8 text file in `paths`; binary or undecodable files are skipped."""
    violations: list[str] = []
    for path in paths:
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            continue
        if b"\x00" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        violations.extend(check_text(path.relative_to(root).as_posix(), text))
    return violations


def shipped_files(root: Path) -> list[Path]:
    """Tracked files plus untracked, non-ignored files, so new files are checked before they are committed."""
    out = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    paths = [root / p for p in out.decode("utf-8").split("\0") if p]
    # This test file holds the URL as a constant, not as user-facing text; it is excluded on purpose.
    return [p for p in paths if p.resolve() != THIS_FILE]


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- real add-on files -------------------------------------------------------------------------


def test_shipped_files_place_affiliate_link_correctly() -> None:
    files = shipped_files(ROOT)
    assert any(p.name == "README.md" for p in files), (
        "file set unexpectedly missing README.md"
    )
    assert find_violations(files, ROOT) == []


def test_affiliate_link_is_present_where_expected() -> None:
    # Guards against a zero-run green: the placement check must actually see the link somewhere.
    texts = {
        p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8")
        for p in shipped_files(ROOT)
        if p.suffix == ".md"
    }
    assert AFFILIATE_URL in texts["README.md"]
    assert AFFILIATE_URL in texts["skills/tts-setup/SKILL.md"]


# --- planted cases -----------------------------------------------------------------------------


def test_planted_link_without_disclosure_is_flagged(tmp_path: Path) -> None:
    f = write(tmp_path, "docs/a.md", f"Sign up:\n{AFFILIATE_URL}\nThanks.\n")
    assert find_violations([f], tmp_path) == [
        "docs/a.md:2: affiliate link not followed on the next line by the disclosure"
    ]


def test_planted_schemeless_link_without_disclosure_is_flagged(tmp_path: Path) -> None:
    f = write(tmp_path, "docs/e.md", "Sign up: try.elevenlabs.io/vbrajywhn5js\nThanks.\n")
    assert find_violations([f], tmp_path) == [
        "docs/e.md:1: affiliate link not followed on the next line by the disclosure"
    ]


def test_planted_http_link_without_disclosure_is_flagged(tmp_path: Path) -> None:
    f = write(tmp_path, "docs/f.md", "Sign up: http://WWW.Try.ElevenLabs.io/vbrajywhn5js\nOk.\n")
    assert find_violations([f], tmp_path) == [
        "docs/f.md:1: affiliate link not followed on the next line by the disclosure"
    ]


def test_regex_escaped_grader_pattern_is_not_a_link(tmp_path: Path) -> None:
    f = write(tmp_path, "evals/g.py", 'PATTERN = r"try\\.elevenlabs\\.io/vbrajywhn5js"\n')
    assert find_violations([f], tmp_path) == []


def test_planted_link_on_last_line_is_flagged(tmp_path: Path) -> None:
    f = write(tmp_path, "docs/b.md", f"Sign up: {AFFILIATE_URL}")
    assert find_violations([f], tmp_path) == [
        "docs/b.md:1: affiliate link not followed on the next line by the disclosure"
    ]


def test_planted_disclosure_on_non_adjacent_line_is_flagged(tmp_path: Path) -> None:
    f = write(tmp_path, "docs/c.md", f"{AFFILIATE_URL}\n\n{DISCLOSURE}\n")
    assert find_violations([f], tmp_path) == [
        "docs/c.md:1: affiliate link not followed on the next line by the disclosure"
    ]


def test_planted_altered_disclosure_is_flagged(tmp_path: Path) -> None:
    f = write(tmp_path, "docs/d.md", f"{AFFILIATE_URL}\n{DISCLOSURE} Thanks!\n")
    assert len(find_violations([f], tmp_path)) == 1


def test_planted_link_in_plugin_manifest_is_flagged(tmp_path: Path) -> None:
    f = write(
        tmp_path, ".claude-plugin/plugin.json", f'{{"homepage": "{AFFILIATE_URL}"}}\n'
    )
    violations = find_violations([f], tmp_path)
    assert (
        ".claude-plugin/plugin.json:1: affiliate link in store-listing metadata (.claude-plugin/)"
        in violations
    )


def test_planted_link_in_marketplace_listing_is_flagged_even_with_disclosure(
    tmp_path: Path,
) -> None:
    f = write(
        tmp_path, ".claude-plugin/marketplace.json", f"{AFFILIATE_URL}\n{DISCLOSURE}\n"
    )
    assert find_violations([f], tmp_path) == [
        ".claude-plugin/marketplace.json:1: affiliate link in store-listing metadata (.claude-plugin/)"
    ]


def test_planted_link_in_skill_frontmatter_is_flagged(tmp_path: Path) -> None:
    f = write(
        tmp_path,
        "skills/x/SKILL.md",
        f"---\nname: x\ndescription: sign up at {AFFILIATE_URL}\n{DISCLOSURE}\n---\n\n# x\n",
    )
    assert find_violations([f], tmp_path) == [
        "skills/x/SKILL.md:3: affiliate link in SKILL.md frontmatter (listing field)"
    ]


def test_planted_link_glued_to_backslash_is_flagged(tmp_path: Path) -> None:
    f = write(tmp_path, "README.md", f"- Affiliate link: {AFFILIATE_URL}\\\n  {DISCLOSURE}\n")
    assert find_violations([f], tmp_path) == [
        "README.md:1: affiliate link is glued to '\\\\'; wrap it as <URL>"
    ]


def test_planted_uppercase_path_link_is_matched(tmp_path: Path) -> None:
    f = write(tmp_path, "docs/h.md", "Sign up: try.elevenlabs.io/VBRAJYWHN5JS\nOk.\n")
    assert find_violations([f], tmp_path) == [
        "docs/h.md:1: affiliate link not followed on the next line by the disclosure"
    ]


def test_planted_autolink_with_trailing_backslash_passes(tmp_path: Path) -> None:
    f = write(tmp_path, "README.md", f"- Affiliate link: <{AFFILIATE_URL}>\\\n  {DISCLOSURE}\n")
    assert find_violations([f], tmp_path) == []


def test_planted_correct_placement_passes(tmp_path: Path) -> None:
    readme = write(
        tmp_path, "README.md", f"- Affiliate link: {AFFILIATE_URL}\n  {DISCLOSURE}\n"
    )
    skill = write(
        tmp_path,
        "skills/x/SKILL.md",
        f"---\nname: x\n---\n\n```text\n{AFFILIATE_URL}\n{DISCLOSURE}\n```\n",
    )
    assert find_violations([readme, skill], tmp_path) == []
