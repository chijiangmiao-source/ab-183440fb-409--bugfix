"""Deterministic line-based three-way merge for revisable shot notes.

Given a common ``base`` text and two independently edited versions
(``mine`` and ``theirs``), :func:`three_way_merge` produces either a single
merged text or conflict regions that contain all three fragments for the
script supervisor to reconcile manually.

The merge is purely functional and deterministic: given the same three
inputs it always returns the same output, regardless of which terminal
"wins" the database write lock first.

Rules
-----

* A hunk is a maximal run of lines that one side replaced/inserted/deleted
  relative to ``base`` (computed with :class:`difflib.SequenceMatcher`,
  ``autojunk=False`` so results never depend on string length heuristics).
* Hunks that touch disjoint base regions merge automatically.
* Hunks that overlap are a conflict — *unless* both sides produced the
  exact same replacement text (e.g. an identical retry), in which case
  that text is taken.
* Two insertions at the exact same line position conflict (their relative
  order is ambiguous); an insertion right next to a changed region merges.

Tight conflicts
---------------
When two terminals change *different* lines inside one coarse overlapping
span (e.g. one terminal saves a single edit that replaces both lines 2 and
3, and the other terminal only edits line 2), a naive conflict would cover
the whole span and hide the server's disjoint line-3 change inside the
fragment.  We therefore refine a conflict span into per-line blocks, but
**only when the refinement is forced and unambiguous**: every hunk in the
span must be a pure N→N line-aligned replacement (no insertion, deletion or
structural N↔M edit) and every base line it removes must be unique in the
whole base document.  Under those conditions each edited line corresponds
to exactly one base line on both sides, so the per-line merge cannot pair a
change with the wrong repeated line; lines changed by only one side become
clean blocks (surviving rebase) and lines changed differently by both sides
stay tight, single-line conflicts.  In every other situation the span is
reported as one coarse :class:`ConflictRegion` exactly as before.

Besides ``merged_text`` and ``conflicts``, the result carries ``blocks``:
the ordered scaffold of clean text blocks interleaved with conflict blocks.
A clean block contains the merged content of a non-conflicting span —
unchanged lines plus *either side's* disjoint edits — so every server-only
edit is guaranteed to appear there.  A conflicted client rebases by taking
every clean block verbatim and filling the conflict blocks (initially with
its own text); resubmitting that text against the server's current revision
can therefore never silently revert a non-conflicting remote edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Union


@dataclass(frozen=True)
class TextBlock:
    """A clean, already-merged run of text (identical on both sides)."""

    text: str


@dataclass(frozen=True)
class ConflictRegion:
    """One overlapping region, with the three texts involved."""

    base: str
    mine: str
    theirs: str


MergeBlock = Union[TextBlock, ConflictRegion]


@dataclass(frozen=True)
class MergeResult:
    merged_text: str
    conflicts: tuple[ConflictRegion, ...]
    #: Ordered scaffold: TextBlock (take verbatim) | ConflictRegion (resolve).
    blocks: tuple[MergeBlock, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.conflicts

    def as_scaffold(self) -> list[dict]:
        """JSON-serialisable ordered scaffold for the 409 response.

        A client rebases by taking every clean ``text`` block verbatim (they
        already include the server's disjoint edits) and substituting each
        ``conflict`` block with the reconciled text (initially the client's own
        ``mine`` fragment, which keeps its input).  The server's full current
        note is sent separately as ``server_notes``.
        """
        scaffold: list[dict] = []
        for block in self.blocks:
            if isinstance(block, TextBlock):
                scaffold.append({"type": "text", "text": block.text})
            else:
                scaffold.append(
                    {
                        "type": "conflict",
                        "base": block.base,
                        "mine": block.mine,
                        "theirs": block.theirs,
                    }
                )
        return scaffold


def _split_lines(text: str) -> list[str]:
    """Split text keeping the ``\\n`` attached to every line but the last.

    Unlike ``str.splitlines`` this preserves empty trailing lines and never
    treats other Unicode line separators as line breaks.
    """
    if text == "":
        return []
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + parts[-1:]


def _change_hunks(base: list[str], side: list[str]) -> list[tuple[int, int, list[str]]]:
    """(base_start, base_end, replacement_lines) for every non-equal hunk."""
    matcher = SequenceMatcher(None, base, side, autojunk=False)
    return [
        (i1, i2, side[j1:j2])
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def _overlaps(h1: tuple[int, int], h2: tuple[int, int]) -> bool:
    """Whether two base-coordinate spans interact.

    Half-open ranges ``[start, end)``; ``start == end`` is a pure insertion
    at that line position.
    """
    s1, e1 = h1
    s2, e2 = h2
    if s1 == e1 and s2 == e2:
        # Two pure insertions only interact at the exact same position.
        return s1 == s2
    if s1 == e1:
        # An insertion merges when adjacent to a changed range, conflicts
        # only when it sits strictly inside it.
        return s2 < s1 < e2
    if s2 == e2:
        return s1 < s2 < e1
    return s1 < e2 and s2 < e1


# A hunk tagged with the side it came from: 'mine' | 'theirs'.
_TaggedHunk = tuple[str, int, int, list[str]]


def _group_hunks(hunks: list[_TaggedHunk]) -> list[list[_TaggedHunk]]:
    """Group hunks that overlap transitively (fixed point on pair merges)."""
    groups: list[list[_TaggedHunk]] = [[hunk] for hunk in hunks]
    merged_any = True
    while merged_any:
        merged_any = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                if any(
                    _overlaps((hs, he), (gs, ge))
                    for _, hs, he, _ in groups[i]
                    for _, gs, ge, _ in groups[j]
                ):
                    groups[i] = groups[i] + groups[j]
                    del groups[j]
                    merged_any = True
                    break
            if merged_any:
                break
    return groups


def _assemble(
    base: list[str], span: tuple[int, int], side_hunks: Iterable[_TaggedHunk]
) -> list[str]:
    """Reconstruct one side's text for a conflict span, keeping unchanged gaps."""
    start, end = span
    lines: list[str] = []
    pos = start
    for _, hs, he, replacement in sorted(side_hunks, key=lambda h: (h[1], h[2])):
        lines.extend(base[pos:hs])
        lines.extend(replacement)
        pos = he
    lines.extend(base[pos:end])
    return lines


def _aligned_per_line_maps(
    base: list[str],
    span: tuple[int, int],
    side_hunks: list[_TaggedHunk],
    base_counts: dict[str, int],
) -> dict[int, str] | None:
    """Map ``base index -> new line`` for a side inside ``span``, if forced.

    Returns ``None`` when the side cannot be expressed as independent per-line
    replacements of unique base lines (it contains an insertion, a deletion,
    an N↔M structural edit, or touches a non-unique base line).

    Hunks of a single side never overlap, so a base index is mapped at most
    once; untouched indices are simply absent from the map.
    """
    start, end = span
    mapped: dict[int, str] = {}
    for _, hs, he, replacement in side_hunks:
        if hs < start or he > end:
            return None
        removed = base[hs:he]
        if hs == he or not removed or len(removed) != len(replacement):
            # Insertion, deletion or structural edit: cannot refine.
            return None
        if any(base_counts[line] != 1 for line in removed):
            # A repeated line: its alignment with the new lines is ambiguous.
            return None
        for offset, new_line in enumerate(replacement):
            idx = hs + offset
            assert idx not in mapped
            mapped[idx] = new_line
    return mapped


def _refine_conflict_span(
    base: list[str],
    span: tuple[int, int],
    mine_hunks: list[_TaggedHunk],
    theirs_hunks: list[_TaggedHunk],
    base_counts: dict[str, int],
) -> list[MergeBlock] | None:
    """Refine an overlapping span into per-line blocks, when unambiguous.

    Returns a list of text/conflict blocks covering the span, or ``None`` if
    the span cannot be safely refined (then the caller reports it as one
    coarse conflict exactly as the original algorithm did).
    """
    start, end = span
    mine_map = _aligned_per_line_maps(base, span, mine_hunks, base_counts)
    theirs_map = _aligned_per_line_maps(base, span, theirs_hunks, base_counts)
    if mine_map is None or theirs_map is None:
        return None

    blocks: list[MergeBlock] = []

    def push(text: str) -> None:
        if text:
            if blocks and isinstance(blocks[-1], TextBlock):
                blocks[-1] = TextBlock(blocks[-1].text + text)
            else:
                blocks.append(TextBlock(text))

    for idx in range(start, end):
        original = base[idx]
        mine_line = mine_map.get(idx, original)
        theirs_line = theirs_map.get(idx, original)
        if mine_line == theirs_line:
            # Unchanged on one side, or both sides made it identical.
            push(mine_line)
        elif mine_line == original:
            # Only the server changed this line: a clean, non-conflicting edit.
            push(theirs_line)
        elif theirs_line == original:
            # Only this terminal changed this line: keep its text.
            push(mine_line)
        else:
            blocks.append(
                ConflictRegion(base=original, mine=mine_line, theirs=theirs_line)
            )
    return blocks


def three_way_merge(base_text: str, mine_text: str, theirs_text: str) -> MergeResult:
    """Merge ``mine_text`` and ``theirs_text`` from ``base_text``."""
    if mine_text == theirs_text:
        # Both sides arrived at exactly the same text (typical for a retried
        # update whose first attempt already committed): nothing to do.
        return MergeResult(mine_text, (), (TextBlock(mine_text),))

    base = _split_lines(base_text)
    mine = _split_lines(mine_text)
    theirs = _split_lines(theirs_text)

    base_counts: dict[str, int] = {}
    for line in base:
        base_counts[line] = base_counts.get(line, 0) + 1

    hunks: list[_TaggedHunk] = [
        ("mine", s, e, repl) for s, e, repl in _change_hunks(base, mine)
    ]
    hunks.extend(
        ("theirs", s, e, repl) for s, e, repl in _change_hunks(base, theirs)
    )
    if not hunks:
        return MergeResult(base_text, (), (TextBlock(base_text),))

    groups = _group_hunks(hunks)
    blocks: list[MergeBlock] = []
    cursor = 0
    for group in sorted(groups, key=lambda g: (min(h[1] for h in g), max(h[2] for h in g))):
        start = min(h[1] for h in group)
        end = max(h[2] for h in group)
        sides = {h[0] for h in group}

        # Unchanged base lines before this group.
        leading = "".join(base[cursor:start])
        if leading:
            blocks.append(TextBlock(leading))

        if len(sides) == 1:
            blocks.append(TextBlock("".join(_assemble(base, (start, end), group))))
        else:
            mine_hunks = [h for h in group if h[0] == "mine"]
            theirs_hunks = [h for h in group if h[0] == "theirs"]
            mine_lines = _assemble(base, (start, end), mine_hunks)
            theirs_lines = _assemble(base, (start, end), theirs_hunks)
            if mine_lines == theirs_lines:
                # Overlapping edits with identical outcomes: take the text once.
                blocks.append(TextBlock("".join(mine_lines)))
            else:
                refined = _refine_conflict_span(
                    base, (start, end), mine_hunks, theirs_hunks, base_counts
                )
                if refined is not None:
                    blocks.extend(refined)
                else:
                    blocks.append(
                        ConflictRegion(
                            base="".join(base[start:end]),
                            mine="".join(mine_lines),
                            theirs="".join(theirs_lines),
                        )
                    )
        cursor = end

    trailing = "".join(base[cursor:])
    if trailing:
        blocks.append(TextBlock(trailing))

    conflicts = tuple(b for b in blocks if isinstance(b, ConflictRegion))
    # Only meaningful when there are no conflicts; clean blocks then cover
    # the whole document.
    merged_text = "".join(b.text for b in blocks if isinstance(b, TextBlock))
    return MergeResult(merged_text, conflicts, tuple(blocks))
