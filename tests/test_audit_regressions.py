"""Focused regressions from the independent acceptance review."""

import json
import os

import pytest

from travel_health_log.cli import main
from travel_health_log.core.common import LogError
from travel_health_log.core.store import Store


def test_rolling_weight_uses_history_before_display_window(store):
    store.log("2026-10-02", {"weight": 100})  # Outside the Oct 9 rolling window.
    store.log("2026-10-08", {"weight": 60})
    store.log("2026-10-09", {"weight": 80})
    narrow = store.stats(7, "2026-10-15")
    wide = store.stats(30, "2026-10-15")
    expected = {"date": "2026-10-09", "value": 70.0, "n": 2}
    assert narrow["weight_7_calendar_day_mean"] == [expected]
    assert wide["weight_7_calendar_day_mean"][-1] == expected
    assert [row["date"] for row in narrow["daily"]] == ["2026-10-09"]
    assert narrow["recorded_days"] == 1


def test_rolling_weight_supports_first_allowed_calendar_date(store):
    store.log("1900-01-01", {"weight": 70})
    assert store.stats(1, "1900-01-01")["weight_7_calendar_day_mean"] == [
        {"date": "1900-01-01", "value": 70.0, "n": 1}
    ]


@pytest.mark.parametrize(
    "destination", ["database", "symlink", "hardlink", "-wal", "-shm", "-journal"]
)
def test_export_rejects_database_aliases_and_sidecars(store, tmp_path, destination):
    store.log("2026-10-15", {"sleep": 7})
    database = store.root / "health.db"
    if destination == "database":
        target = database
    elif destination == "symlink":
        target = tmp_path / "alias.json"
        target.symlink_to(database)
    elif destination == "hardlink":
        target = tmp_path / "hardlink.json"
        os.link(database, target)
    else:
        target = store.root / ("health.db" + destination)
    with pytest.raises(LogError) as error:
        store.export("json", str(target), overwrite=True)
    assert error.value.code == "PROTECTED_PATH"
    assert database.read_bytes().startswith(b"SQLite format 3")
    reopened = Store(store.root, store.config)
    try:
        assert reopened.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
    finally:
        reopened.close()


def test_report_cannot_overwrite_database(store, capsys):
    store.log("2026-10-15", {"sleep": 7})
    assert (
        main(
            [
                "--data-dir",
                str(store.root),
                "report",
                "--week",
                "2026-W42",
                "--out",
                str(store.root / "health.db"),
                "--yes",
                "--json",
            ]
        )
        == 2
    )
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "PROTECTED_PATH"
    assert store.records()[0]["value"] == 7
