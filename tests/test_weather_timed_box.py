import os
import sys
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
import pytz

# Plugins import their siblings as top-level packages (e.g. `plugins.base_plugin`),
# so src/ must be importable the same way it is at runtime.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from plugins.weather.weather import Weather  # noqa: E402

TZ = pytz.timezone("America/New_York")
DAY = date(2026, 11, 30)


def _plugin():
    return Weather({"id": "weather"})


def _codes(*spans, default=3):
    """24 hourly weather codes: `default`, overwritten by (start, end, code) spans."""
    codes = [default] * 24
    for start, end, code in spans:
        codes[start:end] = [code] * (end - start)
    return codes


def _outlook(codes, wet=(), time_format="12h"):
    return _plugin()._day_outlook(codes, list(wet), time_format)


class TestOutlook:

    @pytest.mark.parametrize("codes, expected", [
        (_codes((0, 24, 0)), "Sunny all day"),
        (_codes((0, 24, 3)), "Cloudy all day"),
        (_codes((0, 24, 45)), "Fog all day"),
        (_codes((0, 10, 45), (10, 24, 0)), "Fog until 10 AM, then sunny"),
        (_codes((0, 10, 48), (10, 24, 2)), "Fog until 10 AM, then partly cloudy"),
        (_codes((0, 13, 3), (13, 24, 2)), "Cloudy, clearing from 1 PM"),
        (_codes((0, 15, 1), (15, 24, 3)), "Sunny, clouding over from 3 PM"),
        (_codes((0, 19, 2), (19, 24, 45)), "Partly cloudy, fog from 7 PM"),
        (_codes((0, 19, 45), (19, 24, 0)), "Fog until 7 PM, then clear"),
        (_codes((0, 24, 3), (9, 12, 0), (15, 17, 0)), "Mostly cloudy"),
        (_codes((0, 9, 0), (9, 12, 3), (12, 15, 0), (15, 19, 2), (19, 24, 3)), "Mixed sun and cloud"),
    ])
    def test_dry_days_describe_the_sky(self, codes, expected):
        assert _outlook(codes) == expected

    def test_hours_before_the_day_starts_are_ignored(self):
        # Fog until 6 AM is before the outlook's hours, which start with the day chart's.
        assert _outlook(_codes((0, 6, 45), (6, 24, 0))) == "Sunny all day"

    @pytest.mark.parametrize("codes, wet, expected", [
        (_codes((14, 18, 81)), range(14, 18), "Showers 2–6 PM"),
        (_codes((11, 14, 95)), range(11, 14), "Thunderstorms 11 AM–2 PM"),
        (_codes((20, 24, 73)), range(20, 24), "Snow 8 PM–midnight"),
        (_codes((9, 12, 51)), range(9, 12), "Drizzle 9 AM–12 PM"),
        (_codes((0, 24, 63)), range(0, 24), "Rain all day"),
        (_codes((0, 24, 61)), [7, 8, 15, 21, 22], "Rain on and off"),
        (_codes((8, 10, 71), (10, 12, 61)), range(8, 12), "Rain and snow 8 AM–12 PM"),
    ])
    def test_wet_days_give_what_falls_and_when(self, codes, wet, expected):
        assert _outlook(codes, wet) == expected

    def test_rain_without_a_rain_code_is_called_rain(self):
        assert _outlook(_codes(), range(14, 18)) == "Rain 2–6 PM"

    def test_rain_only_at_night_leaves_a_dry_outlook(self):
        assert _outlook(_codes((0, 24, 0)), [2, 3]) == "Sunny all day"

    def test_24_hour_clock(self):
        assert _outlook(_codes((14, 18, 81)), range(14, 18), "24h") == "Showers 14:00–18:00"
        assert _outlook(_codes((0, 10, 45), (10, 24, 0)), time_format="24h") == "Fog until 10:00, then sunny"


class TestDetails:

    DAY_FORECAST = {"rain_pct": 60, "rain_amount": "5", "rain_unit": "mm", "uvi": 9, "wind_dir": "SW"}

    def test_rain_gives_the_total_and_when_it_falls(self):
        detail = _plugin()._rain_detail(self.DAY_FORECAST, list(range(14, 18)), DAY, "12h", TZ)
        assert detail == "5 mm · 2-6p"

    def test_rain_too_light_to_total_gives_only_when(self):
        day = dict(self.DAY_FORECAST, rain_amount=None)
        assert _plugin()._rain_detail(day, [9], DAY, "12h", TZ) == "9-10a"

    def test_a_day_without_wet_hours_is_dry_unless_rain_is_likely(self):
        plugin = _plugin()
        assert plugin._rain_detail(dict(self.DAY_FORECAST, rain_pct=20), [], DAY, "12h", TZ) == "dry all day"
        assert plugin._rain_detail(dict(self.DAY_FORECAST, rain_pct=40), [], DAY, "12h", TZ) == "little expected"

    def test_wind_gives_its_direction_and_strongest_gust(self):
        assert _plugin()._wind_detail(self.DAY_FORECAST, [12.4, 30.6, 22]) == "SW · gusts 31"
        assert _plugin()._wind_detail(dict(self.DAY_FORECAST, wind_dir=""), [18]) == "gusts 18"
        assert _plugin()._wind_detail(self.DAY_FORECAST, []) == "SW"

    def test_24_hour_ranges_are_whole_hours(self):
        day = dict(self.DAY_FORECAST)
        assert _plugin()._rain_detail(day, list(range(20, 24)), DAY, "24h", TZ) == "5 mm · 20–24"

    @pytest.mark.parametrize("degrees, point", [(0, "N"), (44, "NE"), (180, "S"), (225, "SW"), (350, "N"), (None, "")])
    def test_compass_point(self, degrees, point):
        assert Weather._compass_point(degrees) == point


