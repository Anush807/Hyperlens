"""The backfill CLI: explicit ranges, and when ground truth may be written.

`ground_truth.json` is what `python -m ml.evaluate` scores against, so writing
it from a partial run silently invalidates every anomaly number. These tests
exist because that actually happened.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from synthetic.generate_history import _parse_when, generate


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-22", datetime(2026, 10, 22, tzinfo=UTC)),
        ("2026-10-22T06:30", datetime(2026, 10, 22, 6, 30, tzinfo=UTC)),
        ("2026-10-22T06:30:00+00:00", datetime(2026, 10, 22, 6, 30, tzinfo=UTC)),
    ],
)
def test_when_accepts_dates_and_datetimes_and_assumes_utc(value, expected):
    assert _parse_when(value, "--from") == expected


def test_when_is_optional():
    assert _parse_when(None, "--from") is None


def test_a_bad_when_fails_with_a_usable_message():
    with pytest.raises(SystemExit, match="not an ISO date or datetime"):
        _parse_when("last tuesday", "--from")


def test_an_inverted_range_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "synthetic.generate_history._entities_for_source", lambda s: [], raising=True
    )
    with pytest.raises(SystemExit, match="must be before"):
        generate(
            180,
            300,
            5000,
            dry_run=True,
            start=datetime(2026, 11, 1, tzinfo=UTC),
            end=datetime(2026, 10, 1, tzinfo=UTC),
        )


def test_ground_truth_only_refuses_a_single_source():
    """The file describes every source, so a filtered run cannot produce it."""
    with pytest.raises(SystemExit, match="cannot be combined with --source"):
        generate(180, 300, 5000, only_source="vcsim-lab", ground_truth_only=True)
