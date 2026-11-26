import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytz

# Plugins import their siblings as top-level packages (e.g. `plugins.base_plugin`),
# so src/ must be importable the same way it is at runtime.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from plugins.weather.weather import (  # noqa: E402
    DAY_CHART_START_HOUR,
    Weather,
)

POINTS_PER_DAY = 24 - DAY_CHART_START_HOUR + 1


def _plugin():
    return Weather({"id": "weather"})


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


class _DeviceConfig:
    def get_config(self, key=None, default=None):
        return {"timezone": "America/New_York", "time_format": "12h"}.get(key, default)

    def get_resolution(self):
        return (1600, 1200)

    def load_env_key(self, key):
        return None


def _template_params(**settings):
    """Run generate_image on made-up Open-Meteo data and return what it hands the template."""
    tz = pytz.timezone("America/New_York")
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    days = [midnight + timedelta(days=d) for d in range(3)]
    weather = {
        "current": {"time": midnight.strftime("%Y-%m-%dT%H:%M"), "temperature": 9},
        "daily": {
            "time": [day.strftime("%Y-%m-%d") for day in days],
            "weathercode": [3] * 3,
            "temperature_2m_max": [18] * 3,
            "temperature_2m_min": [8] * 3,
            "sunrise": [day.strftime("%Y-%m-%dT06:00") for day in days],
            "sunset": [day.strftime("%Y-%m-%dT20:00") for day in days],
        },
        # Two full days: today and tomorrow each end with the next day's midnight.
        "hourly": _hourly(49, start=midnight.strftime("%Y-%m-%dT%H:%M")),
    }
    plugin = _plugin()
    captured = {}

    def capture(dimensions, html_file, css_file, template_params):
        captured.update(template_params)
        return object()

    all_settings = {
        "latitude": "40.7", "longitude": "-74.0", "units": "metric", "layout": "agenda",
        "weatherProvider": "OpenMeteo", "calendarURLs[]": ["https://example.invalid/a.ics"],
    }
    all_settings.update(settings)
    with patch.object(plugin, "_get_open_meteo_data", return_value=weather), \
            patch.object(plugin, "_get_open_meteo_air_quality", return_value={}), \
            patch.object(plugin, "_fetch_ics_events", return_value=[]), \
            patch.object(plugin, "render_image", side_effect=capture):
        plugin.generate_image(all_settings, _DeviceConfig())
    return captured


class TestForecastStyleSetting:

    def test_day_chart_is_the_default(self):
        cards = _template_params()["agenda_events"]

        # Today and tomorrow have a forecast and a full day of hourly data.
        assert [bool(card["chart"]) for card in cards[:2]] == [True, True]

    def test_chart_style_attaches_each_days_chart(self):
        cards = _template_params(agendaForecastStyle="chart")["agenda_events"]

        assert len(cards[0]["chart"]["temperature"]) == POINTS_PER_DAY

    def test_summary_style_leaves_the_charts_out(self):
        cards = _template_params(agendaForecastStyle="summary")["agenda_events"]

        assert [card["chart"] for card in cards] == [None] * len(cards)
        assert cards[0]["forecast"] is not None

    def test_card_without_a_forecast_has_no_chart(self):
        cards = _template_params()["agenda_events"]

        # The made-up forecast covers three days; the fourth card has none.
        assert cards[3]["forecast"] is None
        assert cards[3]["chart"] is None


class TestDayChartWindSetting:

    def test_own_setting_turns_wind_on(self):
        assert _template_params(agendaChartWind="true")["agenda_chart_wind"] is True

    def test_own_setting_turns_wind_off_even_with_hourly_wind_on(self):
        params = _template_params(agendaChartWind="false", displayGraphWind="true")

        assert params["agenda_chart_wind"] is False

    def test_wind_is_off_when_the_setting_is_missing(self):
        assert _template_params(displayGraphWind="true")["agenda_chart_wind"] is False


class TestClassicLayout:

    def test_classic_layout_has_no_agenda(self):
        params = _template_params(layout="classic")

        assert "agenda_events" not in params
        assert "sun_metrics" not in params