def _hourly(days=2, **overrides):
    """Open-Meteo hourly data from DAY's midnight: cloudy, dry, light wind, no UV."""
    start = datetime.combine(DAY, datetime.min.time())
    hours = days * 24 + 1
    hourly = {
        "time": [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(hours)],
        "weather_code": [3] * hours,
        "precipitation": [0] * hours,
        "uv_index": [0] * hours,
        "wind_gusts_10m": [20] * hours,
    }
    hourly.update(overrides)
    return hourly


def _forecast(days=2):
    return [
        {"date": (DAY + timedelta(days=d)).isoformat(), "rain_pct": 50, "rain_amount": "2",
         "rain_unit": "mm", "uvi": 1, "wind_dir": "W"}
        for d in range(days)
    ]


class TestDayDetailsFromOpenMeteo:

    def test_each_covered_day_gets_details(self):
        details = _plugin()._parse_open_meteo_day_details(_hourly(), _forecast(), "metric", "12h", TZ)

        assert set(details) == {"2026-11-30", "2026-12-01"}
        assert details["2026-11-30"] == {
            "outlook": "Cloudy all day", "rain": "little expected", "wind": "W · gusts 20",
        }

    def test_rain_belongs_to_the_hour_before_its_time(self):
        # Open-Meteo reports the rain of 2-3 PM at 3 PM.
        rain = [0] * 49
        rain[15] = 1.2
        details = _plugin()._parse_open_meteo_day_details(_hourly(precipitation=rain), _forecast(), "metric", "12h", TZ)

        assert details["2026-11-30"]["rain"] == "2 mm · 2-3p"
        assert details["2026-11-30"]["outlook"] == "Rain 2–3 PM"

    def test_rain_below_the_wet_threshold_does_not_count(self):
        rain = [0.05] * 49
        details = _plugin()._parse_open_meteo_day_details(_hourly(precipitation=rain), _forecast(), "metric", "12h", TZ)

        assert details["2026-11-30"]["rain"] == "little expected"

    def test_a_day_the_hours_do_not_cover_is_left_out(self):
        hourly = _hourly()
        for values in hourly.values():
            del values[30:]  # the second day stops at 6 AM
        details = _plugin()._parse_open_meteo_day_details(hourly, _forecast(), "metric", "12h", TZ)

        assert list(details) == ["2026-11-30"]

    def test_missing_gusts_leave_only_the_direction(self):
        details = _plugin()._parse_open_meteo_day_details(
            _hourly(wind_gusts_10m=[None] * 49), _forecast(), "metric", "12h", TZ)

        assert details["2026-11-30"]["wind"] == "W"


class _DeviceConfig:
    def get_config(self, key=None, default=None):
        return {"timezone": "America/New_York", "time_format": "12h"}.get(key, default)

    def get_resolution(self):
        return (1600, 1200)

    def load_env_key(self, key):
        return None


def _template_params(**settings):
    """Run generate_image on made-up Open-Meteo data and return what it hands the template."""
    midnight = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    days = [midnight + timedelta(days=d) for d in range(3)]
    hours = 3 * 24 + 1
    weather = {
        "current": {"time": midnight.strftime("%Y-%m-%dT%H:%M"), "temperature": 9},
        "daily": {
            "time": [day.strftime("%Y-%m-%d") for day in days],
            "weathercode": [3] * 3,
            "temperature_2m_max": [18] * 3,
            "temperature_2m_min": [8] * 3,
            "sunrise": [day.strftime("%Y-%m-%dT06:00") for day in days],
            "sunset": [day.strftime("%Y-%m-%dT20:00") for day in days],
            "uv_index_max": [7.6] * 3,
        },
        "hourly": {
            "time": [(midnight + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(hours)],
            "temperature_2m": [10.0] * hours,
            "weather_code": [3] * hours,
        },
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


class TestTimedStyleSetting:

    def test_timed_style_attaches_each_days_details(self):
        cards = _template_params(agendaForecastStyle="timed")["agenda_events"]

        assert cards[0]["details"]["outlook"] == "Cloudy all day"
        assert cards[0]["chart"] is None
        # The fourth card has no forecast, so nothing to say about it.
        assert cards[3]["details"] is None

    @pytest.mark.parametrize("style", ["chart", "summary", None])
    def test_other_styles_have_no_details(self, style):
        settings = {} if style is None else {"agendaForecastStyle": style}
        cards = _template_params(**settings)["agenda_events"]

        assert [card["details"] for card in cards] == [None] * len(cards)

    def test_timed_box_renders_its_outlook_and_rows(self):
        params = _template_params(agendaForecastStyle="timed")
        html = _plugin().env.get_template("agenda.html").render(params)

        assert html.count('class="wa-agenda-day-wx wa-agenda-day-wx--timed"') == 3
        assert '<span class="wa-wx-outlook-text">Cloudy all day</span>' in html
        # Rain and wind have details; the UV row is its label and value.
        assert html.count('class="wa-wx-timed-detail"') == 6
        assert html.count('<span class="wa-wx-timed-value">UV 8</span>') == 3
