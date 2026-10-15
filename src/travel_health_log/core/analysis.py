"""Descriptive comparisons with explicit sample sizes and missing-data handling."""

from datetime import date, timedelta
from statistics import mean

from .common import DISCLAIMER, LogError, bp_hint, day, today


class AnalysisMixin:
    def window(self, days: int, as_of: str | None = None):
        if type(days) is not int or not 1 <= days <= 366:
            raise LogError("INVALID_DAYS", "days must be an integer from 1 to 366")
        end = date.fromisoformat(day(as_of or today(self.config)))
        return (end - timedelta(days=days - 1)).isoformat(), end.isoformat()

    def compare(self, days: int = 30, as_of: str | None = None) -> dict:
        start, end = self.window(days, as_of)
        rows = self.daily(start, end)
        groups = {}
        for status in ["home", "trip", "transit", "unknown"]:
            selected = [row for row in rows if row["status"] == status]
            summary = {"recorded_days": len(selected), "metrics": {}}
            for metric in ["sleep", "steps", "alcohol", "weight", "exercise", "water", "coffee"]:
                values = [row["metrics"][metric] for row in selected if metric in row["metrics"]]
                summary["metrics"][metric] = {
                    "n": len(values),
                    "mean": round(mean(values), 4) if values else None,
                }
            groups[status] = summary
        differences = {}
        for metric in ["sleep", "steps", "alcohol"]:
            home, trip = groups["home"]["metrics"][metric], groups["trip"]["metrics"][metric]
            differences[metric] = {
                "trip_minus_home": round(trip["mean"] - home["mean"], 4)
                if home["n"] and trip["n"]
                else None,
                "home_n": home["n"],
                "trip_n": trip["n"],
            }
        return {
            "start": start,
            "end": end,
            "groups": groups,
            "differences": differences,
            "interpretation": "Observed associations, not causation. Missing values are not zeros; "
            "unmarked days are unknown, not home.",
            "disclaimer": DISCLAIMER,
        }

    def stats(self, days: int = 30, as_of: str | None = None) -> dict:
        start, end = self.window(days, as_of)
        rows = self.daily(start, end)
        # Display bounds must not change an observation's seven-day mean.
        lookback = max(date(1900, 1, 1), date.fromisoformat(start) - timedelta(days=6)).isoformat()
        weight_rows = self.daily(lookback, end)
        rolling, pressure = [], []
        for row in rows:
            date_text = row["date"]
            earliest = (date.fromisoformat(date_text) - timedelta(days=6)).isoformat()
            values = [
                item["metrics"]["weight"]
                for item in weight_rows
                if earliest <= item["date"] <= date_text and "weight" in item["metrics"]
            ]
            if "weight" in row["metrics"]:
                rolling.append(
                    {"date": date_text, "value": round(mean(values), 4), "n": len(values)}
                )
            if {"systolic", "diastolic"} <= set(row["metrics"]):
                pressure.append(
                    {
                        "date": date_text,
                        **bp_hint(row["metrics"]["systolic"], row["metrics"]["diastolic"]),
                    }
                )
        # The 21:00 split is a configurable-style journal grouping, not a health threshold.
        associations = {}
        for label, predicate in [
            ("late_meal", lambda m: m.get("last_meal", "") >= "21:00"),
            ("earlier_meal", lambda m: m.get("last_meal", "") < "21:00"),
        ]:
            values = [
                row["metrics"]["sleep"]
                for row in rows
                if "last_meal" in row["metrics"]
                and "sleep" in row["metrics"]
                and predicate(row["metrics"])
            ]
            associations[label] = {
                "n": len(values),
                "mean_same_date_sleep": round(mean(values), 4) if values else None,
            }
        return {
            "start": start,
            "end": end,
            "requested_days": days,
            "recorded_days": len(rows),
            "daily": rows,
            "weight_7_calendar_day_mean": rolling,
            "blood_pressure": pressure,
            "meal_sleep_association": associations,
            "association_note": "Same-date journal grouping at 21:00, not a clinical threshold "
            "or evidence that meal timing changed sleep.",
            "habits": self.habit_status(days, end),
            "disclaimer": DISCLAIMER,
        }

    def report(self, week: str) -> dict:
        try:
            year_text, week_text = week.split("-W")
            if len(year_text) != 4 or len(week_text) != 2:
                raise ValueError
            start = date.fromisocalendar(int(year_text), int(week_text), 1)
        except (ValueError, TypeError):
            raise LogError("INVALID_WEEK", "Use an ISO week such as 2026-W42") from None
        end = start + timedelta(days=6)
        stats = self.stats(7, end.isoformat())
        comparison = self.compare(7, end.isoformat())
        lines = [
            f"# Travel health journal — {week}",
            "",
            f"{start} to {end}",
            "",
            DISCLAIMER,
            "",
            f"Recorded days: {stats['recorded_days']}/7. "
            "Missing days are not assumed healthy or unhealthy.",
            "",
            "## Habits",
            "",
            "| Habit | Completed | Rate | Streak in this week |",
            "| --- | ---: | ---: | ---: |",
        ]
        for habit in stats["habits"]:
            name = habit["name"].replace("|", "\\|").replace("\n", " ")
            rate = (
                f"{habit['completion_rate']:.0%}"
                if habit["completion_rate"] is not None
                else "No eligible days"
            )
            lines.append(
                f"| {name} | {habit['completed_days']}/{habit['eligible_days']} | "
                f"{rate} | {habit['streak_days_in_window']} |"
            )
        lines += [
            "",
            "## Home and travel observations",
            "",
            "| Context | Sleep hours (n) | Steps (n) | Alcohol drinks (n) |",
            "| --- | ---: | ---: | ---: |",
        ]
        for status, summary in comparison["groups"].items():
            values = []
            for metric in ["sleep", "steps", "alcohol"]:
                result = summary["metrics"][metric]
                values.append(
                    f"{result['mean']} ({result['n']})" if result["n"] else "No observations"
                )
            lines.append("| " + " | ".join([status, *values]) + " |")
        lines += [
            "",
            comparison["interpretation"],
            "",
            "## Next week",
            "",
            "- Use the same units and record context alongside each day.",
            "- Review the reminder list against your own recorded schedule.",
            "- Choose a manageable existing habit; note travel situations that affect recording.",
        ]
        if stats["recorded_days"] < 7:
            lines.append(
                "- Leave unavailable measurements blank; improve coverage before comparing weeks."
            )
        if stats["blood_pressure"]:
            lines += [
                "",
                "## Blood pressure category references",
                "",
                "Adult range labels are not a diagnosis. "
                "A clinician interprets repeated measurements and context.",
            ]
            for item in stats["blood_pressure"]:
                lines.append(f"- {item['date']}: {item['category']}. {item['message']}")
            lines += [
                "",
                "[American Heart Association reference](https://www.heart.org/en/health-topics/high-blood-pressure/blood-pressure-explained).",
            ]
        return {
            "week": week,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "markdown": "\n".join(lines) + "\n",
        }
