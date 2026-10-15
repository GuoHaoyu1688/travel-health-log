"""Local CSV/JSON exchange, trip context import, portable backup and restore."""

import csv
import hashlib
import io
import json
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .common import METRICS, LogError, canonical, day, utc_now

TABLES = [
    "entries",
    "days",
    "habits",
    "checkins",
    "meds",
    "doses",
    "reminders",
    "events",
    "apple_samples",
    "metadata",
]


class InterchangeMixin:
    def protect_output(self, target: Path) -> None:
        """Reject database destinations, including aliases and SQLite sidecars."""
        database = self.root / "health.db"
        protected = [
            database,
            *[Path(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal")],
        ]
        if any(
            target.resolve() == path.resolve()
            or (target.exists() and path.exists() and target.samefile(path))
            for path in protected
        ):
            raise LogError(
                "PROTECTED_PATH", "Outputs cannot replace the live database or its sidecar files"
            )

    def export(
        self,
        format: str,
        out: str,
        kind: str = "archive",
        overwrite: bool = False,
        bom: bool = True,
    ) -> dict:
        if format not in {"csv", "json"} or kind not in {"archive", "entries"}:
            raise LogError("INVALID_FORMAT", "Use csv/json with archive/entries")
        target = Path(out).expanduser()
        self.protect_output(target)
        if target.exists() and not overwrite:
            raise LogError("EXISTS", "Output exists; --yes is required to replace it")
        if kind == "archive":
            tables = {
                name: [dict(row) for row in self.db.execute(f"SELECT * FROM {name}")]
                for name in TABLES
            }
            payload = {"schema_version": 1, "timezone": self.config["timezone"], "tables": tables}
            rows = [
                {
                    "entity": "meta",
                    "payload": canonical(
                        {"schema_version": 1, "timezone": self.config["timezone"]}
                    ),
                }
            ]
            rows += [
                {"entity": name, "payload": canonical(row)}
                for name, records in tables.items()
                for row in records
            ]
        else:
            rows = self.records()
            payload = {"schema_version": 1, "entries": rows}
        if format == "json":
            content = json.dumps(payload, ensure_ascii=False, indent=2)
        else:
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(
                stream,
                fieldnames=list(rows[0])
                if rows
                else ["date", "metric", "value", "unit", "source", "note", "updated_at"],
            )
            writer.writeheader()
            writer.writerows(rows)
            content = stream.getvalue()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open(
            "w" if overwrite else "x",
            encoding="utf-8-sig" if bom and format == "csv" else "utf-8",
            newline="",
        ) as file:
            file.write(content)
        if self.config["private_permissions"]:
            target.chmod(0o600)
        return {"path": str(target.absolute()), "kind": kind, "format": format, "rows": len(rows)}

    def import_file(self, path: str) -> dict:
        source = Path(path)
        if source.stat().st_size > 100 * 1024 * 1024:
            raise LogError("IMPORT_TOO_LARGE", "CSV/JSON input exceeds 100 MiB")
        if source.suffix.lower() == ".json":
            payload = json.loads(source.read_text("utf-8-sig"))
            if isinstance(payload, dict) and "tables" in payload:
                return self.restore(payload)
            rows = payload.get("entries") if isinstance(payload, dict) else payload
        else:
            with source.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            if rows and set(rows[0]) == {"entity", "payload"}:
                payload = {"tables": {name: [] for name in TABLES}}
                for row in rows:
                    if row["entity"] == "meta":
                        payload.update(json.loads(row["payload"]))
                    elif row["entity"] in TABLES:
                        payload["tables"][row["entity"]].append(json.loads(row["payload"]))
                    else:
                        raise LogError("INVALID_ARCHIVE", "Unknown archive entity")
                return self.restore(payload)
        if not isinstance(rows, list) or len(rows) > 100000:
            raise LogError("INVALID_IMPORT", "Expected up to 100000 records")
        changed = 0
        with self.transaction():
            for row in rows:
                if not isinstance(row, dict):
                    raise LogError("INVALID_IMPORT", "Each row must be an object")
                if "metric" in row:
                    if (
                        row["metric"] not in METRICS
                        or row.get("unit", METRICS[row["metric"]][0]) != METRICS[row["metric"]][0]
                    ):
                        raise LogError(
                            "INVALID_UNIT", "Metric or unit does not match the data dictionary"
                        )
                    values = {row["metric"]: row["value"]}
                    record_source = row.get("source") or "csv"
                else:
                    allowed = set(METRICS) | {"date", "bp", "status", "city_code", "note", "source"}
                    if set(row) - allowed:
                        raise LogError("UNKNOWN_FIELD", "Unknown CSV/JSON columns")
                    values = {
                        key: value
                        for key, value in row.items()
                        if key in set(METRICS) | {"bp"} and value not in {"", None}
                    }
                    record_source = row.get("source") or "csv"
                if record_source.startswith("apple:"):
                    # Flat exports carry daily summaries, not raw Apple samples.
                    record_source = "csv:" + record_source
                changed += self.log(row["date"], values, record_source, row.get("note") or "")[
                    "changed"
                ]
                if row.get("status"):
                    self.set_day(row["date"], row["status"], row.get("city_code") or "")
        return {"rows": len(rows), "changed_metrics": changed}

    def import_trips(self, path: str) -> dict:
        source = Path(path)
        if source.stat().st_size > 20 * 1024 * 1024:
            raise LogError("IMPORT_TOO_LARGE", "Trip CSV exceeds 20 MiB")
        with source.open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        if rows and set(rows[0]) == {"entity", "payload"}:
            rows = [json.loads(row["payload"]) for row in rows if row["entity"] == "trips"]
        days = set()
        with self.transaction():
            for row in rows:
                start = datetime.fromisoformat(row["start_at"])
                end = datetime.fromisoformat(row["end_at"])
                zone = ZoneInfo(self.config["timezone"])
                start = (
                    start.replace(tzinfo=zone) if start.tzinfo is None else start.astimezone(zone)
                )
                end = end.replace(tzinfo=zone) if end.tzinfo is None else end.astimezone(zone)
                if end <= start or end - start > timedelta(days=366):
                    raise LogError(
                        "INVALID_TRIP", "Trip duration must be positive and at most 366 days"
                    )
                current = start.date()
                while current <= end.date():
                    status = "transit" if current in {start.date(), end.date()} else "trip"
                    self.set_day(current.isoformat(), status, row["destination"])
                    days.add(current.isoformat())
                    current += timedelta(days=1)
        return {
            "trips": len(rows),
            "marked_days": len(days),
            "rule": "Departure and return dates are transit; intermediate dates are trip.",
        }

    def restore(self, payload: dict) -> dict:
        if payload.get("schema_version") != 1 or set(payload.get("tables", {})) != set(TABLES):
            raise LogError("INVALID_ARCHIVE", "Invalid native archive schema")
        if payload.get("timezone") != self.config["timezone"]:
            raise LogError("TIMEZONE_MISMATCH", "Restore with the archive calendar timezone")
        digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
        old = self.db.execute("SELECT value FROM metadata WHERE key='last_import'").fetchone()
        if old and old[0] == digest:
            return {"reused": True}
        if any(
            self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
            if table != "metadata"
        ):
            raise LogError("NONEMPTY_LIBRARY", "Native restore requires an empty library")
        with self.transaction():
            for table in TABLES:
                columns = [row[1] for row in self.db.execute(f"PRAGMA table_info({table})")]
                rows = payload["tables"][table]
                if not isinstance(rows, list) or len(rows) > 2000000:
                    raise LogError("INVALID_ARCHIVE", "Invalid table row count")
                for row in rows:
                    if not isinstance(row, dict) or set(row) != set(columns):
                        raise LogError("INVALID_ARCHIVE", f"Invalid columns for {table}")
                    if table == "metadata":
                        if row["key"] == "timezone" and row["value"] != self.config["timezone"]:
                            raise LogError("TIMEZONE_MISMATCH", "Inconsistent archive timezone")
                        continue
                    self.db.execute(
                        f"INSERT INTO {table} ({','.join(columns)}) "
                        f"VALUES({','.join('?' for _ in columns)})",
                        [row[key] for key in columns],
                    )
            if self.db.execute("PRAGMA foreign_key_check").fetchone():
                raise LogError("INVALID_ARCHIVE", "Broken archive references")
            # Re-run public validators while preserving original timestamps and identities.
            from .common import metrics

            for row in self.records():
                day(row["date"])
                metrics({row["metric"]: row["value"]})
                if row["unit"] != METRICS[row["metric"]][0]:
                    raise LogError("INVALID_ARCHIVE", "Invalid archived units")
            for row in self.db.execute("SELECT * FROM days").fetchall():
                self.set_day(row["date"], row["status"], row["city_code"], row["updated_at"])
            for row in self.db.execute("SELECT * FROM habits").fetchall():
                self.add_habit(row["name"], json.loads(row["rule_json"]), row["created_date"])
            for row in self.db.execute("SELECT * FROM meds").fetchall():
                self.add_med(row["name"], json.loads(row["schedule"]), row["start"], row["end"])
            for row in self.db.execute("SELECT * FROM reminders").fetchall():
                self.add_reminder(row["title"], row["due_date"])
            for row in self.db.execute("SELECT date FROM checkins UNION SELECT date FROM doses"):
                day(row[0])
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", ("last_import", digest))
        return {"reused": False, "entries": len(self.records())}

    def backup(self, out: str | None = None) -> dict:
        stamp = utc_now().replace(":", "").replace("+", "_")
        target = Path(out) if out else self.root / "backups" / f"health-{stamp}.zip"
        if target.exists():
            raise LogError("EXISTS", "Backup destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            with sqlite3.connect(temporary / "health.db") as database:
                self.db.backup(database)
            self.export("json", str(temporary / "archive.json"))
            with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as archive:
                archive.write(temporary / "health.db", "health.db")
                archive.write(temporary / "archive.json", "archive.json")
                archive.writestr("config.yaml", yaml.safe_dump(self.config))
        if self.config["private_permissions"]:
            target.chmod(0o600)
        return {"path": str(target.absolute()), "encrypted": False}
