import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

# Plugins import their siblings as top-level packages (e.g. `plugins.base_plugin`),
# so src/ must be importable the same way it is at runtime.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from plugins.weather_agenda.weather_agenda import WeatherAgenda  # noqa: E402

MODULE = "plugins.weather_agenda.weather_agenda"

OPEN_METEO_DATA = {
    "current": {
        "time": "2026-11-19T10:00", "temperature": 10.2, "weather_code": 0,
        "is_day": 1, "windspeed": 5, "winddirection": 90, "uv_index": 1.2,
    },
    "daily": {
        "time": ["2026-11-19"], "weathercode": [0],
        "temperature_2m_max": [12], "temperature_2m_min": [3],
        "sunrise": ["2026-11-19T07:00"], "sunset": ["2026-11-19T16:30"],
        "precipitation_probability_max": [10], "uv_index_max": [2],
        "wind_speed_10m_max": [10], "wind_direction_10m_dominant": [180],
        "precipitation_sum": [0],
    },
    "hourly": {},
}

OWM_DATA = {
    "timezone": "Asia/Tokyo",  # deliberately not the device timezone
    "current": {
        "dt": 1795644000, "temp": 50.3, "weather": [{"icon": "01d"}],
        "sunrise": 1795583600, "sunset": 1795620600,
        "wind_speed": 3, "wind_deg": 90, "uvi": 1.5,
    },
    "daily": [{
        "dt": 1795600800, "weather": [{"icon": "01d"}], "moon_phase": 0.5,
        "temp": {"max": 55, "min": 40}, "pop": 0.2, "uvi": 3,
        "wind_speed": 4, "wind_deg": 100,
        "sunrise": 1795583600, "sunset": 1795620600,
    }],
    "hourly": [],
}


