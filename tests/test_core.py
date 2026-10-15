import os
from datetime import UTC, datetime, timedelta

import pytest

from travel_health_log.core.common import LogError, bp_hint, metrics, settings
from travel_health_log.core.store import Store


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (("119", "79"), "normal_range"),
        (("120", "79"), "elevated_range"),
        (("129", "80"), "stage_1_range"),
        (("130", "79"), "stage_1_range"),
        (("139", "89"), "stage_1_range"),
        (("140", "80"), "stage_2_range"),
        (("125", "90"), "stage_2_range"),
        (("180", "120"), "stage_2_range"),
        (("181", "80"), "severe_range"),
        (("125", "121"), "severe_range"),
    ],
)
def test_official_aha_threshold_boundaries(value, expected):
    result = bp_hint(*map(int, value))
    assert result["category"] == expected
    assert result["source"].startswith("https://www.heart.org/")


def test_metrics_validation_and_idempotence(store):
    assert store.log("2026-10-15", {"sleep": 7.5, "steps": 9200, "bp": "125/80"})["changed"] == 4
    assert store.log("2026-10-15", {"sleep": 7.5, "steps": 9200, "bp": "125/80"})["changed"] == 0
    for invalid in [
        {"sleep": "NaN"},
        {"steps": 1.5},
        {"sleep": True},
        {"weight": -1},
        {"bedtime": "25:00"},
        {"bp": "80/125"},
        {"unknown": 3},
        {"sleep": 25},
    ]:
        with pytest.raises((LogError, ValueError)):
            metrics(invalid)
    assert len(store.records()) == 4
    assert os.stat(store.root / "health.db").st_mode & 0o777 == 0o600
    store.log("2026-10-15", {"sleep": 5}, "apple:Example Watch")
    assert store.daily("2026-10-15", "2026-10-15")[0]["metrics"]["sleep"] == 7.5
    with pytest.raises(LogError):
        store.set_day("2026-10-15", "vacation")
    store.set_day("2026-10-15", "trip", "C01")
    assert store.daily("2026-10-15", "2026-10-15")[0]["status"] == "trip"


def test_ninety_day_hand_calculated_statistics(store, examples):
    result = store.import_file(str(examples / "90-days.csv"))
    assert result == {"rows": 90, "changed_metrics": 1440}
    assert store.import_file(str(examples / "90-days.csv"))["changed_metrics"] == 0
    comparison = store.compare(90, "2026-10-15")
    for context, sleep, steps, alcohol in [
        ("home", 8, 9000, 0),
        ("trip", 6, 6000, 1),
        ("transit", 5, 3000, 0),
    ]:
        group = comparison["groups"][context]
        assert group["recorded_days"] == 30
        for metric, value in [("sleep", sleep), ("steps", steps), ("alcohol", alcohol)]:
            assert group["metrics"][metric] == {"n": 30, "mean": value}
    assert comparison["differences"]["sleep"]["trip_minus_home"] == -2
    assert comparison["differences"]["steps"]["trip_minus_home"] == -3000
    assert comparison["differences"]["alcohol"]["trip_minus_home"] == 1
    stats = store.stats(90, "2026-10-15")
    assert stats["recorded_days"] == 90
    assert stats["weight_7_calendar_day_mean"][-1]["n"] == 7
    assert stats["weight_7_calendar_day_mean"][-1]["value"] == 72.3
    assert stats["meal_sleep_association"]["late_meal"] == {"n": 60, "mean_same_date_sleep": 5.5}
    assert stats["meal_sleep_association"]["earlier_meal"] == {"n": 30, "mean_same_date_sleep": 8}
    assert store.report("2026-W42")["markdown"].startswith("# Travel health journal")


