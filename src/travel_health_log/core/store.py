"""Private local storage, idempotent offline events and habit/reminder tracking."""

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

from .analysis import AnalysisMixin
from .common import (
    METRICS,
    LogError,
    bounded_text,
    canonical,
    clock,
    day,
    metrics,
    today,
    utc_now,
)
from .interchange import InterchangeMixin


class Store(AnalysisMixin, InterchangeMixin):
    def __init__(self, root, config):
        self.root, self.config = root, config
        root.mkdir(parents=True, exist_ok=True)
        if config["private_permissions"]:
            root.chmod(0o700)
        self.db = sqlite3.connect(root / "health.db", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS entries(date TEXT,metric TEXT,value TEXT,unit TEXT,
          source TEXT,note TEXT,updated_at TEXT,PRIMARY KEY(date,metric,source));
        CREATE TABLE IF NOT EXISTS days(date TEXT PRIMARY KEY,status TEXT,city_code TEXT,
          updated_at TEXT);
        CREATE TABLE IF NOT EXISTS habits(id INTEGER PRIMARY KEY,name TEXT UNIQUE,
          rule_json TEXT,active INTEGER,created_date TEXT);
        CREATE TABLE IF NOT EXISTS checkins(habit_id INTEGER REFERENCES habits(id),date TEXT,
          done INTEGER,PRIMARY KEY(habit_id,date));
        CREATE TABLE IF NOT EXISTS meds(id INTEGER PRIMARY KEY,name TEXT UNIQUE,schedule TEXT,
          start TEXT,end TEXT);
        CREATE TABLE IF NOT EXISTS doses(med_id INTEGER REFERENCES meds(id),date TEXT,time TEXT,
          PRIMARY KEY(med_id,date,time));
        CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY,title TEXT,due_date TEXT,
          completed INTEGER DEFAULT 0,UNIQUE(title,due_date));
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,digest TEXT,result_json TEXT,
          received_at TEXT);
        CREATE TABLE IF NOT EXISTS apple_samples(id TEXT PRIMARY KEY,metric TEXT,start_at TEXT,
          end_at TEXT,value REAL,source TEXT);
        CREATE INDEX IF NOT EXISTS sample_dates ON apple_samples(metric,source,start_at);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
        """)
        existing = self.db.execute("SELECT value FROM metadata WHERE key='timezone'").fetchone()
        if existing and existing[0] != config["timezone"]:
            self.db.close()
            raise LogError(
                "TIMEZONE_CHANGED", "Create a new library to change its calendar timezone"
            )
        self.db.execute(
            "INSERT OR IGNORE INTO metadata VALUES(?,?)", ("timezone", config["timezone"])
        )
        self.db.commit()
        if config["private_permissions"]:
            os.chmod(root / "health.db", 0o600)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def log(
        self,
        date: str,
        values: dict,
        source: str = "manual",
        note: str = "",
        updated_at: str | None = None,
    ) -> dict:
        day(date)
        parsed = metrics(values)
        bounded_text(source, "source", 200)
        if not source:
            raise LogError("INVALID_SOURCE", "source cannot be empty")
        bounded_text(note, "note", 2000)
        stamp = updated_at or utc_now()
        changed = 0
        ignored_stale = []
        with self.transaction():
            for name, value in parsed.items():
                previous = self.db.execute(
                    "SELECT value,note,updated_at FROM entries "
                    "WHERE date=? AND metric=? AND source=?",
                    (date, name, source),
                ).fetchone()
                if previous and previous["updated_at"] > stamp:
                    ignored_stale.append(name)
                    continue
                if previous and previous["value"] == canonical(value) and previous["note"] == note:
                    self.db.execute(
                        "UPDATE entries SET updated_at=? WHERE date=? AND metric=? AND source=?",
                        (stamp, date, name, source),
                    )
                    continue
                self.db.execute(
                    "INSERT INTO entries VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(date,metric,source) DO UPDATE SET value=excluded.value,"
                    "note=excluded.note,updated_at=excluded.updated_at",
                    (date, name, canonical(value), METRICS[name][0], source, note, stamp),
                )
                changed += 1
        return {
            "date": date,
            "metrics": parsed,
            "changed": changed,
            "source": source,
            "ignored_stale": ignored_stale,
        }

    def set_day(
        self, date: str, status: str, city_code: str = "", updated_at: str | None = None
    ) -> dict:
        day(date)
        if status not in {"home", "trip", "transit"}:
            raise LogError("INVALID_STATUS", "Day status must be home, trip or transit")
        bounded_text(city_code, "city_code", 100)
        stamp = updated_at or utc_now()
        with self.transaction():
            self.db.execute(
                "INSERT INTO days VALUES(?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
                "status=excluded.status,city_code=excluded.city_code,"
                "updated_at=excluded.updated_at WHERE excluded.updated_at>=days.updated_at",
                (date, status, city_code, stamp),
            )
        return dict(self.db.execute("SELECT * FROM days WHERE date=?", (date,)).fetchone())

    def apply_event(self, event: dict) -> dict:
        """Commit one browser event and its receipt together; replay never duplicates writes."""
        if not isinstance(event, dict) or set(event) != {"id", "created_at", "action", "payload"}:
            raise LogError("INVALID_EVENT", "Event requires id, created_at, action and payload")
        if not isinstance(event["id"], str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{8,100}", event["id"]
        ):
            raise LogError("INVALID_EVENT", "Invalid event identifier")
        if len(canonical(event)) > 32000:
            raise LogError("INVALID_EVENT", "Event exceeds 32 KB")
        stamp = datetime.fromisoformat(event["created_at"])
        if stamp.tzinfo is None or stamp > datetime.now(UTC) + timedelta(minutes=10):
            raise LogError(
                "INVALID_EVENT", "Event time must have an offset and cannot be in the future"
            )
        stamp = stamp.astimezone(UTC).isoformat(timespec="microseconds")
        digest = hashlib.sha256(canonical(event).encode()).hexdigest()
        with self.transaction():
            old = self.db.execute("SELECT * FROM events WHERE id=?", (event["id"],)).fetchone()
            if old:
                if old["digest"] != digest:
                    raise LogError(
                        "EVENT_CONFLICT", "Event ID was already used for another payload"
                    )
                return {**json.loads(old["result_json"]), "replayed": True}
            payload = event["payload"]
            if event["action"] == "log":
                if set(payload) - {"date", "metrics", "status", "city_code", "note"}:
                    raise LogError("INVALID_EVENT", "Unknown log event fields")
                result = self.log(
                    payload["date"], payload["metrics"], "manual", payload.get("note", ""), stamp
                )
                if payload.get("status"):
                    self.set_day(
                        payload["date"], payload["status"], payload.get("city_code", ""), stamp
                    )
            elif event["action"] == "habit":
                if set(payload) != {"name", "date", "done"}:
                    raise LogError("INVALID_EVENT", "Invalid habit event fields")
                result = self.check_habit(**payload)
            else:
                raise LogError("INVALID_EVENT", "Unknown event action")
            result = {"id": event["id"], "result": result, "replayed": False}
            self.db.execute(
                "INSERT INTO events VALUES(?,?,?,?)",
                (event["id"], digest, canonical(result), utc_now()),
            )
        return result

    def records(self, start: str | None = None, end: str | None = None) -> list[dict]:
        conditions, params = [], []
        for operator, value in [(">=", start), ("<=", end)]:
            if value:
                conditions.append("date" + operator + "?")
                params.append(day(value))
        sql = "SELECT * FROM entries" + (" WHERE " + " AND ".join(conditions) if conditions else "")
        result = []
        for row in self.db.execute(sql + " ORDER BY date,metric,source", params):
            item = dict(row)
            item["value"] = json.loads(item["value"])
            result.append(item)
        return result

    def daily(self, start: str, end: str) -> list[dict]:
        """Select one source per day/metric, preferring manual records over imports."""
        grouped = {}
        preferred = "apple:" + self.config["preferred_apple_source"]

        def rank(row):
            return (
                3
                if row["source"] == "manual"
                else 2
                if not row["source"].startswith("apple:")
                else 1
                if row["source"] == preferred
                else 0,
                row["updated_at"] if not row["source"].startswith("apple:") else "",
                row["value"] if isinstance(row["value"], (int, float)) else 0,
                row["source"],
            )

        for record in self.records(start, end):
            key = record["date"], record["metric"]
            if key not in grouped or rank(record) > rank(grouped[key]):
                grouped[key] = record
        dates = {}
        for (date_text, metric), record in grouped.items():
            item = dates.setdefault(date_text, {"date": date_text, "metrics": {}, "sources": {}})
            item["metrics"][metric] = record["value"]
            item["sources"][metric] = record["source"]
        contexts = {
            row["date"]: dict(row)
            for row in self.db.execute("SELECT * FROM days WHERE date>=? AND date<=?", (start, end))
        }
        for date_text, item in dates.items():
            item.update(
                status=contexts.get(date_text, {}).get("status", "unknown"),
                city_code=contexts.get(date_text, {}).get("city_code", ""),
            )
        return [dates[key] for key in sorted(dates)]

    def add_habit(self, name: str, rule: dict | None = None, start: str | None = None) -> dict:
        bounded_text(name, "habit name", 100)
        if not name.strip():
            raise LogError("INVALID_HABIT", "Habit name cannot be empty")
        rule = rule or {"type": "manual"}
        if rule != {"type": "manual"}:
            if set(rule) != {"metric", "op", "target"} or rule["op"] not in {
                "gte",
                "lte",
                "eq",
                "before",
            }:
                raise LogError(
                    "INVALID_RULE", "Use manual or metric/op/target with gte,lte,eq,before"
                )
            if rule["metric"] == "exercise_type":
                raise LogError("INVALID_RULE", "Text labels cannot be numeric habit targets")
            metrics({rule["metric"]: rule["target"]})
            if (rule["op"] == "before") != (rule["metric"] in {"bedtime", "last_meal"}):
                raise LogError(
                    "INVALID_RULE", "Clock metrics use before; numeric metrics use gte/lte/eq"
                )
        start = day(start or today(self.config))
        with self.transaction():
            old = self.db.execute("SELECT * FROM habits WHERE name=?", (name,)).fetchone()
            if old and old["rule_json"] != canonical(rule):
                raise LogError("HABIT_EXISTS", "Existing habit has another rule; use another name")
            self.db.execute(
                "INSERT OR IGNORE INTO habits(name,rule_json,active,created_date) VALUES(?,?,1,?)",
                (name, canonical(rule), start),
            )
        return dict(self.db.execute("SELECT * FROM habits WHERE name=?", (name,)).fetchone())

    def check_habit(self, name: str, date: str, done: bool = True) -> dict:
        day(date)
        if type(done) is not bool:
            raise LogError("INVALID_CHECKIN", "done must be boolean")
        habit = self.db.execute("SELECT * FROM habits WHERE name=?", (name,)).fetchone()
        if not habit:
            raise LogError("NOT_FOUND", "Habit not found", 3)
        if date < habit["created_date"]:
            raise LogError("INVALID_DATE", "Check-in predates the habit")
        with self.transaction():
            self.db.execute(
                "INSERT INTO checkins VALUES(?,?,?) ON CONFLICT(habit_id,date) "
                "DO UPDATE SET done=excluded.done",
                (habit["id"], date, int(done)),
            )
        return {"name": name, "date": date, "done": done}

    def habit_status(self, days: int = 30, as_of: str | None = None) -> list[dict]:
        end = date.fromisoformat(day(as_of or today(self.config)))
        if not 1 <= days <= 366:
            raise LogError("INVALID_DAYS", "days must be from 1 to 366")
        start = end - timedelta(days=days - 1)
        rows = {
            row["date"]: row["metrics"] for row in self.daily(start.isoformat(), end.isoformat())
        }
        result = []
        for habit in self.db.execute("SELECT * FROM habits WHERE active=1 ORDER BY id"):
            rule = json.loads(habit["rule_json"])
            checks = {
                row["date"]: bool(row["done"])
                for row in self.db.execute(
                    "SELECT * FROM checkins WHERE habit_id=?", (habit["id"],)
                )
            }
            beginning = max(start, date.fromisoformat(habit["created_date"]))
            flags = []
            current = beginning
            while current <= end:
                key = current.isoformat()
                value = rows.get(key, {}).get(rule.get("metric"))
                done = False
                if key in checks:
                    done = checks[key]
                elif value is not None and rule.get("op"):
                    target = rule["target"]
                    if rule["op"] == "before":

                        def minutes(text):
                            h, m = map(int, text.split(":"))
                            return (
                                h * 60 + m + (1440 if rule["metric"] == "bedtime" and h < 12 else 0)
                            )

                        done = minutes(value) <= minutes(target)
                    else:
                        number = float(target)
                        done = {
                            "gte": value >= number,
                            "lte": value <= number,
                            "eq": value == number,
                        }[rule["op"]]
                flags.append(done)
                current += timedelta(days=1)
            streak = 0
            for done in reversed(flags):
                if not done:
                    break
                streak += 1
            result.append(
                {
                    "id": habit["id"],
                    "name": habit["name"],
                    "rule": rule,
                    "eligible_days": len(flags),
                    "completed_days": sum(flags),
                    "completion_rate": round(sum(flags) / len(flags), 4) if flags else None,
                    "streak_days_in_window": streak,
                    "done_today": flags[-1] if flags else False,
                }
            )
        return result

    def add_med(self, name: str, schedule: list[str], start: str, end: str | None = None) -> dict:
        bounded_text(name, "medication label", 100)
        if not name.strip() or not isinstance(schedule, list) or not 1 <= len(schedule) <= 12:
            raise LogError("INVALID_SCHEDULE", "Supply a name and 1 to 12 daily HH:MM times")
        times = sorted({clock(value) for value in schedule})
        day(start)
        if end and day(end) < start:
            raise LogError("INVALID_DATE", "Medication end predates its start")
        with self.transaction():
            self.db.execute(
                "INSERT INTO meds(name,schedule,start,end) VALUES(?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET schedule=excluded.schedule,"
                "start=excluded.start,end=excluded.end",
                (name, canonical(times), start, end),
            )
        return dict(self.db.execute("SELECT * FROM meds WHERE name=?", (name,)).fetchone())

    def take_med(self, name: str, date: str, time: str) -> dict:
        day(date)
        clock(time)
        med = self.db.execute("SELECT * FROM meds WHERE name=?", (name,)).fetchone()
        if not med:
            raise LogError("NOT_FOUND", "Medication schedule not found", 3)
        if (
            time not in json.loads(med["schedule"])
            or date < med["start"]
            or (med["end"] and date > med["end"])
        ):
            raise LogError("INVALID_DOSE", "Check-in does not match the recorded schedule")
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO doses VALUES(?,?,?)", (med["id"], date, time))
        return {"name": name, "date": date, "time": time, "recorded": True}

    def add_reminder(self, title: str, due_date: str) -> dict:
        bounded_text(title, "reminder title", 200)
        if not title.strip():
            raise LogError("INVALID_REMINDER", "Title cannot be empty")
        day(due_date)
        with self.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO reminders(title,due_date) VALUES(?,?)", (title, due_date)
            )
        return dict(
            self.db.execute(
                "SELECT * FROM reminders WHERE title=? AND due_date=?", (title, due_date)
            ).fetchone()
        )

    def complete_reminder(self, identifier: int) -> dict:
        with self.transaction():
            changed = self.db.execute(
                "UPDATE reminders SET completed=1 WHERE id=?", (identifier,)
            ).rowcount
            if not changed:
                raise LogError("NOT_FOUND", "Reminder not found", 3)
        return {"id": identifier, "completed": True}

    def due(self, as_of: str | None = None, at: str | None = None) -> dict:
        current = day(as_of or today(self.config))
        cutoff = clock(at or "23:59")
        medication = []
        for med in self.db.execute(
            "SELECT * FROM meds WHERE start<=? AND (end IS NULL OR end>=?)", (current, current)
        ):
            for time in json.loads(med["schedule"]):
                taken = bool(
                    self.db.execute(
                        "SELECT 1 FROM doses WHERE med_id=? AND date=? AND time=?",
                        (med["id"], current, time),
                    ).fetchone()
                )
                if time <= cutoff and not taken:
                    medication.append({"name": med["name"], "date": current, "time": time})
        reminders = [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM reminders WHERE completed=0 AND due_date<=? ORDER BY due_date,id",
                (current,),
            )
        ]
        return {
            "date": current,
            "through_time": cutoff,
            "medications": medication,
            "reminders": reminders,
            "notice": "Recorded plans only. Never change doses based on this tool.",
        }
