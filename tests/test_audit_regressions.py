"""Focused regressions from the independent acceptance review."""

import csv
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


def test_trip_import_streams_past_large_non_trip_payloads(store, tmp_path):
    source = tmp_path / "expense-archive.csv"
    trip = {
        "start_at": "2026-10-10T08:00:00+08:00",
        "end_at": "2026-10-12T18:00:00+08:00",
        "destination": "C01",
    }
    with source.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["entity", "payload"])
        writer.writeheader()
        # Intentionally invalid JSON proves irrelevant receipt payloads are not decoded.
        writer.writerow({"entity": "receipt_files", "payload": "x" * 200000})
        writer.writerow({"entity": "trips", "payload": json.dumps(trip)})
        writer.writerow({"entity": "receipts", "payload": "y" * 200000})
    previous = csv.field_size_limit(131072)
    try:
        result = store.import_trips(str(source))
        assert csv.field_size_limit() == 20 * 1024 * 1024
    finally:
        csv.field_size_limit(previous)
    assert result["trips"] == 1
    assert result["marked_days"] == 3
    assert [dict(row) for row in store.db.execute("SELECT date,status,city_code FROM days")] == [
        {"date": "2026-10-10", "status": "transit", "city_code": "C01"},
        {"date": "2026-10-11", "status": "trip", "city_code": "C01"},
        {"date": "2026-10-12", "status": "transit", "city_code": "C01"},
    ]
    assert store.import_trips(str(source))["marked_days"] == 3
    assert store.db.execute("SELECT COUNT(*) FROM days").fetchone()[0] == 3


def test_streamed_trip_import_rolls_back_before_later_invalid_trip(store, tmp_path):
    source = tmp_path / "invalid-trips.csv"
    source.write_text(
        "start_at,end_at,destination\n"
        "2026-10-10T08:00:00,2026-10-12T18:00:00,C01\n"
        "2026-10-15T08:00:00,2026-10-14T18:00:00,C02\n",
        encoding="utf-8",
    )
    with pytest.raises(LogError, match="duration"):
        store.import_trips(str(source))
    assert store.db.execute("SELECT COUNT(*) FROM days").fetchone()[0] == 0
