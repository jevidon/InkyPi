import os
import sys
from unittest.mock import patch

import pytest

SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


@pytest.fixture(autouse=True)
def isolate_open_meteo_cache(tmp_path):
    """Keep tests from reading or writing the developer's real weather cache."""
    cache_path = tmp_path / "open_meteo_cache.json"
    with patch("plugins.weather.weather.OPEN_METEO_CACHE_PATH", str(cache_path)):
        yield
