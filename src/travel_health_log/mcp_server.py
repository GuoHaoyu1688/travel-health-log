"""Official SDK tools that share the CLI core and never contact an external service."""

from mcp.server.fastmcp import FastMCP

from .core.common import failure, settings, success
from .core.store import Store


def create_server(root, config):
    server = FastMCP("travel-health-log")

    def invoke(method, *args):
        store = None
        try:
            store = Store(root, config)
            return success(getattr(store, method)(*args))
        except Exception as exc:
            return failure(exc)[0]
        finally:
            if store:
                store.close()

    @server.tool()
    def log_metrics(date: str, metrics: dict, note: str = "") -> dict:
        """Write metrics, replacing the manual values for that date."""
        return invoke("log", date, metrics, "manual", note)

    @server.tool()
    def get_stats(days: int = 30, as_of: str | None = None) -> dict:
        """Read descriptive trends and adult BP range labels; never a diagnosis."""
        return invoke("stats", days, as_of)

    @server.tool()
    def compare_home_trip(days: int = 30, as_of: str | None = None) -> dict:
        """Read context means and sample counts; associations do not establish causation."""
        return invoke("compare", days, as_of)

    @server.tool()
    def list_due(as_of: str | None = None, at: str | None = None) -> dict:
        """Read previously entered schedules; no advice about starting or changing medications."""
        return invoke("due", as_of, at)

    @server.tool()
    def check_habit(name: str, date: str, done: bool = True) -> dict:
        """Write a habit check-in, replacing its completion flag for the selected day."""
        return invoke("check_habit", name, date, done)

    return server


def main():
    root, config = settings()
    create_server(root, config).run(transport="stdio")


if __name__ == "__main__":
    main()
