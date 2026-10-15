"""Generate fictional step XML and measure streaming import, without network access."""

import argparse
import json
import time
import tracemalloc
from datetime import UTC, datetime, timedelta

from travel_health_log.core.apple import import_apple
from travel_health_log.core.common import settings
from travel_health_log.core.store import Store

parser = argparse.ArgumentParser()
parser.add_argument("--records", type=int, default=10000)
parser.add_argument("--data-dir", default="data/import-benchmark")
args = parser.parse_args()
root, config = settings(args.data_dir)
root.mkdir(parents=True, exist_ok=True)
path = root / "fictional-export.xml"
start = datetime(2026, 10, 15, tzinfo=UTC)
with path.open("w") as file:
    file.write("<HealthData>\n")
    for index in range(args.records):
        stamp = (start + timedelta(seconds=index)).isoformat()
        file.write(
            f'<Record type="HKQuantityTypeIdentifierStepCount" sourceName="Example Watch" '
            f'unit="count" value="1" startDate="{stamp}" endDate="{stamp}"/>\n'
        )
    file.write("</HealthData>")
store = Store(root, config)
tracemalloc.start()
began = time.perf_counter()
result = import_apple(store, str(path))
elapsed = time.perf_counter() - began
_, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
store.close()
print(
    json.dumps(
        {
            "records": args.records,
            "elapsed_seconds": round(elapsed, 4),
            "peak_python_bytes": peak,
            "result": result,
        },
        indent=2,
    )
)
