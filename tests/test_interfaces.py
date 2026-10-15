import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from travel_health_log.cli import main
from travel_health_log.mcp_server import create_server
from travel_health_log.web.server import create_app


def test_cli_complete_workflow(tmp_path, capsys, examples):
    root = tmp_path / "cli"

    def cli(*args, code=0):
        assert main(["--data-dir", str(root), *args, "--json"]) == code
        return json.loads(capsys.readouterr().out)

    assert cli("log", "sleep=7.5", "steps=9200", "bp=125/80", "--date", "2026-10-15")["ok"]
    cli("day", "trip", "--city", "C01", "--date", "2026-10-15")
    cli("habit", "add", "Steps", "--metric", "steps", "--target", "8000", "--start", "2026-10-01")
    cli("habit", "check", "Steps", "--date", "2026-10-15")
    assert cli("habit", "list", "--as-of", "2026-10-15")["data"][0]["done_today"]
    cli("templates")
    cli("med", "add", "Example plan", "--schedule", "08:00,20:00", "--start", "2026-10-01")
    cli("med", "taken", "Example plan", "--date", "2026-10-15", "--time", "08:00")
    cli("reminder", "add", "Example review", "--date", "2026-10-15")
    cli("reminder", "done", "1")
    cli("due", "--as-of", "2026-10-15")
    cli("stats", "--days", "7", "--as-of", "2026-10-15")
    cli("compare", "--as-of", "2026-10-15")
    cli("entries", "--start", "2026-10-15", "--end", "2026-10-15")
    cli("report", "--week", "2026-W42", "--out", str(tmp_path / "report.md"))
    cli("backup")
    cli("export", "--format", "json", "--out", str(tmp_path / "archive.json"))
    cli("import", "apple-health", str(examples / "apple-health.xml"), "--progress")
    cli("import", "trips", str(examples / "trips.csv"))
    cli("import", str(examples / "90-days.csv"))
    assert cli("log", "weight=NaN", code=2)["error"]["code"] == "INVALID_VALUE"
    assert cli("log", "x", code=2)["ok"] is False
    assert cli("log", code=2)["ok"] is False
    assert cli("habit", "check", "missing", code=3)["error"]["code"] == "NOT_FOUND"
    assert cli("serve", "--host", "0.0.0.0", code=2)["error"]["code"] == "LAN_DISABLED"


def test_all_five_mcp_tools_and_actual_stdio(store, tmp_path):
    store.add_habit("Stretch", start="2026-10-01")

    async def direct():
        server = create_server(store.root, store.config)
        listed = await server.list_tools()
        assert {tool.name for tool in listed} == {
            "log_metrics",
            "get_stats",
            "compare_home_trip",
            "list_due",
            "check_habit",
        }
        for name, args in [
            ("log_metrics", {"date": "2026-10-15", "metrics": {"sleep": 7}}),
            ("get_stats", {"days": 7, "as_of": "2026-10-15"}),
            ("compare_home_trip", {"days": 7, "as_of": "2026-10-15"}),
            ("list_due", {"as_of": "2026-10-15"}),
            ("check_habit", {"name": "Stretch", "date": "2026-10-15"}),
        ]:
            blocks = await server.call_tool(name, args)
            structured = blocks[1] if isinstance(blocks, tuple) else None
            if structured:
                assert structured["ok"]
            else:
                assert json.loads(blocks[0].text)["ok"]

    asyncio.run(direct())

    async def actual():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "travel_health_log.mcp_server"],
            env={**os.environ, "THL_DATA_DIR": str(tmp_path / "stdio")},
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 5
                result = await session.call_tool(
                    "log_metrics", {"date": "2026-10-15", "metrics": {"sleep": 8}}
                )
                assert not result.isError
                assert json.loads(result.content[0].text)["ok"]
                result = await session.call_tool("get_stats", {"days": 7, "as_of": "2026-10-15"})
                assert json.loads(result.content[0].text)["data"]["recorded_days"] == 1

    asyncio.run(actual())


def test_fastapi_sync_replay_rejection_static_and_report(store):
    client = TestClient(create_app(store.root, store.config), base_url="http://localhost")
    assert client.get("/").status_code == 200
    assert client.get("/sw.js").status_code == 200
    assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert client.get("/api/dashboard").headers["cache-control"] == "no-store"
    event = {
        "id": "offline-event-one",
        "created_at": datetime.now(UTC).isoformat(),
        "action": "log",
        "payload": {"date": "2026-10-15", "metrics": {"sleep": 8}, "status": "home"},
    }
    response = client.post("/api/sync", json={"events": [event]}).json()
    assert response["data"]["results"][0]["ok"]
    assert client.post("/api/sync", json={"events": [event]}).json()["data"]["results"][0]["data"][
        "replayed"
    ]
    bad = {
        **event,
        "id": "invalid-event-two",
        "payload": {"date": "2026-10-15", "metrics": {"sleep": 99}},
    }
    assert not client.post("/api/sync", json={"events": [bad]}).json()["data"]["results"][0]["ok"]
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert (
        client.post(
            "/api/sync", json={"events": [event]}, headers={"origin": "https://outside.invalid"}
        ).status_code
        == 403
    )
    assert client.get("/", headers={"host": "outside.invalid"}).status_code == 403
    assert client.post("/api/sync", content="{}").status_code == 400
    assert client.get("/api/dashboard?days=0").status_code == 400
    assert client.get("/api/report?week=2026-W42").json()["ok"]
    assert client.get("/api/report?week=bad").status_code == 400
    assert client.get("/../../health.db").status_code == 404


def test_installed_cli_subprocess(tmp_path):
    result = subprocess.run(
        [
            str(Path(sys.executable).parent / "thl"),
            "--data-dir",
            str(tmp_path / "cmd"),
            "log",
            "sleep=7.5",
            "--date",
            "2026-10-15",
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and json.loads(result.stdout)["ok"]
