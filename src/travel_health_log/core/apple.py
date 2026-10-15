"""Bounded XML parsing of user-exported Apple Health step, sleep and weight records."""

import hashlib
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from defusedxml.ElementTree import iterparse

from .common import LogError, canonical

TYPES = {
    "HKQuantityTypeIdentifierStepCount": "steps",
    "HKQuantityTypeIdentifierBodyMass": "weight",
    "HKCategoryTypeIdentifierSleepAnalysis": "sleep",
}
ASLEEP = {
    "HKCategoryValueSleepAnalysisAsleep",
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
    "HKCategoryValueSleepAnalysisAsleepCore",
    "HKCategoryValueSleepAnalysisAsleepDeep",
    "HKCategoryValueSleepAnalysisAsleepREM",
    "1",
    "3",
    "4",
    "5",
}


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise LogError("APPLE_TIMEZONE", "Apple samples require timestamps with UTC offsets")
    return result.astimezone(UTC)


def import_apple(store, path: str, progress=None) -> dict:
    source = Path(path)
    if source.stat().st_size > 2 * 1024**3:
        raise LogError("IMPORT_TOO_LARGE", "XML exceeds the 2 GiB input limit")
    inserted = ignored = duplicates = processed = 0
    # One transaction rolls back every new sample when a malformed supported record is encountered.
    with store.transaction(), source.open("rb") as stream:
        context = iterparse(
            stream, events=("start", "end"), forbid_entities=True, forbid_external=True
        )
        root = None
        for event, element in context:
            if root is None:
                root = element
                if root.tag != "HealthData":
                    raise LogError("INVALID_APPLE_XML", "Expected a HealthData root element")
            if event != "end":
                continue
            if element.tag == "Record":
                processed += 1
                fields = element.attrib
                metric = TYPES.get(fields.get("type"))
                if not metric or (metric == "sleep" and fields.get("value") not in ASLEEP):
                    ignored += 1
                else:
                    start, end = parse_time(fields["startDate"]), parse_time(fields["endDate"])
                    if end < start or end - start > timedelta(days=7):
                        raise LogError(
                            "INVALID_APPLE_RECORD", "Sample interval is invalid or exceeds 7 days"
                        )
                    value = 0.0 if metric == "sleep" else float(fields["value"])
                    if metric == "steps":
                        if (
                            fields.get("unit") != "count"
                            or not 0 <= value <= 200000
                            or not value.is_integer()
                        ):
                            raise LogError(
                                "INVALID_APPLE_RECORD", "Step samples require bounded whole counts"
                            )
                    elif metric == "weight":
                        unit = fields.get("unit")
                        if unit in {"lb", "lbs"}:
                            value *= 0.45359237
                        elif unit != "kg":
                            raise LogError("INVALID_APPLE_RECORD", "Weight unit must be kg or lb")
                        if not 1 <= value <= 600:
                            raise LogError(
                                "INVALID_APPLE_RECORD", "Weight sample is outside supported bounds"
                            )
                    source_name = fields.get("sourceName", "Unknown device")
                    if not 1 <= len(source_name) <= 180:
                        raise LogError("INVALID_APPLE_RECORD", "Invalid source name")
                    normalized = [metric, start.isoformat(), end.isoformat(), value, source_name]
                    identity = hashlib.sha256(canonical(normalized).encode()).hexdigest()
                    cursor = store.db.execute(
                        "INSERT OR IGNORE INTO apple_samples VALUES(?,?,?,?,?,?)",
                        [identity, *normalized],
                    )
                    inserted += cursor.rowcount
                    duplicates += 1 - cursor.rowcount
                if progress and processed % 10000 == 0:
                    progress(processed)
                element.clear()
                root.clear()
            elif element is not root:
                element.clear()
                root.clear()
        rebuild_apple(store)
    overlaps = 0
    previous = {}
    for record in store.db.execute(
        "SELECT source,start_at,end_at FROM apple_samples "
        "WHERE metric='steps' ORDER BY source,start_at,end_at"
    ):
        source_name = record["source"]
        if source_name in previous and record["start_at"] < previous[source_name]:
            overlaps += 1
        previous[source_name] = max(previous.get(source_name, ""), record["end_at"])
    warnings = (
        [
            f"{overlaps} overlapping step intervals within one source; "
            "review device exports before interpreting daily totals."
        ]
        if overlaps
        else []
    )
    return {
        "warnings": warnings,
        "processed": processed,
        "inserted": inserted,
        "duplicates": duplicates,
        "ignored": ignored,
        "timezone": store.config["timezone"],
        "source_policy": "Manual and imported daily values first; preferred Apple source next, "
        "otherwise maximum "
        "daily device value; devices are never summed together.",
    }


def rebuild_apple(store):
    """Recompute daily imported values without changing manual records."""
    zone = ZoneInfo(store.config["timezone"])
    daily = defaultdict(float)
    weights = {}
    sleep = defaultdict(list)
    for record in store.db.execute("SELECT * FROM apple_samples ORDER BY source,start_at,end_at"):
        start, end = parse_time(record["start_at"]), parse_time(record["end_at"])
        source = "apple:" + record["source"]
        if record["metric"] == "weight":
            key = (end.astimezone(zone).date().isoformat(), "weight", source)
            if key not in weights or weights[key][0] <= end:
                weights[key] = (end, record["value"])
        elif record["metric"] == "steps":
            if end == start:
                daily[(end.astimezone(zone).date().isoformat(), "steps", source)] += record["value"]
                continue
            total_seconds = (end - start).total_seconds()
            current, remaining = start, int(record["value"])
            while current < end:
                local = current.astimezone(zone)
                midnight = datetime.combine(
                    local.date() + timedelta(days=1), datetime.min.time(), zone
                ).astimezone(UTC)
                stop = min(midnight, end)
                allocated = (
                    remaining
                    if stop == end
                    else int(record["value"] * (stop - current).total_seconds() / total_seconds)
                )
                daily[(local.date().isoformat(), "steps", source)] += allocated
                remaining -= allocated
                current = stop
        else:
            sleep[source].append((start, end))
    for key, (_, value) in weights.items():
        daily[key] = round(value, 6)
    for source, intervals in sleep.items():
        merged = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        session_end, seconds = None, 0
        for start, end in merged:
            if session_end and start - session_end > timedelta(minutes=90):
                daily[(session_end.astimezone(zone).date().isoformat(), "sleep", source)] += (
                    seconds / 3600
                )
                seconds = 0
            seconds += (end - start).total_seconds()
            session_end = end
        if session_end:
            daily[(session_end.astimezone(zone).date().isoformat(), "sleep", source)] += (
                seconds / 3600
            )
    store.db.execute("DELETE FROM entries WHERE source LIKE 'apple:%'")
    for (date_text, metric, source), value in sorted(daily.items()):
        store.log(
            date_text,
            {metric: round(value, 6)},
            source,
            "Apple export: steps split across local midnight; sleep assigned to session wake date.",
        )
