import os
import re
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import pytz

# Plugins import their siblings as top-level packages (e.g. `plugins.base_plugin`),
# so src/ must be importable the same way it is at runtime.
ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from plugins.weather.weather import (  # noqa: E402
    CHART_BARS,
    CHART_HOURS_CHOICES,
    HOURLY_FORECAST_HOURS,
    Weather,
)

TZ = pytz.timezone("America/New_York")
SETTINGS_PAGE = os.path.join(ROOT, "src", "plugins", "weather", "settings.html")


def _plugin():
    return Weather({"id": "weather"})


def _hours(count):
    """Hourly entries as the parsers give them. Each reading identifies its hour."""
    return [
        {
            "time": f"h{h}", "day": "Fri", "temperature": h, "rain": 0.1, "precipitation": 0.0,
            "uv_index": h, "wind_speed": h, "wind_gust": h + 10, "icon": f"icons/{h}.png",
        }
        for h in range(count)
    ]


def _open_meteo_hourly(count, start="2026-11-27T00:00"):
    """Open-Meteo hourly block. Rain and its chance count up so a value names its time."""
    first = datetime.fromisoformat(start)
    return {
        "time": [(first + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(count)],
        "temperature_2m": [10 + h for h in range(count)],
        "precipitation": [h / 10 for h in range(count)],
        "precipitation_probability": [h for h in range(count)],
        "weather_code": [0] * count,
        "uv_index": [0] * count,
    }


def _parse_open_meteo(hourly):
    plugin = _plugin()
    with patch.object(plugin, "_open_meteo_hourly_start_index", return_value=0):
        return plugin._parse_open_meteo_hourly(
            hourly, "metric", TZ, "12h", ["2026-11-27T07:00"], ["2026-11-27T16:30"]
        )


class TestChartHoursSetting:

    def test_missing_setting_is_24_hours(self):
        assert Weather._chart_hours({}) == 24

    @pytest.mark.parametrize("value", ["24", "48", "72"])
    def test_each_choice_is_kept(self, value):
        assert Weather._chart_hours({"graphHours": value}) == int(value)

    @pytest.mark.parametrize("value", ["12", "96", "", "soon", None])
    def test_anything_else_is_24_hours(self, value):
        assert Weather._chart_hours({"graphHours": value}) == 24

    def test_settings_page_offers_the_spans_the_plugin_accepts(self):
        with open(SETTINGS_PAGE, encoding="utf-8") as page:
            select = re.search(r'<select id="graphHours".*?</select>', page.read(), re.S).group(0)

        assert tuple(int(v) for v in re.findall(r'value="(\d+)"', select)) == CHART_HOURS_CHOICES


class TestChartPoints:

    def test_one_hour_steps_keep_each_hour_as_it_is(self):
        points = Weather._chart_points(_hours(HOURLY_FORECAST_HOURS), 1)

        # Each point also carries the highest UV of its hours: here, its own.
        assert points == [{**hour, "uv_index_max": hour["uv_index"]} for hour in _hours(CHART_BARS + 1)]

    @pytest.mark.parametrize("hours", CHART_HOURS_CHOICES)
    def test_each_span_has_24_bars_and_a_point_that_closes_it(self, hours):
        points = Weather._chart_points(_hours(HOURLY_FORECAST_HOURS), hours // CHART_BARS)

        assert len(points) == CHART_BARS + 1
        assert points[-1]["temperature"] == hours

    def test_point_keeps_the_readings_of_its_first_hour(self):
        point = Weather._chart_points(_hours(10), 3)[1]

        assert point["time"] == "h3"
        assert (point["temperature"], point["uv_index"], point["wind_speed"], point["wind_gust"]) == (3, 3, 3, 13)
        assert point["icon"] == "icons/3.png"

    def test_rain_is_the_total_over_the_step(self):
        hourly = _hours(6)
        for hour, amount in zip(hourly, [0.1, 0.25, 0.4, 0, 0, 0.05]):
            hour["rain"] = amount

        assert [p["rain"] for p in Weather._chart_points(hourly, 3)] == [0.75, 0.05]

    def test_chance_of_rain_is_the_highest_over_the_step(self):
        hourly = _hours(4)
        for hour, chance in zip(hourly, [0.2, 0.7, 0.1, 0.3]):
            hour["precipitation"] = chance

        assert [p["precipitation"] for p in Weather._chart_points(hourly, 2)] == [0.7, 0.3]

    def test_missing_rain_counts_as_none(self):
        hourly = _hours(2)
        hourly[0]["rain"] = None
        hourly[1]["precipitation"] = None

        point = Weather._chart_points(hourly, 2)[0]

        assert (point["rain"], point["precipitation"]) == (0.1, 0)

    def test_hourly_data_that_runs_out_gives_fewer_points(self):
        # OpenWeatherMap's 48 hours, three hours a step.
        points = Weather._chart_points(_hours(48), 3)

        assert len(points) == 16
        assert points[-1]["time"] == "h45"

    def test_no_hourly_data_gives_no_points(self):
        assert Weather._chart_points([], 2) == []


class TestHourlyData:

    def test_open_meteo_rain_is_read_from_the_end_of_its_hour(self):
        hourly = _parse_open_meteo(_open_meteo_hourly(3))

        assert [h["rain"] for h in hourly] == [0.1, 0.2, 0]
        assert [h["precipitation"] for h in hourly] == [0.01, 0.02, 0]

    def test_open_meteo_keeps_enough_hours_for_the_longest_chart(self):
        hourly = _parse_open_meteo(_open_meteo_hourly(100))

        assert len(hourly) == HOURLY_FORECAST_HOURS
        assert hourly[-1]["temperature"] == 10 + max(CHART_HOURS_CHOICES)

    def test_open_meteo_hours_name_their_day(self):
        hourly = _parse_open_meteo(_open_meteo_hourly(3, start="2026-11-27T23:00"))

        assert [h["day"] for h in hourly] == ["Fri", "Sat", "Sat"]

    def test_owm_hours_keep_their_own_rain_and_name_their_day(self):
        hours = [
            {"dt": 1795096800 + h * 3600, "temp": 10, "pop": 0.5, "weather": [{"icon": "10d"}],
             "rain": {"1h": 0.4 * h}}
            for h in range(2)
        ]
        hourly = _plugin()._parse_owm_hourly(hours, TZ, "12h", "metric", "m/s", [])

        assert [h["rain"] for h in hourly] == [0, 0.4]
        assert [h["day"] for h in hourly] == ["Thu", "Thu"]


class _DeviceConfig:
    def get_config(self, key=None, default=None):
        return {"timezone": "America/New_York", "time_format": "12h"}.get(key, default)

    def get_resolution(self):
        return (1600, 1200)

    def load_env_key(self, key):
        return None


def _template_params(**settings):
    """Run generate_image on 80 hours of made-up Open-Meteo data; return the template params."""
    weather = {
        "current": {"time": "2026-11-27T00:00", "temperature": 9},
        "daily": {"time": ["2026-11-27"], "sunrise": ["2026-11-27T07:00"], "sunset": ["2026-11-27T16:30"]},
        "hourly": _open_meteo_hourly(80),
    }
    plugin = _plugin()
    captured = {}

    def capture(dimensions, html_file, css_file, template_params):
        captured.update(template_params)
        return object()

    all_settings = {"latitude": "40.7", "longitude": "-74.0", "units": "metric", "weatherProvider": "OpenMeteo"}
    all_settings.update(settings)
    with patch.object(plugin, "_get_open_meteo_data", return_value=weather), \
            patch.object(plugin, "_get_open_meteo_air_quality", return_value={}), \
            patch.object(plugin, "_open_meteo_hourly_start_index", return_value=0), \
            patch.object(plugin, "render_image", side_effect=capture):
        plugin.generate_image(all_settings, _DeviceConfig())
    return captured


class TestChartParams:

    def test_chart_covers_24_hours_by_default(self):
        points = _template_params()["hourly_chart"]

        assert [p["temperature"] for p in points] == [10 + h for h in range(CHART_BARS + 1)]

    @pytest.mark.parametrize("layout", ["classic", "agenda"])
    def test_chosen_span_reaches_either_layouts_chart(self, layout):
        points = _template_params(graphHours="72", layout=layout)["hourly_chart"]

        assert [p["temperature"] for p in points] == [10 + h for h in range(0, 73, 3)]
        # Rain over hours 0-2, which Open-Meteo reports at hours 1-3.
        assert points[0]["rain"] == 0.6

    def test_hourly_forecast_stays_hourly(self):
        hourly = _template_params(graphHours="48")["hourly_forecast"]

        assert [h["temperature"] for h in hourly[:3]] == [10, 11, 12]


class TestAgendaChartTemplate:

    POINTS = [
        {"time": "3 PM", "day": "Thu", "temperature": 10, "rain": 0.5, "precipitation": 0.4,
         "uv_index": 1, "wind_speed": 18, "wind_gust": 30, "icon": "icons/10d.png"},
        {"time": "6 PM", "day": "Thu", "temperature": 8, "rain": 1.2, "precipitation": 0.8,
         "uv_index": 0, "wind_speed": 20, "wind_gust": 32, "icon": "icons/10n.png"},
        {"time": "9 PM", "day": "Thu", "temperature": 7, "rain": 0, "precipitation": 0.1,
         "uv_index": 0, "wind_speed": 15, "wind_gust": 25, "icon": "icons/01n.png"},
    ]

    def _render(self):
        settings = {"displayGraph": "true", "textColor": "#000000", "backgroundColor": "#ffffff"}
        return _plugin().env.get_template("agenda.html").render(
            plugin_settings=settings, units="metric", current_temperature="10", hourly_chart=self.POINTS,
        )

    def test_every_point_is_drawn_on_the_line(self):
        html = self._render()

        assert 'const labels        = ["3 PM", "6 PM", "9 PM"];' in html
        assert "const temperatures  = [10, 8, 7];" in html
        assert 'const days          = ["Thu", "Thu", "Thu"];' in html

    def test_one_rain_bar_runs_between_each_pair_of_points(self):
        assert "const rain          = [0.5, 1.2];" in self._render()

    def test_rain_bars_have_their_own_axis_at_full_width(self):
        html = self._render()

        assert "xAxisID: 'xBars'" in html
        # Grouped bars would share each slot with the lines' axis and draw at half width.
        assert "grouped: false" in html


class TestClassicChartTemplate:

    def test_bars_leave_out_the_point_that_closes_the_agenda_line(self):
        html = _plugin().env.get_template("weather.html").render(
            plugin_settings={"forecastDays": "7", "textColor": "#000000"},
            forecast=[{"high": 12, "low": 3}], data_points=[], hourly_chart=TestAgendaChartTemplate.POINTS,
        )

        assert 'const labels = ["3 PM", "6 PM"];' in html
        assert "const rain = [0.5, 1.2];" in html
        assert "const precipitation = [40.0, 80.0];" in html
