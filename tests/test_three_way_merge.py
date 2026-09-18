"""Unit tests for the deterministic line-based three-way merge engine.

These exercise ``app.merge`` directly (the API lives under ``../api``) in
addition to the real-HTTP acceptance tests.  They focus on the property that
matters for conflict rebasing: an aligned multi-line save must yield a *tight*
per-line conflict so that the save's disjoint edits stay clean and can never
be reverted when the conflict is reconciled.
"""

from __future__ import annotations

import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from app.merge import ConflictRegion, TextBlock, three_way_merge  # noqa: E402


def _clean_text(result) -> str:
    """All non-conflicting (already merged) text of a result."""
    return "".join(b.text for b in result.blocks if isinstance(b, TextBlock))


def _server_text_when_client_only_conflicted(result) -> str:
    """Rebuild the server note when the client edited nothing but conflicts.

    In the conflict-rebase scenario the stale client's *only* edit sits inside
    a conflict block, so every clean block derives from the server; filling
    conflicts with ``theirs`` must therefore reconstruct the server's note.
    """
    return "".join(
        b.text if isinstance(b, TextBlock) else b.theirs for b in result.blocks
    )


def test_aligned_multiline_save_keeps_disjoint_line_out_of_conflict():
    # B changes lines 2 AND 3 in one save; A (stale) changes only line 2.
    result = three_way_merge(
        "第一行\n第二行\n第三行",
        "第一行\nA第二行\n第三行",
        "第一行\nB第二行\nB第三行",
    )
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    # Tight: the conflict covers line 2 only, not the 2..3 hunk B submitted.
    assert conflict.base == "第二行\n"
    assert conflict.mine == "A第二行\n"
    assert conflict.theirs == "B第二行\n"

    # B's disjoint third line is a clean block and therefore survives rebase.
    assert "B第三行" in _clean_text(result)
    # A edited only the conflicting line, so the server note round-trips.
    assert (
        _server_text_when_client_only_conflicted(result)
        == "第一行\nB第二行\nB第三行"
    )


def test_disjoint_aligned_edits_still_auto_merge():
    result = three_way_merge(
        "第一段\n第二段\n第三段\n",
        "第一段（A改）\n第二段\n第三段\n",
        "第一段\n第二段\n第三段（B改）\n",
    )
    assert result.clean
    assert result.merged_text == "第一段（A改）\n第二段\n第三段（B改）\n"


def test_clean_scaffold_round_trips_identical_text():
    result = three_way_merge("a\nb\nc\n", "a\nb\nc\n", "a\nb\nc\n")
    assert result.clean
    assert result.merged_text == "a\nb\nc\n"
    assert _clean_text(result) == "a\nb\nc\n"


def test_structural_n_to_m_replacement_stays_one_conflict():
    # A replaces line 2 with two lines (1->2, structural -> atomic), B edits
    # the same region: the structural hunk cannot be split, so it conflicts as
    # a whole and its fragments stay coherent.
    result = three_way_merge(
        "a\nb\nc\n",
        "a\nb1\nb2\nc\n",
        "a\nB\nc\n",
    )
    assert len(result.conflicts) == 1
    assert result.conflicts[0].base == "b\n"
    assert result.conflicts[0].mine == "b1\nb2\n"
    assert result.conflicts[0].theirs == "B\n"
    assert _server_text_when_client_only_conflicted(result) == "a\nB\nc\n"


def test_repeated_lines_keep_one_coarse_conflict_no_per_line_refinement():
    # Safety boundary: line 2 is non-unique (also present at line 3), so the
    # N→N replacement must NOT be split per line.  Both terminals change a
    # duplicate line differently; alignment is ambiguous, therefore the span
    # stays a single coarse conflict instead of a guessed auto-merge.
    result = three_way_merge(
        "x\nx\ny\n",
        "x\nA\ny\n",
        "x\nB\ny\n",
    )
    assert len(result.conflicts) == 1
    # The conflict spans the repeated line region as one fragment.
    assert result.conflicts[0].base == "x\n"
    assert result.conflicts[0].mine == "A\n"
    assert result.conflicts[0].theirs == "B\n"


def test_only_one_side_edits_line_takes_that_side():
    result = three_way_merge("a\nb\nc\n", "a\nb\nc\n", "a\nB\nc\n")
    assert result.clean
    assert result.merged_text == "a\nB\nc\n"


def test_server_only_disjoint_edits_always_live_in_clean_blocks():
    # Property: wherever the server edited a region the client did not touch,
    # that text must be in a clean block (so rebasing preserves it).  The
    # client's own edits are confined to the conflicted line in every case.
    cases = [
        # base, mine (only the conflicting line), theirs (conflict + disjoint)
        ("l1\nl2\nl3", "l1\nA\nl3", "l1\nB\nB3", "B3"),
        ("x\ny\nz", "x\nMY\nz", "x\nTY\nTZ", "TZ"),
        ("r0", "r0-A", "r0-B", None),
    ]
    for base, mine, theirs, disjoint in cases:
        result = three_way_merge(base, mine, theirs)
        assert not result.clean, (base, mine, theirs)
        if disjoint is not None:
            assert disjoint in _clean_text(result)
        # Server note round-trips because the client only edited conflicts.
        assert _server_text_when_client_only_conflicted(result) == theirs
        assert all(isinstance(b, (TextBlock, ConflictRegion)) for b in result.blocks)