def _response(payload, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.content = b""
    return response


class FakeApis:
    """Stands in for requests.get, routing by host and recording every URL."""

    def __init__(self, open_meteo_error=None, owm_error=None):
        self.open_meteo_error = open_meteo_error
        self.owm_error = owm_error
        self.urls = []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if "open-meteo.com" in url:
            if self.open_meteo_error:
                raise self.open_meteo_error
            return _response(OPEN_METEO_DATA if "/forecast" in url else {"hourly": {}})
        if "openweathermap.org" in url:
            if self.owm_error:
                raise self.owm_error
            if "/geo/" in url:
                return _response([{"name": "Elsewhere", "country": "XX"}])
            return _response(OWM_DATA if "onecall" in url else {"list": []})
        raise AssertionError(f"Unexpected URL: {url}")

    def called(self, host):
        return [u for u in self.urls if host in u]


def _device_config(api_key=None):
    config = {"timezone": "America/New_York", "time_format": "12h", "orientation": "horizontal"}
    device_config = MagicMock()
    device_config.get_config.side_effect = lambda key, default=None: config.get(key, default)
    device_config.get_resolution.return_value = [800, 480]
    device_config.load_env_key.return_value = api_key
    return device_config


def _settings(**overrides):
    settings = {
        "latitude": "40.7", "longitude": "-74.0", "units": "imperial",
        "weatherProvider": "OpenMeteo", "customTitle": "Home",
        "titleSelection": "location", "displayUpdatedAt": "true",
    }
    settings.update(overrides)
    return settings


def _run(settings, device_config, apis):
    """Run generate_image with the network and the browser screenshot mocked.

    Returns the template params handed to the renderer."""
    plugin = WeatherAgenda({"id": "weather_agenda"})
    captured = {}

    def fake_render(dimensions, html_file, css_file=None, template_params={}):
        captured.update(template_params)
        return object()

    with patch(f"{MODULE}.requests.get", side_effect=apis), \
            patch.object(plugin, "render_image", side_effect=fake_render):
        plugin.generate_image(settings, device_config)
    return captured


class TestOpenMeteoFallback:

    def test_open_meteo_success_does_not_use_backup(self):
        apis = FakeApis()
        params = _run(_settings(), _device_config(api_key="key"), apis)

        assert params["backup_source"] is None
        assert params["current_temperature"] == "10"
        assert params["title"] == "Home"
        assert apis.called("open-meteo.com")
        assert not apis.called("openweathermap.org")

    def test_open_meteo_failure_falls_back_to_owm(self, caplog):
        apis = FakeApis(open_meteo_error=requests.exceptions.ReadTimeout("TLS handshake stalled"))
        params = _run(_settings(), _device_config(api_key="key"), apis)

        assert params["backup_source"] == "OpenWeatherMap"
        assert params["current_temperature"] == "50"  # from OWM_DATA
        assert params["units"] == "imperial"
        assert params["temperature_unit"] == "°F"
        # The user's title is kept; no reverse geocoding call is made.
        assert params["title"] == "Home"
        assert not [u for u in apis.called("openweathermap.org") if "/geo/" in u]
        assert all("units=imperial" in u for u in apis.called("onecall"))
        # Device timezone (New York), not the OWM-reported one (Tokyo):
        # 1795644000 is 2026-11-25 17:00 in New York but 2026-11-26 in Tokyo.
        assert params["current_date"] == "Wednesday, November 25"
        assert params["data_points"][0]["label"] == "Sunrise"
        assert "Falling back to OpenWeatherMap" in caplog.text
        assert "TLS handshake stalled" in caplog.text

    def test_open_meteo_parse_failure_falls_back_to_owm(self):
        apis = FakeApis()
        plugin_settings = _settings()
        with patch.object(WeatherAgenda, "_parse_open_meteo_data", side_effect=KeyError("daily")):
            params = _run(plugin_settings, _device_config(api_key="key"), apis)
        assert params["backup_source"] == "OpenWeatherMap"

    def test_open_meteo_failure_without_key_raises(self, caplog):
        apis = FakeApis(open_meteo_error=requests.exceptions.ConnectTimeout("dead server"))
        with pytest.raises(RuntimeError, match="OpenMeteo request failure, please check logs."):
            _run(_settings(), _device_config(api_key=None), apis)

        assert not apis.called("openweathermap.org")
        assert "dead server" in caplog.text
        assert "no OpenWeatherMap API key configured" in caplog.text

    def test_both_providers_failing_raises(self, caplog):
        apis = FakeApis(
            open_meteo_error=requests.exceptions.ConnectTimeout("dead server"),
            owm_error=requests.exceptions.ConnectionError("owm down"),
        )
        with pytest.raises(RuntimeError, match="OpenMeteo request failure, please check logs."):
            _run(_settings(), _device_config(api_key="key"), apis)

        assert apis.called("openweathermap.org")
        assert "dead server" in caplog.text
        assert "fallback to OpenWeatherMap also failed: owm down" in caplog.text

    def test_owm_provider_has_no_reverse_fallback(self):
        apis = FakeApis(owm_error=requests.exceptions.ConnectionError("owm down"))
        with pytest.raises(RuntimeError, match="OpenWeatherMap request failure, please check logs."):
            _run(_settings(weatherProvider="OpenWeatherMap"), _device_config(api_key="key"), apis)

        assert not apis.called("open-meteo.com")

    def test_open_meteo_requests_use_short_timeout(self):
        apis = FakeApis()
        with patch(f"{MODULE}.requests.get", side_effect=apis) as get:
            plugin = WeatherAgenda({"id": "weather_agenda"})
            plugin._get_open_meteo_data(40.7, -74.0, "imperial", 8, "America/New_York")
            plugin._get_open_meteo_air_quality(40.7, -74.0, "America/New_York")
        for call in get.call_args_list:
            connect, read = call.kwargs["timeout"]
            assert connect + read <= 15


class TestBackupSourceTemplate:

    def _render_header(self, backup_source, updated_at_text="(updated at 4:33 AM)"):
        params = _run(_settings(), _device_config(api_key="key"), FakeApis())
        params.update(backup_source=backup_source, updated_at_text=updated_at_text)
        plugin = WeatherAgenda({"id": "weather_agenda"})
        html = plugin.env.get_template("weather_agenda.html").render(params)
        start = html.index('<div class="wa-header">')
        return html[start:html.index('<div class="wa-sun-metrics">', start)]

    def test_tag_hidden_without_backup_source(self):
        header = self._render_header(backup_source=None)
        assert "wa-backup-tag" not in header
        assert '<span class="wa-updated-at-text">(updated at 4:33 AM)</span>' in header

    def test_tag_follows_updated_at_text(self):
        header = self._render_header(backup_source="OpenWeatherMap")
        assert (
            '<span class="wa-updated-at-text">(updated at 4:33 AM)</span>'
            '<span class="wa-backup-tag">Backup source</span>'
            '<span class="wa-backup-source">OpenWeatherMap</span>'
        ) in header

    def test_tag_shown_when_updated_at_is_off(self):
        header = self._render_header(backup_source="OpenWeatherMap", updated_at_text="")
        assert "wa-updated-at-text" not in header
        assert '<span class="wa-backup-tag">Backup source</span>' in header
