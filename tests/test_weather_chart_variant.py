"""The agenda's hourly chart variant: hours before now, hazard flags and the fields they need."""
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

from plugins.weather.weather import (  # noqa: E402
    CHART_PAST_HOURS,
    OPEN_METEO_FORECAST_URL,
    Weather,
)

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


def _hours(hourly, now_index, units="metric"):
    """(past, future) entries with `now_index` as the current hour."""
    plugin = _plugin()
    with patch.object(plugin, "_open_meteo_hourly_start_index", return_value=now_index):
        return plugin._parse_open_meteo_hours(hourly, units, TZ, "12h", [], [])


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

    def test_asks_for_yesterdays_hours(self):
        assert "&past_days=1" in OPEN_METEO_FORECAST_URL


class TestDropPastDays:

    def test_daily_rows_start_at_today_and_hourly_rows_stay(self):
        weather = {
            "current": {"time": "2026-11-27T22:45"},
            "daily": {"time": ["2026-11-26", "2026-11-27", "2026-11-28"], "sunrise": ["a", "b", "c"]},
            "hourly": {"time": ["2026-11-26T00:00", "2026-11-27T00:00"]},
        }

        Weather._drop_past_days(weather)

        assert weather["daily"] == {"time": ["2026-11-27", "2026-11-28"], "sunrise": ["b", "c"]}
        assert weather["hourly"]["time"] == ["2026-11-26T00:00", "2026-11-27T00:00"]

    def test_days_that_already_start_today_are_left_alone(self):
        weather = {"current": {"time": "2026-11-27T22:45"}, "daily": {"time": ["2026-11-27", "2026-11-28"]}}

        Weather._drop_past_days(weather)

        assert weather["daily"]["time"] == ["2026-11-27", "2026-11-28"]

    @pytest.mark.parametrize("weather", [
        {},
        {"daily": {"time": ["2026-11-26"]}},
        {"current": {"time": "2026-11-27T22:45"}},
        # Every day is in the past: an odd reply, kept as it came.
        {"current": {"time": "2026-11-27T22:45"}, "daily": {"time": ["2026-11-25", "2026-11-26"]}},
    ])
    def test_replies_without_a_clear_today_are_left_alone(self, weather):
        before = {key: dict(value) for key, value in weather.items()}

        assert Weather._drop_past_days(weather) == before


class TestHoursEitherSideOfNow:

    def test_the_past_is_the_hours_just_before_now_oldest_first(self):
        past, future = _hours(_open_meteo_hourly(60), now_index=30)

        assert [h["temperature"] for h in past] == list(range(30 - CHART_PAST_HOURS, 30))
        assert future[0]["temperature"] == 30

    def test_the_past_is_shorter_when_the_data_starts_later(self):
        past, future = _hours(_open_meteo_hourly(60), now_index=2)

        assert [h["temperature"] for h in past] == [0, 1]
        assert future[0]["temperature"] == 2

    def test_hourly_forecast_is_still_the_hours_from_now_on(self):
        plugin = _plugin()
        with patch.object(plugin, "_open_meteo_hourly_start_index", return_value=30):
            hourly = plugin._parse_open_meteo_hourly(_open_meteo_hourly(60), "metric", TZ, "12h", [], [])

        assert [h["temperature"] for h in hourly] == list(range(30, 60))

    def test_feels_like_is_rounded_and_absent_without_a_reading(self):
        _, future = _hours(_open_meteo_hourly(3, apparent_temperature=[1.6, None, 3.4]), now_index=0)

        assert [h["feels_like"] for h in future] == [2, None, 3]

    def test_a_thunder_code_is_a_storm(self):
        _, future = _hours(_open_meteo_hourly(3, weather_code=[0, 95, 3]), now_index=0)

        assert [h["storm"] for h in future] == [False, True, False]

    def test_high_cape_is_a_storm_only_with_a_real_chance_of_rain(self):
        # The chance of rain for an hour is reported at the hour's end.
        hourly = _open_meteo_hourly(4, cape=[1500, 1500, 400, 0], precipitation_probability=[0, 10, 60, 60])
        _, future = _hours(hourly, now_index=0)

        assert [h["storm"] for h in future][:3] == [False, True, False]

    def test_fog_codes_are_fog(self):
        _, future = _hours(_open_meteo_hourly(3, weather_code=[45, 48, 3]), now_index=0)

        assert [h["fog"] for h in future] == [True, True, False]


class TestPastChartPoints:

    @pytest.mark.parametrize("step, temperatures", [(1, [0, 1, 2, 3, 4, 5]), (2, [0, 2, 4]), (3, [0, 3])])
    def test_six_hours_make_whole_steps_that_end_at_now(self, step, temperatures):
        past = [{"temperature": h, "rain": 0.1, "precipitation": 0, "uv_index": 0} for h in range(6)]

        points = Weather._chart_past_points(past, step)

        assert [p["temperature"] for p in points] == temperatures
        assert [p["rain"] for p in points] == [round(0.1 * step, 2)] * len(temperatures)

    def test_a_short_past_keeps_its_latest_whole_steps(self):
        past = [{"temperature": h, "rain": 0, "precipitation": 0, "uv_index": 0} for h in range(5)]

        assert [p["temperature"] for p in Weather._chart_past_points(past, 2)] == [1, 3]

    @pytest.mark.parametrize("past", [[], [{"temperature": 1, "rain": 0, "precipitation": 0, "uv_index": 0}]])
    def test_less_than_one_step_of_past_draws_none(self, past):
        assert Weather._chart_past_points(past, 2) == []


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


def _template_params(now_index, **settings):
    """Run generate_image on made-up Open-Meteo data; return the template params."""
    hourly = _open_meteo_hourly(120, wind_gusts_10m=[40.0 if h == now_index + 4 else 5.0 for h in range(120)])
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

    # Hour 40 is "now": each temperature is its hour's index, so every hour ahead is above freezing.
    @pytest.mark.parametrize("graph_hours, step, past", [("24", 1, [34, 35, 36, 37, 38, 39]), ("48", 2, [34, 36, 38]), ("72", 3, [34, 37])])
    def test_the_chart_gets_the_hours_before_now_in_its_own_steps(self, graph_hours, step, past):
        params = _template_params(40, graphHours=graph_hours)

        assert params["chart_step"] == step
        assert [p["temperature"] for p in params["chart_past"]] == past
        assert params["hourly_chart"][0]["temperature"] == 40

    def test_the_chart_gets_the_hazards_over_its_span(self):
        params = _template_params(40)

        assert params["chart_flags"] == [{"kind": "gust", "label": "Gusts 30+ mph", "start": 4, "end": 5}]

    def test_hazards_beyond_the_span_are_left_out(self):
        hourly = [_hour()] * 24 + [_hour(fog=True)]

        assert Weather._chart_flags(hourly[:24], "imperial", "mph") == []
