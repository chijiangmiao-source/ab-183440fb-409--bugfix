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
* Adjacent changed lines stay *independent*: an equal-length replacement is
  paired positionally line-by-line.  Otherwise B editing two adjacent lines
  (say lines 2 and 3) would produce one atomic hunk that overlaps A's edit
  of line 2, falsely dragging B's disjoint edit of line 3 into the conflict
  — and a later save could silently revert it.  Only unequal-length
  replacements (line counts differ, so a safe pairing does not exist) stay
  atomic and conflict conservatively.
* Hunks that touch disjoint base regions merge automatically.
* Hunks that overlap are a conflict — *unless* both sides produced the
  exact same replacement text (e.g. an identical retry), in which case
  that text is taken.
* Two insertions at the exact same line position conflict (their relative
  order is ambiguous); an insertion right next to a changed region merges.

Conflict resolution protocol
----------------------------

After a 409 the client must rebase its reconciliation onto the server's
*full* current text.  Just advancing the base revision while resubmitting
the client's stale draft verbatim would silently revert every disjoint
edit the other terminal committed (the client never saw those lines).

:func:`rebase_template` builds a safe starting point: all clean (disjoint,
already merged) regions are kept verbatim and each conflict region is
pre-filled with the *client's own* text, so no local input is lost.

:func:`apply_conflict_resolution` verifies server-side that a submitted
resolution still contains every disjoint edit the *other* terminal
committed (theirs-only replacements/insertions must be present, theirs-only
deletions must not be revived).  A draft from which those edits are missing
— i.e. an un-rebased stale draft — yields :data:`UNREBASED` and the caller
must reject it without touching the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Optional


@dataclass(frozen=True)
class ConflictRegion:
    """One overlapping region, with the three texts involved."""

    base: str
    mine: str
    theirs: str


@dataclass(frozen=True)
class MergeResult:
    merged_text: str
    conflicts: tuple[ConflictRegion, ...]

    @property
    def clean(self) -> bool:
        return not self.conflicts


