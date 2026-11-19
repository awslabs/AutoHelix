"""Every relative link in docs/*.md, README.md and CHANGELOG.md resolves.

A link's path must name a file or folder that exists; a `#anchor` (on its
own, or after a markdown file's path) must name a heading of that file, by
GitHub's anchor rules (`github_anchor`). Links in fenced code blocks and in
inline code are not links. Absolute URLs (`https:`, `mailto:`) aren't checked.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md", ROOT / "CHANGELOG.md"]

FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
INLINE_CODE = re.compile(r"(`+)(?:(?!\n[ \t]*\n).)*?\1", re.DOTALL)
# [text](target) and ![alt](target); the target may have a "title".
MD_LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REF_DEF = re.compile(r"^ {0,3}\[[^\]]+\]:[ \t]*<?([^\s>]+)>?", re.MULTILINE)
HTML_ATTR = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']+)["']""")
HTML_ID = re.compile(r"""<a\s[^>]*\b(?:id|name)\s*=\s*["']([^"']+)["']""")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def github_anchor(heading: str) -> str:
    """The anchor GitHub gives a heading's text (before duplicate suffixes).

    The rendered text (markdown emphasis, code ticks and link targets gone),
    lowercased, every character but letters, digits, `_`, `-` and spaces
    dropped, spaces turned into hyphens.
    """
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # links: their text
    text = re.sub(r"<[^>]+>", "", text)  # inline HTML tags
    text = text.replace("`", "")
    text = re.sub(r"(\*+|(?<!\w)_+|_+(?!\w))", "", text)  # emphasis markers
    text = text.strip().lower()
    text = "".join(ch for ch in text if ch.isalnum() or ch in "_- ")
    return text.replace(" ", "-")


def _lines(path: Path) -> list[tuple[int, str]]:
    """The file's lines outside fenced code blocks, numbered from 1."""
    out: list[tuple[int, str]] = []
    fence: str | None = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = FENCE.match(line)
        if match:
            if fence is None:
                fence = match.group(1)
            elif match.group(1) == fence:
                fence = None
            continue
        if fence is None:
            out.append((number, line))
    return out


def anchors(path: Path) -> set[str]:
    """Every anchor GitHub gives the markdown file: headings (a repeated one
    gets `-1`, `-2`, ...) and explicit `<a id=...>` / `<a name=...>`."""
    seen: dict[str, int] = {}
    found: set[str] = set()
    for _, line in _lines(path):
        found.update(HTML_ID.findall(line))
        match = HEADING.match(line)
        if not match:
            continue
        slug = github_anchor(match.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def links(path: Path) -> list[tuple[int, str]]:
    """(line, target) for every link in the file outside code. Inline code and
    link text may wrap onto the next line, so the prose is searched whole."""
    numbered = dict(_lines(path))
    count = len(path.read_text(encoding="utf-8").splitlines())
    prose = "\n".join(numbered.get(number, "") for number in range(1, count + 1))
    # Blank inline code (a span ends at a blank line), keeping the newlines.
    prose = INLINE_CODE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), prose)
    found = sorted(
        (match.start(1), match.group(1))
        for pattern in (MD_LINK, HTML_ATTR, REF_DEF)
        for match in pattern.finditer(prose)
    )
    return [(prose.count("\n", 0, start) + 1, target) for start, target in found]


def problems(path: Path, root: Path = ROOT) -> list[str]:
    """Each relative link of the file that doesn't resolve, as `file:line: why`
    (paths relative to `root`)."""
    found: list[str] = []
    for number, target in links(path):
        if SCHEME.match(target) or target.startswith("//"):
            continue
        where = f"{path.relative_to(root)}:{number}: {target}"
        file_part, _, anchor = target.partition("#")
        file_part = unquote(file_part.split("?", 1)[0])
        linked = (path.parent / file_part).resolve() if file_part else path
        if not linked.exists():
            found.append(f"{where}: no such file")
            continue
        if not anchor:
            continue
        if linked.suffix != ".md":
            found.append(f"{where}: anchor on a file that isn't markdown")
        elif unquote(anchor) not in anchors(linked):
            found.append(f"{where}: no heading #{anchor} in {linked.relative_to(root)}")
    return found


@pytest.mark.parametrize("path", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(path: Path) -> None:
    assert problems(path) == []


def test_the_docs_have_links() -> None:
    """The checker finds the links that are there (it isn't vacuous)."""
    readme = dict(links(ROOT / "README.md"))
    assert "docs/getting-started.md" in readme.values()
    assert any("#" in target for target in readme.values())
    assert sum(len(links(path)) for path in DOCS) > 100


@pytest.mark.parametrize(
    ("heading", "anchor"),
    [
        ("Old → new", "old--new"),
        ("`autohelix stop`", "autohelix-stop"),
        ("What's NOT isolated", "whats-not-isolated"),
        ("4. Continue, set aside or start over", "4-continue-set-aside-or-start-over"),
        ("Claude Code (default)", "claude-code-default"),
        ("Contributing & feedback", "contributing--feedback"),
        ("Sorting (performance optimization)", "sorting-performance-optimization"),
        ("`AUTOHELIX_USAGE_FILE` and **cost**", "autohelix_usage_file-and-cost"),
        ("See [the config](config.md)", "see-the-config"),
    ],
)
def test_github_anchor(heading: str, anchor: str) -> None:
    assert github_anchor(heading) == anchor


def test_broken_links_are_found(tmp_path: Path) -> None:
    (tmp_path / "other.md").write_text("# Other\n\n## Same\n\n## Same\n")
    doc = tmp_path / "doc.md"
    doc.write_text(
        "# Title\n\n"
        "[ok](other.md#same-1) [ok](#title) [ok](other.md) [web](https://x.invalid/a#b)\n"
        "[gone](missing.md) [bad anchor](other.md#nope) [bad local](#none)\n"
        "`[code](missing-in-code.md)`\n"
        "```\n[fenced](missing-in-fence.md)\n# Not a heading\n```\n"
        '<img src="missing.png">\n'
        "[ref]: missing-ref.md\n"
        "`wrapped\ncode [x](missing-in-wrapped-code.md)` and [wrapped\ntext](missing-wrapped.md)\n"
    )
    assert problems(doc, root=tmp_path) == [
        "doc.md:4: missing.md: no such file",
        "doc.md:4: other.md#nope: no heading #nope in other.md",
        "doc.md:4: #none: no heading #none in doc.md",
        "doc.md:10: missing.png: no such file",
        "doc.md:11: missing-ref.md: no such file",
        "doc.md:14: missing-wrapped.md: no such file",
    ]
    assert "not-a-heading" not in anchors(doc)
