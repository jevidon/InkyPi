"""The agenda's hourly chart variant: hazard flags and the fields they need."""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import pytz

# Plugins import their siblings as top-level packages (e.g. `plugins.base_plugin`),
# so src/ must be importable the same way it is at runtime.
ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from plugins.weather.weather import OPEN_METEO_FORECAST_URL, Weather  # noqa: E402

TZ = pytz.timezone("America/New_York")


def _plugin():
    return Weather({"id": "weather"})


def _open_meteo_hourly(count, start="2026-11-26T00:00", **series):
    """Open-Meteo hourly block. Each temperature is its hour's index, so a value names its time."""
    first = datetime.fromisoformat(start)
    hourly = {
        "time": [(first + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(count)],
        "temperature_2m": [float(h) for h in range(count)],
        "precipitation": [0.0] * count,
        "precipitation_probability": [0] * count,
        "weather_code": [0] * count,
        "uv_index": [0] * count,
    }
    hourly.update(series)
    return hourly


def _hours(hourly):
    """The parsed hours, with the first hour of `hourly` as the current one."""
    plugin = _plugin()
    with patch.object(plugin, "_open_meteo_hourly_start_index", return_value=0):
        return plugin._parse_open_meteo_hourly(hourly, "metric", TZ, "12h", [], [])


def _hour(**readings):
    """One parsed hour: calm, mild and clear unless told otherwise."""
    hour = {"temperature": 60, "feels_like": 60, "wind_gust": 5, "storm": False, "fog": False}
    hour.update(readings)
    return hour


class TestRequest:

    def test_asks_for_the_fields_the_flags_need(self):
        hourly_fields = OPEN_METEO_FORECAST_URL.split("&hourly=")[1].split("&")[0].split(",")

        assert "apparent_temperature" in hourly_fields
        assert "cape" in hourly_fields


class TestHourlyFlagReadings:

    def test_feels_like_is_rounded_and_absent_without_a_reading(self):
        hourly = _hours(_open_meteo_hourly(3, apparent_temperature=[1.6, None, 3.4]))

        assert [h["feels_like"] for h in hourly] == [2, None, 3]

    def test_a_thunder_code_is_a_storm(self):
        hourly = _hours(_open_meteo_hourly(3, weather_code=[0, 95, 3]))

        assert [h["storm"] for h in hourly] == [False, True, False]

    def test_high_cape_is_a_storm_only_with_a_real_chance_of_rain(self):
        # The chance of rain for an hour is reported at the hour's end.
        hourly = _hours(_open_meteo_hourly(4, cape=[1500, 1500, 400, 0], precipitation_probability=[0, 10, 60, 60]))

        assert [h["storm"] for h in hourly][:3] == [False, True, False]

    def test_fog_codes_are_fog(self):
        hourly = _hours(_open_meteo_hourly(3, weather_code=[45, 48, 3]))

        assert [h["fog"] for h in hourly] == [True, True, False]


class TestChartMidnights:

    def test_parsed_hours_carry_their_clock_hour(self):
        hourly = _hours(_open_meteo_hourly(26, start="2026-11-27T22:00"))

        assert [h["hour"] for h in hourly[:4]] == [22, 23, 0, 1]

    def test_each_midnight_names_the_day_it_starts(self):
        hourly = _hours(_open_meteo_hourly(60, start="2026-11-27T22:00"))

        assert Weather._chart_midnights(hourly[:48]) == [{"at": 2, "day": "Sat"}, {"at": 26, "day": "Sun"}]

    def test_a_chart_that_starts_at_midnight_has_no_rule_there(self):
        hourly = _hours(_open_meteo_hourly(30, start="2026-11-28T00:00"))

        assert Weather._chart_midnights(hourly[:24]) == []

    def test_the_chart_gets_its_midnights(self):
        # The made-up data starts at midnight and hour 40 is "now": 4 PM the next day.
        assert _template_params(40)["chart_midnights"] == [{"at": 8, "day": "Sat"}]


class TestChartFlags:

    def test_calm_mild_hours_raise_no_flags(self):
        assert Weather._chart_flags([_hour()] * 24, "imperial", "mph") == []

    def test_a_flag_covers_the_hours_it_holds_counted_from_now(self):
        hourly = [_hour()] * 3 + [_hour(wind_gust=31)] * 4 + [_hour()] * 5

        assert Weather._chart_flags(hourly, "imperial", "mph") == [
            {"kind": "gust", "label": "Gusts 30+ mph", "start": 3, "end": 7},
        ]

    def test_hours_a_short_gap_apart_share_a_flag(self):
        hourly = [_hour(fog=True), _hour(), _hour(), _hour(fog=True)]

        assert [(f["start"], f["end"]) for f in Weather._chart_flags(hourly, "imperial", "mph")] == [(0, 4)]

    def test_hours_a_long_gap_apart_get_a_flag_each(self):
        hourly = [_hour(fog=True), _hour(), _hour(), _hour(), _hour(fog=True)]

        assert [(f["start"], f["end"]) for f in Weather._chart_flags(hourly, "imperial", "mph")] == [(0, 1), (4, 5)]

    @pytest.mark.parametrize("wind_unit, gust, label", [
        ("mph", 30, "Gusts 30+ mph"), ("km/h", 50, "Gusts 50+ kph"), ("m/s", 13, "Gusts 13+ m/s"),
    ])
    def test_the_gust_limit_follows_the_wind_unit(self, wind_unit, gust, label):
        flags = Weather._chart_flags([_hour(wind_gust=gust - 1), _hour(wind_gust=gust)], "imperial", wind_unit)

        assert [(f["label"], f["start"]) for f in flags] == [(label, 1)]

    @pytest.mark.parametrize("units, freezing, hot, heat_label", [
        ("imperial", 32, 95, "Feels 95° or more"),
        ("metric", 0, 35, "Feels 35° or more"),
        ("standard", 273, 308, "Feels 308 or more"),
    ])
    def test_temperature_limits_follow_the_units(self, units, freezing, hot, heat_label):
        mild = (freezing + hot) // 2
        hourly = [
            _hour(temperature=freezing, feels_like=mild),
            _hour(temperature=freezing + 1, feels_like=mild),
            _hour(temperature=mild, feels_like=hot - 1),
            _hour(temperature=mild, feels_like=hot),
        ]

        flags = Weather._chart_flags(hourly, units, "mph")

        assert [(f["label"], f["start"], f["end"]) for f in flags] == [("Freezing", 0, 1), (heat_label, 3, 4)]

    def test_missing_readings_raise_no_flag(self):
        hourly = [{"temperature": 60, "feels_like": None, "wind_gust": None}]

        assert Weather._chart_flags(hourly, "imperial", "mph") == []

    def test_flags_come_in_the_order_they_start(self):
        hourly = [_hour(fog=True), _hour(storm=True)]

        assert [f["kind"] for f in Weather._chart_flags(hourly, "imperial", "mph")] == ["fog", "storm"]


class TestOpenWeatherMapHours:

    def test_hours_carry_what_the_flags_need(self):
        hours = [
            {"dt": 1795096800, "temp": 50, "feels_like": 44.6, "weather": [{"id": 211, "icon": "11d"}]},
            {"dt": 1795100400, "temp": 50, "feels_like": 45.2, "weather": [{"id": 741, "icon": "50d"}]},
            {"dt": 1795104000, "temp": 50, "weather": [{"id": 800, "icon": "01d"}]},
        ]

        hourly = _plugin()._parse_owm_hourly(hours, TZ, "12h", "imperial", "mph", [])

        assert [h["feels_like"] for h in hourly] == [45, 45, None]
        assert [h["storm"] for h in hourly] == [True, False, False]
        assert [h["fog"] for h in hourly] == [False, True, False]


class _DeviceConfig:

    def get_config(self, key, default=None):
        return {"timezone": "America/New_York", "time_format": "12h"}.get(key, default)

    def get_resolution(self):
        return (1600, 1200)


def _template_params(now_index, gust_hour=4, **settings):
    """Run generate_image on made-up Open-Meteo data; return the template params.

    The data is calm but for one gusty hour, `gust_hour` hours from now.
    """
    hourly = _open_meteo_hourly(120, wind_gusts_10m=[40.0 if h == now_index + gust_hour else 5.0 for h in range(120)])
    weather = {"current": {"time": "2026-11-27T06:00", "temperature": 9}, "daily": {}, "hourly": hourly}
    plugin = _plugin()
    captured = {}

    def capture(dimensions, html_file, css_file, template_params):
        captured.update(template_params)
        return object()

    all_settings = {
        "latitude": "40.7", "longitude": "-74.0", "units": "imperial", "weatherProvider": "OpenMeteo", "layout": "agenda",
    }
    all_settings.update(settings)
    with patch.object(plugin, "_get_open_meteo_data", return_value=weather), \
            patch.object(plugin, "_get_open_meteo_air_quality", return_value={}), \
            patch.object(plugin, "_open_meteo_hourly_start_index", return_value=now_index), \
            patch.object(plugin, "_fetch_ics_events", return_value=[]), \
            patch.object(plugin, "render_image", side_effect=capture):
        plugin.generate_image(all_settings, _DeviceConfig())
    return captured


class TestChartParams:

    @pytest.mark.parametrize("graph_hours, step", [("24", 1), ("48", 2), ("72", 3)])
    def test_the_chart_gets_the_hours_between_its_points(self, graph_hours, step):
        assert _template_params(40, graphHours=graph_hours)["chart_step"] == step

    # Hour 40 is "now": each temperature is its hour's index, so every hour ahead is above freezing.
    def test_the_chart_gets_the_hazards_over_its_span(self):
        params = _template_params(40)

        assert params["chart_flags"] == [{"kind": "gust", "label": "Gusts 30+ mph", "start": 4, "end": 5}]

    def test_a_hazard_in_the_hour_that_closes_the_chart_is_flagged(self):
        # The chart's last point is that hour, and its peak gust is written there.
        params = _template_params(40, gust_hour=24)

        assert [(f["kind"], f["start"]) for f in params["chart_flags"]] == [("gust", 24)]

    def test_hazards_beyond_the_chart_are_left_out(self):
        assert _template_params(40, gust_hour=25)["chart_flags"] == []