def test_missing_data_habits_bedtime_and_week(store):
    store.log("2026-10-13", {"sleep": 8, "steps": 9000, "bedtime": "23:00"})
    store.log("2026-10-14", {"sleep": 6, "bedtime": "00:30"})
    store.log("2026-10-15", {"steps": 8000, "bedtime": "23:15"})
    store.set_day("2026-10-13", "home")
    store.set_day("2026-10-14", "trip")
    result = store.compare(3, "2026-10-15")
    assert result["groups"]["trip"]["metrics"]["steps"] == {"n": 0, "mean": None}
    assert result["groups"]["unknown"]["recorded_days"] == 1
    store.add_habit("Steps", {"metric": "steps", "op": "gte", "target": 8000}, "2026-10-13")
    store.add_habit(
        "Bedtime", {"metric": "bedtime", "op": "before", "target": "23:30"}, "2026-10-13"
    )
    store.add_habit("Stretch", start="2026-10-13")
    store.check_habit("Stretch", "2026-10-15")
    habits = store.habit_status(3, "2026-10-15")
    assert [(h["completed_days"], h["streak_days_in_window"]) for h in habits] == [
        (2, 1),
        (2, 1),
        (1, 1),
    ]
    assert habits[0]["completion_rate"] == 0.6667
    assert (
        "2/3" in store.report("2026-W42")["markdown"]
        or "2/6" in store.report("2026-W42")["markdown"]
    )
    with pytest.raises(LogError):
        store.add_habit("Bad", {"metric": "sleep", "op": "before", "target": 8})
    with pytest.raises(LogError):
        store.check_habit("missing", "2026-10-15")
    with pytest.raises(LogError):
        store.report("2026-W99")


def test_offline_event_replay_conflict_order_and_atomicity(store):
    now = datetime.now(UTC)

    def event(identifier, time, sleep):
        return {
            "id": identifier,
            "created_at": time.isoformat(),
            "action": "log",
            "payload": {
                "date": "2026-10-15",
                "metrics": {"sleep": sleep},
                "status": "trip",
                "city_code": "C01",
            },
        }

    first = event("offline-first", now - timedelta(minutes=3), 7)
    assert store.apply_event(first)["replayed"] is False
    assert store.apply_event(first)["replayed"] is True
    with pytest.raises(LogError, match="already used"):
        store.apply_event({**first, "payload": {**first["payload"], "metrics": {"sleep": 8}}})
    same = event("offline-newer", now - timedelta(minutes=1), 7)
    store.apply_event(same)
    older = store.apply_event(event("offline-older", now - timedelta(minutes=2), 6))
    assert older["result"]["ignored_stale"] == ["sleep"]
    assert store.daily("2026-10-15", "2026-10-15")[0]["metrics"]["sleep"] == 7
    bad = event("invalid-event", now, 8)
    bad["payload"]["status"] = "bad"
    with pytest.raises(LogError):
        store.apply_event(bad)
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
    assert store.daily("2026-10-15", "2026-10-15")[0]["metrics"]["sleep"] == 7
    with pytest.raises(LogError):
        store.apply_event(event("future-event", now + timedelta(days=1), 8))
    store.add_habit("Stretch", start="2026-10-15")
    item = {
        "id": "habit-event",
        "created_at": now.isoformat(),
        "action": "habit",
        "payload": {"name": "Stretch", "date": "2026-10-15", "done": True},
    }
    store.apply_event(item)
    store.apply_event(item)
    assert store.db.execute("SELECT COUNT(*) FROM checkins").fetchone()[0] == 1


def test_medication_and_review_due_never_changes_doses(store):
    store.add_med("Example plan", ["08:00", "20:00"], "2026-10-01", "2026-10-20")
    store.add_reminder("Example follow-up", "2026-10-15")
    assert len(store.due("2026-10-15", "09:00")["medications"]) == 1
    store.take_med("Example plan", "2026-10-15", "08:00")
    store.take_med("Example plan", "2026-10-15", "08:00")
    assert len(store.due("2026-10-15")["medications"]) == 1
    assert len(store.due("2026-10-15")["reminders"]) == 1
    store.complete_reminder(1)
    assert store.due("2026-10-15")["reminders"] == []
    assert store.due("2026-10-21")["medications"] == []
    with pytest.raises(LogError):
        store.take_med("Example plan", "2026-10-15", "10:00")


def test_timezone_config_fixed_after_creation(store, tmp_path):
    with pytest.raises(LogError, match="calendar timezone"):
        Store(store.root, {**store.config, "timezone": "America/New_York"})
    path = tmp_path / "bad.yaml"
    path.write_text("surprise: true")
    with pytest.raises(LogError):
        settings(tmp_path / "elsewhere", str(path))
    path.write_text("private_permissions: true\ntimezone: UTC")
    assert settings(tmp_path / "elsewhere", str(path))[1]["timezone"] == "UTC"


def test_inconsistent_pressure_pair_and_text_habit_are_not_interpreted(store):
    assert bp_hint(80, 125)["category"] == "invalid_pair"
    with pytest.raises(LogError):
        store.add_habit(
            "Invalid text rule", {"metric": "exercise_type", "op": "gte", "target": "Walking"}
        )
