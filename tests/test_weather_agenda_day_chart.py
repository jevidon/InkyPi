import os
import sys
from datetime import datetime, timedelta

# Plugins import their siblings as top-level packages (e.g. `plugins.base_plugin`),
# so src/ must be importable the same way it is at runtime.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from plugins.weather_agenda.weather_agenda import (  # noqa: E402
    DAY_CHART_START_HOUR,
    WeatherAgenda,
)

POINTS_PER_DAY = 24 - DAY_CHART_START_HOUR + 1


def _plugin():
    return WeatherAgenda({"id": "weather_agenda"})


def _hourly(hours, start="2026-11-27T00:00", **overrides):
    """Open-Meteo hourly block with `hours` consecutive entries from `start`.

    Each series counts up from its own base so a value identifies its hour.
    """
    first = datetime.fromisoformat(start)
    hourly = {
        "time": [(first + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(hours)],
        "temperature_2m": [10 + h * 0.25 for h in range(hours)],
        "precipitation": [h * 0.01 for h in range(hours)],
        "uv_index": [h * 0.1 for h in range(hours)],
        "wind_speed_10m": [h + 0.4 for h in range(hours)],
        "wind_gusts_10m": [h + 20.6 for h in range(hours)],
    }
    hourly.update(overrides)
    return hourly


class TestDayCharts:

    def test_day_runs_from_start_hour_to_the_closing_midnight(self):
        charts = _plugin()._parse_open_meteo_day_charts(_hourly(49), "metric")

        day = charts["2026-11-27"]
        assert len(day["temperature"]) == POINTS_PER_DAY
        # Hour 6 of the first day through hour 0 of the next (index 24).
        assert day["temperature"][0] == 11.5
        assert day["temperature"][-1] == 16.0

    def test_each_series_has_one_value_per_hour(self):
        day = _plugin()._parse_open_meteo_day_charts(_hourly(49), "metric")["2026-11-27"]

        assert {len(series) for series in day.values()} == {POINTS_PER_DAY}
        assert set(day) == {"temperature", "rain", "uv", "wind", "gust"}

    def test_values_are_rounded_for_the_chart(self):
        day = _plugin()._parse_open_meteo_day_charts(_hourly(49), "metric")["2026-11-27"]

        assert day["rain"][0] == 0.06
        assert day["uv"][0] == 0.6
        assert day["wind"][0] == 6
        assert day["gust"][0] == 27

    def test_day_without_its_closing_midnight_is_left_out(self):
        # 48 entries cover two calendar days, but the second has no next-day midnight.
        charts = _plugin()._parse_open_meteo_day_charts(_hourly(48), "metric")

        assert list(charts) == ["2026-11-27"]

    def test_day_that_starts_after_the_start_hour_is_left_out(self):
        charts = _plugin()._parse_open_meteo_day_charts(
            _hourly(42, start="2026-11-27T07:00"), "metric"
        )

        assert list(charts) == ["2026-11-28"]

    def test_standard_units_convert_temperature_to_kelvin(self):
        day = _plugin()._parse_open_meteo_day_charts(_hourly(49), "standard")["2026-11-27"]

        assert day["temperature"][0] == 284.6

    def test_missing_wind_and_gusts_become_gaps(self):
        hourly = _hourly(49)
        del hourly["wind_speed_10m"]
        hourly["wind_gusts_10m"] = [None] * 49

        day = _plugin()._parse_open_meteo_day_charts(hourly, "metric")["2026-11-27"]

        assert day["wind"] == [None] * POINTS_PER_DAY
        assert day["gust"] == [None] * POINTS_PER_DAY

    def test_missing_rain_and_uv_count_as_zero(self):
        hourly = _hourly(49, precipitation=[None] * 49)
        del hourly["uv_index"]

        day = _plugin()._parse_open_meteo_day_charts(hourly, "metric")["2026-11-27"]

        assert day["rain"] == [0] * POINTS_PER_DAY
        assert day["uv"] == [0] * POINTS_PER_DAY

    def test_day_with_a_missing_temperature_is_left_out(self):
        temperatures = [10.0] * 49
        temperatures[12] = None

        charts = _plugin()._parse_open_meteo_day_charts(
            _hourly(49, temperature_2m=temperatures), "metric"
        )

        # The second day is complete, so it is the only one charted.
        assert list(charts) == ["2026-11-28"]

    def test_no_hourly_data_gives_no_charts(self):
        assert _plugin()._parse_open_meteo_day_charts({}, "metric") == {}
