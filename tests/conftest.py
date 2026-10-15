from pathlib import Path

import pytest

from travel_health_log.core.common import settings
from travel_health_log.core.store import Store


@pytest.fixture
def store(tmp_path):
    root, config = settings(tmp_path / "library")
    item = Store(root, config)
    yield item
    item.close()


@pytest.fixture
def examples():
    return Path(__file__).parents[1] / "examples"
