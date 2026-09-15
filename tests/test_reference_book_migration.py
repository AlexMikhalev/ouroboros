"""The chapter split moved bytes, not meaning: a reversible byte proof.

Each chapter file is exactly a two-line prologue plus the old `##` section body:

    # <the old section title>
    <blank>
    <one authored introductory paragraph>
    <the old section body, byte for byte, starting with its own newline>

So the move is INVERTIBLE, and the inverse is this test's whole method:

    first  = raw.index(b"\\n\\n")               -> the H1 line is raw[:first]
    second = raw.index(b"\\n\\n", first + 2)    -> the introduction is between them
    section = b"## " + raw[2:first] + raw[second:]

Concatenating those sections in membership order must reproduce the old
monolith from its first `## ` heading to EOF, byte for byte. The recorded
digests below make that a complete proof without Git; when the base commit is
reachable the same reconstruction is compared against `git show` as well, so a
recorded digest can never stand in for bytes nobody checked.

The entrypoint preamble is deliberately NOT part of the byte proof: it was
replaced by one merged/authored paragraph plus the `## Chapters` membership
list, and `docs/reference-books-migration.md` records what it said before. Its
H1 line IS pinned here, because that line is the release version carrier.
"""

import hashlib
import pathlib
import subprocess

import pytest

from ouroboros.reference_books import BOOK_ENTRYPOINTS, load_reference_book

REPO = pathlib.Path(__file__).resolve().parents[1]

# The integration base this migration was cut from.
MIGRATION_BASE = "5585133db86419c1a28673e498de4fb13c6b2d1e"

# Recorded at the base commit by the split itself (and mirrored in
# docs/reference-books-migration.md): the whole old file, and the part of it
# that moved -- everything from the first `## ` heading to EOF.
OLD_MONOLITHS = {
    "architecture": {
        "old_bytes": 724691,
        "old_sha256": "5db278f8ef5060c4aff5ee1e8743c279661ddd975a311a9bafb5858f32b080de",
        "preamble_bytes": 610,
        "moved_bytes": 724081,
        "moved_sha256": "f1f054c700a15533687e0cf81cf19ac53ffcf022eb179f84c0cbccacbb1d305e",
        "h1": "# Ouroboros v7.0.0 — Architecture & Reference",
    },
    "development": {
        "old_bytes": 275551,
        "old_sha256": "50eb460602f1501915195e3ad1918366312e6292b0dccec6f7ef53f5a5302f3b",
        "preamble_bytes": 60,
        "moved_bytes": 275491,
        "moved_sha256": "bffc00227bc5e91f054b38eaed63acd256c2ddf111e231754bfbe92c7dd122e3",
        "h1": "# DEVELOPMENT.md — Development Principles & Module Guide",
    },
}


def _restore_section(raw: bytes) -> bytes:
    """Undo one chapter's prologue and return the old `## ` section bytes."""
    first = raw.index(b"\n\n")
    second = raw.index(b"\n\n", first + 2)
    h1 = raw[:first]
    assert h1.startswith(b"# "), h1[:40]
    introduction = raw[first + 2:second]
    assert introduction.strip() and b"\n\n" not in introduction, h1
    return b"## " + h1[2:] + raw[second:]


def _reconstruct(book_id: str) -> bytes:
    book = load_reference_book(REPO, book_id)
    assert not book.legacy, f"{book_id} is not chaptered"
    return b"".join(_restore_section(chapter.raw) for chapter in book.chapters)


def _base_bytes(rel: str) -> bytes | None:
    try:
        return subprocess.run(
            ["git", "show", f"{MIGRATION_BASE}:{rel}"],
            cwd=REPO, check=True, capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None  # shallow clone or exported tree: the digests still bind


@pytest.mark.parametrize("book_id", sorted(BOOK_ENTRYPOINTS))
def test_chapters_reconstruct_the_old_monolith_body_byte_for_byte(book_id):
    recorded = OLD_MONOLITHS[book_id]
    moved = _reconstruct(book_id)
    assert len(moved) == recorded["moved_bytes"]
    assert hashlib.sha256(moved).hexdigest() == recorded["moved_sha256"]

    old = _base_bytes(BOOK_ENTRYPOINTS[book_id])
    if old is None:
        pytest.skip(f"base commit {MIGRATION_BASE} is unreachable in this checkout")
    assert len(old) == recorded["old_bytes"]
    assert hashlib.sha256(old).hexdigest() == recorded["old_sha256"]
    assert old[recorded["preamble_bytes"]:] == moved
    assert old[:recorded["preamble_bytes"]].startswith(recorded["h1"].encode("utf-8"))


@pytest.mark.parametrize("book_id", sorted(BOOK_ENTRYPOINTS))
def test_entrypoint_keeps_its_h1_line_and_carries_only_the_membership(book_id):
    rel = BOOK_ENTRYPOINTS[book_id]
    raw = (REPO / rel).read_bytes()
    lines = raw.decode("utf-8").split("\n")
    assert lines[0] == OLD_MONOLITHS[book_id]["h1"]
    book = load_reference_book(REPO, book_id)
    # One `## Chapters` heading and nothing else: the entrypoint orients, the
    # chapters carry the book.
    assert [h.title for h in book.entrypoint.headings] == [lines[0][2:], "Chapters"]
    members = [chapter.source_path for chapter in book.chapters]
    assert members == sorted(members), "membership order must be the reading order"
    for chapter in book.chapters:
        assert chapter.source_path.startswith(f"docs/{book_id}/")
        assert f"]({chapter.source_path[len('docs/'):]})" in book.entrypoint.text


def test_no_chapter_body_was_reheaded_into_a_duplicate_title():
    """Relocation kept `###`/`####` levels, so a section title still names one
    physical place across the whole book -- what `read_book_section` needs."""
    for book_id in BOOK_ENTRYPOINTS:
        book = load_reference_book(REPO, book_id)
        titles = [h.title for source in (book.entrypoint, *book.chapters) for h in source.headings]
        duplicates = sorted({t for t in titles if titles.count(t) > 1})
        assert not duplicates, f"{book_id}: ambiguous section titles {duplicates}"


def test_the_transfer_table_is_committed_and_is_not_a_book_member():
    table = REPO / "docs" / "reference-books-migration.md"
    assert table.is_file(), "the operator transfer table must be reviewable"
    text = table.read_text(encoding="utf-8")
    assert MIGRATION_BASE in text
    for book_id in BOOK_ENTRYPOINTS:
        book = load_reference_book(REPO, book_id)
        assert str(table.relative_to(REPO)) not in [c.source_path for c in book.chapters]
        for chapter in book.chapters:
            assert f"`{chapter.source_path}`" in text, chapter.source_path
        assert OLD_MONOLITHS[book_id]["moved_sha256"] in text