# Returned by apply_conflict_resolution when the "resolved" text was never
# rebased onto the server text: a disjoint remote edit is missing, so
# accepting it would silently revert the other terminal's change.
UNREBASED = None  # type: Optional[str]


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
    """(base_start, base_end, replacement_lines) for every non-equal hunk.

    Equal-length replacements are split into per-line hunks so adjacent
    changed lines stay independent (see the module rules).  Insertions,
    deletions and unequal-length replacements stay atomic: a differing line
    count has no safe positional pairing, and a wider conflict there is the
    conservative (never data-losing) outcome.
    """
    matcher = SequenceMatcher(None, base, side, autojunk=False)
    hunks: list[tuple[int, int, list[str]]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and (i2 - i1) == (j2 - j1):
            for offset in range(i2 - i1):
                old_line = base[i1 + offset]
                new_line = side[j1 + offset]
                if old_line != new_line:
                    hunks.append(
                        (i1 + offset, i1 + offset + 1, [new_line])
                    )
        else:
            hunks.append((i1, i2, side[j1:j2]))
    return hunks


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


@dataclass(frozen=True)
class _Segment:
    """One ordered piece of a merge layout.

    A clean segment is text identical on the automatically merged result
    (unchanged base, a one-sided edit, or an identical overlapping edit).
    A conflict segment carries the three fragments; the reconciliation
    fills this slot.

    Only the *other* terminal's (theirs) disjoint edits are safety anchors:

    * ``anchor`` — theirs-only lines (a replacement or insertion) that the
      reconciled text must still contain, in order, or it is un-rebased;
    * ``deleted`` — theirs-only removal of base lines, which the resolution
      must not revive.

    The reconciling client's own disjoint edits are deliberately *not*
    anchors: the script supervisor is free to drop or rework them while
    resolving, as long as nobody else's work is silently reverted.
    """

    clean: tuple[str, ...]  # lines; empty tuple for a conflict segment
    base: tuple[str, ...]
    mine: tuple[str, ...]
    theirs: tuple[str, ...]
    is_conflict: bool
    anchor: bool = False
    deleted: tuple[str, ...] = ()


def _layout(
    base_text: str, mine_text: str, theirs_text: str
) -> tuple[list[_Segment], tuple[ConflictRegion, ...]]:
    """Ordered clean/conflict segments plus the conflict fragments."""
    base = _split_lines(base_text)
    mine = _split_lines(mine_text)
    theirs = _split_lines(theirs_text)

    hunks: list[_TaggedHunk] = [
        ("mine", s, e, repl) for s, e, repl in _change_hunks(base, mine)
    ]
    hunks.extend(
        ("theirs", s, e, repl) for s, e, repl in _change_hunks(base, theirs)
    )
    if not hunks:
        return (
            [_Segment(tuple(base), (), (), (), False)],
            (),
        )

    groups = _group_hunks(hunks)
    segments: list[_Segment] = []
    conflicts: list[ConflictRegion] = []
    cursor = 0
    for group in sorted(
        groups, key=lambda g: (min(h[1] for h in g), max(h[2] for h in g))
    ):
        start = min(h[1] for h in group)
        end = max(h[2] for h in group)
        sides = {h[0] for h in group}

        # Unchanged base lines before this group.
        if start > cursor:
            segments.append(
                _Segment(tuple(base[cursor:start]), (), (), (), False)
            )

        if len(sides) == 1:
            side = next(iter(sides))
            lines = _assemble(base, (start, end), group)
            anchor = False
            deleted: tuple[str, ...] = ()
            if side == "theirs":
                if lines:
                    # The other terminal's disjoint replacement/insertion:
                    # a rebased resolution must still carry it.
                    anchor = True
                else:
                    # The other terminal's disjoint deletion: a rebased
                    # resolution must not bring the removed lines back.
                    removed = "".join(base[start:end])
                    if removed.strip():
                        deleted = tuple(base[start:end])
            segments.append(
                _Segment(tuple(lines), (), (), (), False, anchor, deleted)
            )
        else:
            mine_lines = _assemble(
                base, (start, end), (h for h in group if h[0] == "mine")
            )
            theirs_lines = _assemble(
                base, (start, end), (h for h in group if h[0] == "theirs")
            )
            if mine_lines == theirs_lines:
                # Overlapping edits with identical outcomes: take it once.
                segments.append(
                    _Segment(tuple(mine_lines), (), (), (), False)
                )
            else:
                base_lines = base[start:end]
                segments.append(
                    _Segment(
                        (),
                        tuple(base_lines),
                        tuple(mine_lines),
                        tuple(theirs_lines),
                        True,
                    )
                )
                conflicts.append(
                    ConflictRegion(
                        base="".join(base_lines),
                        mine="".join(mine_lines),
                        theirs="".join(theirs_lines),
                    )
                )
        cursor = end

    if cursor < len(base):
        segments.append(_Segment(tuple(base[cursor:]), (), (), (), False))
    return segments, tuple(conflicts)


def _segments_text(segments: list[_Segment], *, conflicts_from: str) -> str:
    """Join segments; conflict slots filled from ``mine`` or ``theirs``."""
    lines: list[str] = []
    for seg in segments:
        if seg.is_conflict:
            lines.extend(seg.mine if conflicts_from == "mine" else seg.theirs)
        else:
            lines.extend(seg.clean)
    return "".join(lines)


def rebase_template(
    base_text: str, mine_text: str, theirs_text: str
) -> Optional[str]:
    """A safe pre-rebased draft for conflict reconciliation, or None if clean.

    Clean (disjoint) regions are taken from the automatically merged result,
    so the other terminal's non-conflicting edits are already present; each
    conflict region is pre-filled with the *client's own* text so that no
    local input is lost.  The script supervisor edits only the conflict
    regions and saves; :func:`apply_conflict_resolution` accepts the result.
    """
    segments, conflicts = _layout(base_text, mine_text, theirs_text)
    if not conflicts:
        return None
    return _segments_text(segments, conflicts_from="mine")


def apply_conflict_resolution(
    base_text: str,
    mine_text: str,
    theirs_text: str,
    resolved_text: str,
) -> Optional[str]:
    """Verify a human-reconciled draft is rebased, then accept it verbatim.

    ``resolved_text`` must preserve every *non-conflicting* edit the other
    terminal committed:

    * each of theirs' disjoint replacements/insertions (the ``anchor``
      segments) must occur in the resolution, in document order — these
      lines are exactly what an un-rebased stale draft would be missing
      (it was edited before they existed);
    * each block theirs deleted disjointly must not reappear.

    The reconciling client's own disjoint edits are not constrained: the
    script supervisor may keep, change or drop them while resolving.

    Returns ``resolved_text`` when it really was rebased onto the server's
    full current text.  Returns :data:`UNREBASED` (``None``) otherwise, so
    the caller rejects the save and never overwrites remote disjoint edits.
    """
    segments, conflicts = _layout(base_text, mine_text, theirs_text)
    if not conflicts:
        template = _segments_text(segments, conflicts_from="mine")
        return resolved_text if resolved_text == template else None

    resolved_lines = _split_lines(resolved_text)

    # 1) Theirs' disjoint insertions/replacements must survive, in order.
    pos = 0
    for seg in segments:
        if not seg.is_conflict and seg.anchor:
            found = _find_sublist(resolved_lines, list(seg.clean), pos)
            if found is None:
                return UNREBASED
            pos = found + len(seg.clean)

    # 2) Blocks theirs deleted disjointly must not be revived.
    for seg in segments:
        if not seg.is_conflict and seg.deleted:
            block = list(seg.deleted)
            if _find_sublist(resolved_lines, block, 0) is not None:
                return UNREBASED

    return resolved_text


def _find_sublist(haystack: list[str], needle: list[str], start: int) -> Optional[int]:
    """First index >= ``start`` where ``needle`` occurs contiguously."""
    if not needle:
        return start
    if len(needle) > len(haystack) - start:
        return None
    last = len(haystack) - len(needle)
    for i in range(start, last + 1):
        if haystack[i : i + len(needle)] == needle:
            return i
    return None


def three_way_merge(base_text: str, mine_text: str, theirs_text: str) -> MergeResult:
    """Merge ``mine_text`` and ``theirs_text`` from ``base_text``."""
    if mine_text == theirs_text:
        # Both sides arrived at exactly the same text (typical for a retried
        # update whose first attempt already committed): nothing to do.
        return MergeResult(mine_text, ())

    segments, conflicts = _layout(base_text, mine_text, theirs_text)
    # Clean segments (including disjoint edits from either side) always
    # merge; conflict slots are omitted from the merged text and reported.
    merged = "".join(
        line for seg in segments if not seg.is_conflict for line in seg.clean
    )
    return MergeResult(merged, conflicts)
