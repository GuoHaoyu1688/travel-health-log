"""Generate 90 original fictional days with exactly calculable context averages."""

import csv
import json
from datetime import date, timedelta
from pathlib import Path


def generate(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    start = date(2026, 7, 18)
    for index in range(90):
        context = ["home", "trip", "transit"][index % 3]
        sleep, steps, alcohol = {
            "home": (8, 9000, 0),
            "trip": (6, 6000, 1),
            "transit": (5, 3000, 0),
        }[context]
        rows.append(
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "sleep": sleep,
                "sleep_quality": 4 if context == "home" else 3,
                "steps": steps,
                "weight": 72 + (index % 7) / 10,
                "bp": "118/76",
                "heart_rate": 68,
                "water": 1.8,
                "alcohol": alcohol,
                "coffee": 1,
                "exercise": 15,
                "exercise_type": "Example walking",
                "mood": 4,
                "meals": 3,
                "bedtime": "23:00" if context == "home" else "00:30",
                "last_meal": "19:00" if context == "home" else "21:30",
                "status": context,
                "city_code": "C00" if context == "home" else "C01",
                "note": "Fictional generated record",
                "source": "demo",
            }
        )
    with (directory / "90-days.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    expected = {
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "days": 90,
        "contexts": {
            "home": {"n": 30, "sleep": 8, "steps": 9000, "alcohol": 0},
            "trip": {"n": 30, "sleep": 6, "steps": 6000, "alcohol": 1},
            "transit": {"n": 30, "sleep": 5, "steps": 3000, "alcohol": 0},
        },
        "trip_minus_home": {"sleep": -2, "steps": -3000, "alcohol": 1},
    }
    (directory / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")


if __name__ == "__main__":
    generate(Path(__file__).parent)
