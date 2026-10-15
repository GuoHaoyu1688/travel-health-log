import csv
import json
import zipfile

import pytest
from defusedxml.common import EntitiesForbidden

from travel_health_log.core.apple import import_apple
from travel_health_log.core.common import LogError, settings
from travel_health_log.core.store import Store


def test_apple_real_xml_idempotency_units_sleep_overlap_and_sources(store, examples):
    result = import_apple(store, str(examples / "apple-health.xml"))
    assert (result["inserted"], result["ignored"]) == (7, 2)
    assert import_apple(store, str(examples / "apple-health.xml"))["duplicates"] == 7
    daily = store.daily("2026-10-14", "2026-10-15")
    assert daily[0]["metrics"]["steps"] == 60
    assert daily[1]["metrics"] == {"sleep": 7.0, "steps": 940, "weight": 72.574779}
    assert daily[1]["sources"]["steps"] == "apple:Example Watch"
    store.config["preferred_apple_source"] = "Example Phone"
    assert store.daily("2026-10-15", "2026-10-15")[0]["metrics"]["steps"] == 800
    store.log("2026-10-15", {"steps": 999})
    import_apple(store, str(examples / "apple-health.xml"))
    assert store.daily("2026-10-15", "2026-10-15")[0]["metrics"]["steps"] == 999


def test_streamed_apple_dst_and_atomic_bad_record(tmp_path):
    root, config = settings(tmp_path / "dst")
    config["timezone"] = "America/New_York"
    store = Store(root, config)
    path = tmp_path / "dst.xml"
    path.write_text(
        '<HealthData><Record type="HKCategoryTypeIdentifierSleepAnalysis" '
        'sourceName="Example Watch" value="HKCategoryValueSleepAnalysisAsleepCore" '
        'startDate="2026-11-01 00:30:00 -0400" endDate="2026-11-01 02:30:00 -0500"/>'
        "</HealthData>"
    )
    import_apple(store, str(path))
    assert store.daily("2026-11-01", "2026-11-01")[0]["metrics"]["sleep"] == 3
    bad = tmp_path / "bad.xml"
    bad.write_text(
        '<HealthData><Record type="HKQuantityTypeIdentifierStepCount" '
        'sourceName="Example" unit="count" value="100" startDate="2026-11-02 01:00:00 -0500" '
        'endDate="2026-11-02 02:00:00 -0500"/><Record type="HKQuantityTypeIdentifierBodyMass" '
        'sourceName="Example" unit="stone" value="10" startDate="2026-11-02 02:00:00 -0500" '
        'endDate="2026-11-02 02:00:00 -0500"/></HealthData>'
    )
    with pytest.raises(LogError):
        import_apple(store, str(bad))
    assert store.db.execute("SELECT COUNT(*) FROM apple_samples").fetchone()[0] == 1
    hostile = tmp_path / "hostile.xml"
    hostile.write_text('<!DOCTYPE HealthData [<!ENTITY x "secret">]><HealthData>&x;</HealthData>')
    with pytest.raises(EntitiesForbidden):
        import_apple(store, str(hostile))
    store.close()


def test_generic_csv_json_archive_trip_and_backup(store, examples, tmp_path):
    store.import_file(str(examples / "90-days.csv"))
    trip = store.import_trips(str(examples / "trips.csv"))
    assert trip["marked_days"] == 4
    assert store.daily("2026-10-02", "2026-10-05")[0]["status"] == "transit"
    assert store.daily("2026-10-02", "2026-10-05")[1]["status"] == "trip"
    store.add_habit("Stretch", start="2026-10-01")
    store.check_habit("Stretch", "2026-10-15")
    store.add_med("Example", ["08:00"], "2026-10-01")
    store.take_med("Example", "2026-10-15", "08:00")
    store.add_reminder("Example follow-up", "2026-10-15")
    for format in ["json", "csv"]:
        path = tmp_path / f"archive.{format}"
        store.export(format, str(path))
        root, config = settings(tmp_path / f"restored-{format}")
        other = Store(root, config)
        assert other.import_file(str(path))["entries"] == 1440
        assert other.import_file(str(path))["reused"]
        assert other.compare(90, "2026-10-15") == store.compare(90, "2026-10-15")
        assert other.due("2026-10-15") == store.due("2026-10-15")
        other.close()
    for format in ["json", "csv"]:
        path = tmp_path / f"entries.{format}"
        store.export(format, str(path), "entries")
        root, config = settings(tmp_path / f"flat-{format}")
        other = Store(root, config)
        assert other.import_file(str(path))["changed_metrics"] == 1440
        other.close()
    backup = store.backup()
    with zipfile.ZipFile(backup["path"]) as file:
        assert set(file.namelist()) == {"health.db", "archive.json", "config.yaml"}
    # Project 18's native CSV contains trip entities among other records.
    native = tmp_path / "expense-archive.csv"
    with native.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["entity", "payload"])
        writer.writeheader()
        writer.writerow(
            {
                "entity": "trips",
                "payload": json.dumps(
                    {
                        "start_at": "2026-10-17T08:00+08:00",
                        "end_at": "2026-10-19T20:00+08:00",
                        "destination": "C02",
                    }
                ),
            }
        )
    assert store.import_trips(str(native))["marked_days"] == 3


def test_generic_invalid_rows_roll_back(store, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps([{"date": "2026-10-15", "sleep": 7}, {"date": "2026-10-16", "sleep": "NaN"}])
    )
    with pytest.raises(LogError):
        store.import_file(str(path))
    assert store.records() == []
    with pytest.raises(LogError):
        store.export("xml", str(tmp_path / "bad"))


def test_overlapping_step_samples_warn_without_summing_devices(store, tmp_path):
    path = tmp_path / "overlap.xml"
    path.write_text(
        '<HealthData><Record type="HKQuantityTypeIdentifierStepCount" '
        'sourceName="Example Watch" unit="count" value="100" '
        'startDate="2026-10-15 08:00:00 +0800" endDate="2026-10-15 09:00:00 +0800"/>'
        '<Record type="HKQuantityTypeIdentifierStepCount" sourceName="Example Watch" '
        'unit="count" value="200" startDate="2026-10-15 08:30:00 +0800" '
        'endDate="2026-10-15 09:30:00 +0800"/></HealthData>'
    )
    result = import_apple(store, str(path))
    assert len(result["warnings"]) == 1 and "overlapping step" in result["warnings"][0]
