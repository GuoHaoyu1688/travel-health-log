# Agent operations

Use this tool to maintain a private daily journal and summarize descriptive home/travel associations. It does not diagnose or prescribe. Never upload health records, backups, or browser queues to external services without explicit user authorization. Every bundled example is fictional.

## Install and paths

```sh
git clone https://github.com/GuoHaoyu1688/travel-health-log.git
cd travel-health-log
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
export THL_DATA_DIR=/workspace/data/travel-health-log
thl --version
```

Python 3.11+; no API keys or system OCR packages. Store SQLite at `$THL_DATA_DIR/health.db`. Precedence: `--data-dir`, then `THL_DATA_DIR`, then `./data`. Configuration: `--config PATH` or `data/config.yaml`. Keys: `timezone` (Asia/Shanghai), `private_permissions` (true), `preferred_apple_source` (empty), `allowed_hosts` (127.0.0.1/localhost/[::1]). The library timezone is fixed after creation. Setting file modes 600/700 does not encrypt records.

## Full command reference

Global flags may appear anywhere: `--data-dir`, `--config`, `--json`, `--quiet`, `--progress`, `--yes`, `--version`. The CLI is noninteractive. All ordinary commands output one JSON envelope; `--json` makes it compact. XML progress goes to stderr. MCP reserves stdout for its protocol.

| Command | Arguments / effect |
| --- | --- |
| `log key=value ...` | `--date YYYY-MM-DD --source manual --note TEXT`; replaces supplied daily metrics only |
| `day home\|trip\|transit` | `--date YYYY-MM-DD --city CODE`; updates daily context |
| `entries` | Optional `--start` and `--end`; long-form stored entries with sources |
| `habit add NAME` | Manual by default; optional `--metric KEY --op gte\|lte\|eq\|before --target VALUE --start DATE` |
| `habit check NAME` | `--date DATE`; `--undone` explicitly marks incomplete |
| `habit list` | `--days 30 --as-of DATE`; rates and streak bounded by window |
| `templates` | Read optional fictional goals and room-movement example |
| `med add NAME` | Required `--schedule 08:00,20:00 --start DATE`; optional `--end DATE`; same name updates entered schedule |
| `med taken NAME` | `--date DATE --time HH:MM`; records an existing scheduled occurrence |
| `reminder add TITLE` | Required `--date DATE`; identical title/date is reused |
| `reminder done ID` | Marks a follow-up complete |
| `stats` / `compare` | `--days 7\|30\|90` or any integer 1–366, `--as-of DATE` |
| `due` | `--as-of DATE --at HH:MM`; default through 23:59; does not send anything |
| `import PATH` | Native CSV/JSON archive or generic metric rows |
| `import apple-health XML` | Streaming, entity-safe step/sleep/weight subset; `--progress` |
| `import trips CSV` | Standard timed trip rows or project 18 native archive CSV |
| `export` | Required `--format csv\|json --out PATH`; `--kind archive\|entries`, `--no-bom`, `--yes` for replacement |
| `backup` | Optional `--out PATH`; timestamped ZIP, never overwritten |
| `report` | Required `--week YYYY-Www`; optional `--out PATH`, replacement requires `--yes` |
| `serve` | `--host 127.0.0.1 --port 8915`; remote binding needs `--allow-lan` and explicit host configuration |
| `mcp` | Official SDK stdio service |

Metrics: sleep(hours), sleep_quality(1–5), steps(integer), weight(kg), bp(SYS/DIA), systolic/diastolic(mmHg), heart_rate(bpm), water(liters), alcohol(self-defined drinks), coffee(cups), exercise(minutes), exercise_type(text), mood(1–5), meals(count), bedtime/last_meal(HH:MM). Unknown keys and invalid bounds are errors. Input bounds are not medical judgments. Reserve `apple:` sources for the importer. Generic CSV files may use wide or long records; see README.

A real repeatable CLI example in an empty directory:

```sh
thl log sleep=7.5 steps=9200 --date 2026-10-15 --json
```

```json
{"ok":true,"data":{"date":"2026-10-15","metrics":{"sleep":7.5,"steps":9200},"changed":2,"source":"manual","ignored_stale":[]},"warnings":[]}
```

```json
{"ok":false,"error":{"code":"INVALID_VALUE","message":"weight must be between 1 and 600 kg"}}
```

