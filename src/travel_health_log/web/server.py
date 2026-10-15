"""Local FastAPI endpoint for an installable offline journal."""

import json
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from ..cli import TEMPLATES
from ..core.common import DISCLAIMER, METRICS, LogError, failure, success, today
from ..core.store import Store

STATIC = Path(__file__).parent / "static"
FILES = {
    "/": "index.html",
    "/app.js": "app.js",
    "/style.css": "style.css",
    "/sw.js": "sw.js",
    "/manifest.webmanifest": "manifest.webmanifest",
    "/icon.svg": "icon.svg",
}


def create_app(root, config):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse(
            failure(LogError("INVALID_ARGUMENT", "Invalid query parameters"))[0], status_code=400
        )

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        host = urlparse("//" + request.headers.get("host", "")).hostname or ""
        if host not in {item.strip("[]") for item in config["allowed_hosts"]}:
            return JSONResponse(
                {"ok": False, "error": {"code": "HOST_DENIED", "message": "Untrusted host"}},
                status_code=403,
            )
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse(
                {
                    "ok": False,
                    "error": {
                        "code": "ORIGIN_DENIED",
                        "message": "Cross-origin access is disabled",
                    },
                },
                status_code=403,
            )
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; worker-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = (
            "no-store" if request.url.path.startswith("/api") else "no-cache"
        )
        return response

    @app.get("/api/dashboard")
    def dashboard(days: int = 30, as_of: str | None = None):
        store = None
        try:
            store = Store(root, config)
            end = as_of or today(config)
            return success(
                {
                    "stats": store.stats(days, end),
                    "compare": store.compare(days, end),
                    "due": store.due(end),
                    "timezone": config["timezone"],
                    "today": today(config),
                    "metrics": METRICS,
                    "templates": TEMPLATES,
                    "disclaimer": DISCLAIMER,
                }
            )
        except Exception as exc:
            return JSONResponse(failure(exc)[0], status_code=400)
        finally:
            if store:
                store.close()

    @app.post("/api/sync")
    async def sync(request: Request):
        store = None
        try:
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                raise LogError("INVALID_CONTENT_TYPE", "Use application/json")
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise LogError("TOO_LARGE", "Sync request exceeds 1 MiB")
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise LogError("INVALID_BATCH", "Send an object with an events list")
            events = payload.get("events")
            if not isinstance(events, list) or not 1 <= len(events) <= 100:
                raise LogError("INVALID_BATCH", "Send 1 to 100 events")
            store = Store(root, config)
            results = []
            for event in events:
                try:
                    result = success(store.apply_event(event))
                except Exception as exc:
                    result = failure(exc)[0]
                result["id"] = event.get("id") if isinstance(event, dict) else None
                results.append(result)
            return success({"results": results})
        except Exception as exc:
            return JSONResponse(failure(exc)[0], status_code=400)
        finally:
            if store:
                store.close()

    @app.get("/api/report")
    def report(week: str):
        store = None
        try:
            store = Store(root, config)
            return success(store.report(week))
        except Exception as exc:
            return JSONResponse(failure(exc)[0], status_code=400)
        finally:
            if store:
                store.close()

    @app.get("/{path:path}")
    def static(path: str):
        name = FILES.get("/" + path)
        if name is None:
            return JSONResponse(
                {"ok": False, "error": {"code": "NOT_FOUND", "message": "Resource not found"}},
                status_code=404,
            )
        return FileResponse(
            STATIC / name,
            media_type="application/manifest+json" if name.endswith("webmanifest") else None,
        )

    return app
