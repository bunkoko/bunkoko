"""--episode latest：いちばん新しい回の ID にする（回の ID は企画を作るまで分からないため）。"""

from __future__ import annotations

import pytest
import typer

from koten.cli import _episode_id
from koten.db import connect


def test_latest_is_the_newest_episode_that_is_not_discarded(tmp_path):
    conn = connect(tmp_path)
    with conn:
        for eid, created, state in (
            ("E20261124-01", "2026-11-24 01:00:00", "script_done"),
            ("E20261124-02", "2026-11-24 02:00:00", "planned"),
            ("E20261124-03", "2026-11-24 03:00:00", "discarded"),
        ):
            conn.execute(
                "INSERT INTO episodes(episode_id, format, worry_ids, state, created_at) "
                "VALUES (?, 'A', '[]', ?, ?)",
                (eid, state, created),
            )
    assert _episode_id("latest", conn) == "E20261124-02"
    assert _episode_id("最新", conn) == "E20261124-02"
    assert _episode_id("E20261124-01", conn) == "E20261124-01"


def test_latest_without_episodes_stops(tmp_path):
    with pytest.raises(typer.Exit):
        _episode_id("latest", connect(tmp_path))
