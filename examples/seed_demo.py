"""Populate a local demo using only original fictional journal records."""

from pathlib import Path

from travel_health_log.core.common import settings
from travel_health_log.core.store import Store

root, config = settings()
store = Store(root, config)
try:
    print(store.import_file(str(Path(__file__).parent / "90-days.csv")))
    store.add_habit(
        "Example step goal", {"metric": "steps", "op": "gte", "target": 8000}, "2026-07-18"
    )
    store.add_habit(
        "Earlier bedtime", {"metric": "bedtime", "op": "before", "target": "23:30"}, "2026-07-18"
    )
    store.add_habit("Room movement", None, "2026-07-18")
    store.check_habit("Room movement", "2026-10-15")
    store.add_med("Example recorded plan", ["08:00", "20:00"], "2026-10-15", "2026-10-16")
    store.add_reminder("Example scheduled follow-up", "2026-10-20")
finally:
    store.close()
