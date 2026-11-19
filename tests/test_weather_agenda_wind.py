import os
import sys
from unittest.mock import patch

import pytz

# Plugins import their siblings as top-level packages (e.g. `plugins.base_plugin`),
# so src/ must be importable the same way it is at runtime.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from plugins.weather_agenda.weather_agenda import (  # noqa: E402
    OPEN_METEO_FORECAST_URL,
    WeatherAgenda,
)

TZ = pytz.timezone("America/New_York")


def _plugin():
    return WeatherAgenda({"id": "weather_agenda"})


def _open_meteo_hourly(**extra):
    hourly = {
        "time": ["2026-11-19T09:00", "2026-11-19T10:00", "2026-11-19T11:00"],
        "temperature_2m": [9.6, 10.2, 11.8],
        "precipitation_probability": [10, 20, 30],
        "precipitation": [0, 0.2, 0],
        "weather_code": [0, 0, 0],
        "uv_index": [1.0, 2.0, 3.0],
    }
    hourly.update(extra)
    return hourly


def _parse_open_meteo(hourly, start_index=0):
    plugin = _plugin()
    with patch.object(plugin, "_open_meteo_hourly_start_index", return_value=start_index):
        return plugin._parse_open_meteo_hourly(
            hourly, "metric", TZ, "12h", ["2026-11-19T07:00"], ["2026-11-19T16:30"]
        )


def _owm_hour(**fields):
    hour = {"dt": 1795096800, "temp": 10.4, "pop": 0.1, "weather": [{"icon": "01d"}]}
    hour.update(fields)
    return hour


OWM_DAILY = [{"dt": 1795096800, "sunrise": 1795089600, "sunset": 1795125600}]


class TestHourlyWindData:

    def test_open_meteo_request_asks_for_hourly_wind_and_gusts(self):
        hourly_fields = OPEN_METEO_FORECAST_URL.split("&hourly=")[1].split("&")[0].split(",")

        assert "wind_speed_10m" in hourly_fields
        assert "wind_gusts_10m" in hourly_fields

    def test_open_meteo_hourly_carries_rounded_wind_and_gusts(self):
        hourly = _parse_open_meteo(_open_meteo_hourly(
            wind_speed_10m=[10.4, 12.6, 14.7], wind_gusts_10m=[20.2, 25.7, 30.4],
        ))

        assert [h["wind_speed"] for h in hourly] == [10, 13, 15]
        assert [h["wind_gust"] for h in hourly] == [20, 26, 30]

    def test_open_meteo_wind_lines_up_with_the_other_hourly_values(self):
        # The chart starts at the current hour, so wind must be sliced from the same index.
        hourly = _parse_open_meteo(_open_meteo_hourly(
            wind_speed_10m=[10.4, 12.6, 14.7], wind_gusts_10m=[20.2, 25.7, 30.4],
        ), start_index=1)

        assert [h["temperature"] for h in hourly] == [10, 11]
        assert [h["wind_speed"] for h in hourly] == [13, 15]
        assert [h["wind_gust"] for h in hourly] == [26, 30]

    def test_open_meteo_hourly_without_wind_fields_gives_none(self):
        hourly = _parse_open_meteo(_open_meteo_hourly())

        assert [h["wind_speed"] for h in hourly] == [None, None, None]
        assert [h["wind_gust"] for h in hourly] == [None, None, None]

    def test_owm_hourly_wind_is_converted_from_metres_per_second(self):
        hourly = _plugin()._parse_owm_hourly(
            [_owm_hour(wind_speed=5, wind_gust=10)], TZ, "12h", "metric", OWM_DAILY
        )

        assert hourly[0]["wind_speed"] == 18
        assert hourly[0]["wind_gust"] == 36

    def test_owm_hourly_wind_stays_in_mph_for_imperial(self):
        hourly = _plugin()._parse_owm_hourly(
            [_owm_hour(wind_speed=5, wind_gust=10)], TZ, "12h", "imperial", OWM_DAILY
        )

        assert hourly[0]["wind_speed"] == 5
        assert hourly[0]["wind_gust"] == 10

    def test_owm_hour_without_gust_gives_none(self):
        hourly = _plugin()._parse_owm_hourly(
            [_owm_hour(wind_speed=5)], TZ, "12h", "metric", OWM_DAILY
        )

        assert hourly[0]["wind_speed"] == 18
        assert hourly[0]["wind_gust"] is None


class TestWindChartTemplate:

    HOURS = [
        {"time": "9 AM", "temperature": 10, "precipitation": 0.1, "rain": 0, "uv_index": 1,
         "wind_speed": 18, "wind_gust": 36, "icon": "icons/01d.png"},
        {"time": "10 AM", "temperature": 11, "precipitation": 0.1, "rain": 0, "uv_index": 2,
         "wind_speed": 22, "wind_gust": None, "icon": "icons/01d.png"},
    ]

    def _render(self, hours=None, **plugin_settings):
        settings = {"displayGraph": "true", "textColor": "#000000", "backgroundColor": "#ffffff"}
        settings.update(plugin_settings)
        return _plugin().env.get_template("weather_agenda.html").render(
            plugin_settings=settings, units="metric", current_temperature="10",
            hourly_forecast=self.HOURS if hours is None else hours,
        )

    def test_wind_and_gust_values_reach_the_chart_script(self):
        html = self._render(displayGraphWind="true")

        assert "const windRaw       = [18, 22];" in html
        assert "const gustRaw       = [36, null];" in html

    def test_setting_on_enables_the_wind_layer(self):
        html = self._render(displayGraphWind="true")

        assert 'const showWind = "true" === "true"' in html

    def test_wind_layer_is_off_unless_the_setting_is_ticked(self):
        assert 'const showWind = "" === "true"' in self._render()
        assert 'const showWind = "false" === "true"' in self._render(displayGraphWind="false")

    def test_hours_saved_before_wind_existed_render_as_missing(self):
        old_hours = [{k: v for k, v in h.items() if not k.startswith("wind_")} for h in self.HOURS]
        html = self._render(hours=old_hours, displayGraphWind="true")

        assert "const windRaw       = [null, null];" in html
        assert "const gustRaw       = [null, null];" in html