Exit status: 0 success, 1 processing/I/O failure, 2 argument/data/configuration error, 3 missing resource. Never infer success only from the presence of an export file. Apple overlap warnings appear in the normal warning envelope; preserve them when summarizing imports.

## Idempotence, source selection, and synchronization

The long table key is `(date,metric,source)`. Repeating a value does not add another measurement. Manual values take precedence over other imports; imported daily rows take precedence over Apple data. Preferred Apple source wins when available; otherwise the largest daily device value is selected. Source names remain visible. Never sum multiple device summaries yourself.

Apple records are keyed by normalized content hashes. Reimport does not duplicate samples, and daily Apple aggregates are rebuilt. In-bed/awake segments are excluded. Overlapping asleep intervals are merged, gaps up to 90 minutes group sessions without counting the gaps, and the wake date owns the session. Step intervals split across local midnight. These rules differ from Apple's proprietary summaries. Unsupported types are ignored; malformed supported records abort atomically.

PWA events have stable random IDs. The server records changes and the event acknowledgement atomically; retries return that receipt. Same ID/different content is rejected. Older metric edits cannot overwrite newer values; inspect `ignored_stale`. Habit updates follow explicit processing order. Browser queue entries are removed only after acknowledgement. Browser storage must persist until sync completes; server backup cannot rescue an unsynced browser queue.

Generic imports validate in one transaction. Native archive restore requires an empty journal with the same timezone; reapplying the identical archive is a no-op. Existing export/report files require `--yes`. The live SQLite database, its symlink/hardlink aliases, and sidecar files are protected even with `--yes`. Before restoring, replacing exports, updating a medication schedule, or manually altering files, create a backup. Backups contain private unencrypted data. Do not infer permission to send a reminder from permission to run `due`.

## MCP setup

```sh
THL_DATA_DIR=/absolute/private/data /absolute/repo/.venv/bin/thl mcp
```

```json
{"mcpServers":{"travel-health":{"command":"/absolute/repo/.venv/bin/thl","args":["--data-dir","/absolute/private/data","mcp"]}}}
```

Tools and parameters:

- `log_metrics(date: string, metrics: object, note: string="")` writes manual values for supplied keys.
- `get_stats(days: integer=30, as_of: string|null=null)` reads descriptive trends and adult BP labels.
- `compare_home_trip(days: integer=30, as_of: string|null=null)` reads means and sample counts.
- `list_due(as_of: string|null=null, at: string|null=null)` reads entered schedules.
- `check_habit(name: string, date: string, done: boolean=true)` writes/replaces a check-in.

All tools return the same envelope. Exact SDK schemas are in `docs/mcp-tools.json`. Changes and medical interpretation must not be hidden behind a read-only sounding description. Do not treat a range label as a diagnosis.

## Recipes

1. “Record today.” Run `thl log ... --date DATE --json`; mark day context if supplied. Leave unknown values blank rather than guessing. Repeating daily values is replacement, not accumulation.
2. “Compare my travel month.” Run `thl compare --days 30 --as-of DATE --json`. Report each metric's sample counts and missing data; use “observed difference,” not a causal explanation.
3. “What reminders are due?” Run `thl due --as-of DATE --at HH:MM --json`. Restate the recorded plan without recommending dose changes. Request separate authorization before external delivery.
4. “Import my Apple export.” Run `backup`, then `thl import apple-health PATH --progress --json`. Inspect warnings, selected sources, timezones, and sample counts. The user exports the XML themselves; no cloud login is needed.
5. “Back up and move the journal.” Verify the PWA says 0 pending, run `backup` and native JSON `export`, import into an empty same-timezone directory, then compare counts and a known date. Keep the original until verified.

## Verification and release care

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest --cov=travel_health_log --cov-report=term-missing
.venv/bin/python examples/benchmark_import.py --records 10000
```

Tests include actual MCP stdio, HTTP sync, exact 90-day expectations, AHA boundary labels, Apple XML overlap/DST/entity checks, import rollback, event replay, and archive restore. The recorded real-browser test includes offline reload and an intentionally lost acknowledgement after commit. Increment the service-worker shell cache version whenever shipped static assets change. Never cache `/api/` in the service worker. Keep all screenshots/fixtures fictional and exclude `.coverage*`, user data, local databases, `.env`, virtual environments, and private reports from releases.
