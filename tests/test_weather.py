import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

# Plugins import their siblings as top-level packages (e.g. `plugins.base_plugin`),
# so src/ must be importable the same way it is at runtime.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from plugins.weather.weather import Weather, _request_open_meteo  # noqa: E402

MODULE = "plugins.weather.weather"

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
        "titleSelection": "location", "displayRefreshTime": "true",
    }
    settings.update(overrides)
    return settings


def _run(settings, device_config, apis):
    """Run generate_image with the network and the browser screenshot mocked.

    Returns the template params handed to the renderer."""
    plugin = Weather({"id": "weather"})
    captured = {}

    def fake_render(dimensions, html_file, css_file=None, template_params={}):
        captured.update(template_params)
        captured["_template_files"] = (html_file, css_file)
        return object()

    with patch(f"{MODULE}.requests.get", side_effect=apis), \
            patch(f"{MODULE}.time.sleep"), \
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
        # The location's time zone, as Open-Meteo would have used: OWM names it Tokyo.
        # 1795644000 is 2026-11-25 17:00 in New York but 2026-11-26 in Tokyo.
        assert params["current_date"] == "Thursday, November 26"
        assert params["data_points"][0]["label"] == "Sunrise"
        assert "Falling back to OpenWeatherMap" in caplog.text
        assert "TLS handshake stalled" in caplog.text

    def test_fallback_keeps_the_device_time_zone_when_chosen(self):
        apis = FakeApis(open_meteo_error=requests.exceptions.ReadTimeout("TLS handshake stalled"))
        params = _run(_settings(weatherTimeZone="localTimeZone"), _device_config(api_key="key"), apis)

        assert params["backup_source"] == "OpenWeatherMap"
        assert params["current_date"] == "Wednesday, November 25"

    def test_open_meteo_parse_failure_falls_back_to_owm(self):
        apis = FakeApis()
        plugin_settings = _settings()
        with patch.object(Weather, "_parse_open_meteo_data", side_effect=KeyError("daily")):
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

    def test_open_meteo_requests_use_extended_timeout(self):
        apis = FakeApis()
        with patch(f"{MODULE}.requests.get", side_effect=apis) as get:
            plugin = Weather({"id": "weather"})
            plugin._get_open_meteo_data(40.7, -74.0, "imperial", "mph", 8, "America/New_York")
            plugin._get_open_meteo_air_quality(40.7, -74.0, "America/New_York")
        for call in get.call_args_list:
            assert call.kwargs["timeout"] == (15, 30)


class TestOpenMeteoRetries:

    def test_tls_timeouts_are_retried_with_exponential_backoff(self):
        response = _response(OPEN_METEO_DATA)
        timeout = requests.exceptions.ReadTimeout("TLS handshake stalled")
        with patch(f"{MODULE}.requests.get", side_effect=[timeout, timeout, response]) as get, \
                patch(f"{MODULE}.time.sleep") as sleep:
            result = _request_open_meteo("https://api.open-meteo.com/v1/forecast")

        assert result is response
        assert get.call_count == 3
        assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
        assert all(call.kwargs["timeout"] == (15, 30) for call in get.call_args_list)

    def test_transient_http_errors_are_retried(self):
        unavailable = _response({}, status=503)
        response = _response(OPEN_METEO_DATA)
        with patch(f"{MODULE}.requests.get", side_effect=[unavailable, response]) as get, \
                patch(f"{MODULE}.time.sleep") as sleep:
            result = _request_open_meteo("https://api.open-meteo.com/v1/forecast")

        assert result is response
        assert get.call_count == 2
        sleep.assert_called_once_with(1)

    def test_retry_attempts_are_bounded(self):
        timeout = requests.exceptions.ReadTimeout("TLS handshake stalled")
        with patch(f"{MODULE}.requests.get", side_effect=timeout) as get, \
                patch(f"{MODULE}.time.sleep") as sleep, \
                pytest.raises(requests.exceptions.ReadTimeout):
            _request_open_meteo("https://api.open-meteo.com/v1/forecast")

        assert get.call_count == 3
        assert [call.args[0] for call in sleep.call_args_list] == [1, 2]

    def test_non_retryable_http_error_returns_immediately(self):
        response = _response({}, status=400)
        with patch(f"{MODULE}.requests.get", return_value=response) as get, \
                patch(f"{MODULE}.time.sleep") as sleep:
            result = _request_open_meteo("https://api.open-meteo.com/v1/forecast")

        assert result is response
        get.assert_called_once()
        sleep.assert_not_called()


class TestOpenMeteoCache:

    @staticmethod
    def _failed_apis():
        return FakeApis(
            open_meteo_error=requests.exceptions.ReadTimeout("open-meteo down"),
            owm_error=requests.exceptions.ConnectionError("owm down"),
        )

    def test_last_good_payload_is_used_when_both_providers_fail(self, caplog):
        settings = _settings()
        config = _device_config(api_key="key")
        seeded = _run(settings, config, FakeApis())
        assert seeded["backup_source"] is None

        params = _run(settings, config, self._failed_apis())

        assert params["backup_source"] == "Open-Meteo cache"
        assert params["current_temperature"] == seeded["current_temperature"]
        assert "Using cached Open-Meteo data" in caplog.text

    def test_working_owm_takes_priority_over_the_cache(self):
        settings = _settings()
        config = _device_config(api_key="key")
        _run(settings, config, FakeApis())
        apis = FakeApis(open_meteo_error=requests.exceptions.ReadTimeout("open-meteo down"))

        params = _run(settings, config, apis)

        assert params["backup_source"] == "OpenWeatherMap"
        assert params["current_temperature"] == "50"

    def test_cache_is_not_reused_for_different_request_settings(self):
        config = _device_config(api_key="key")
        _run(_settings(), config, FakeApis())

        with pytest.raises(RuntimeError, match="OpenMeteo request failure"):
            _run(_settings(latitude="51.5"), config, self._failed_apis())

    def test_expired_cache_is_rejected(self, tmp_path):
        settings = _settings()
        config = _device_config(api_key="key")
        _run(settings, config, FakeApis())
        cache_path = tmp_path / "open_meteo_cache.json"
        cache = json.loads(cache_path.read_text())
        entry = next(iter(cache["entries"].values()))
        entry["fetched_at"] = 0
        cache_path.write_text(json.dumps(cache))

        with pytest.raises(RuntimeError, match="OpenMeteo request failure"):
            _run(settings, config, self._failed_apis())

    def test_corrupt_cache_preserves_the_original_provider_error(self, tmp_path, caplog):
        settings = _settings()
        config = _device_config(api_key="key")
        _run(settings, config, FakeApis())
        (tmp_path / "open_meteo_cache.json").write_text("not json")

        with pytest.raises(RuntimeError, match="OpenMeteo request failure"):
            _run(settings, config, self._failed_apis())
        assert "Unable to read Open-Meteo cache" in caplog.text
        assert "fallback to OpenWeatherMap also failed: owm down" in caplog.text

    def test_cache_write_failure_does_not_break_a_live_refresh(self, caplog):
        with patch("plugins.weather.weather.os.replace", side_effect=OSError("disk full")):
            params = _run(_settings(), _device_config(api_key="key"), FakeApis())

        assert params["backup_source"] is None
        assert params["current_temperature"] == "10"
        assert "Unable to write Open-Meteo cache" in caplog.text


class TestBackupSourceTemplate:

    def _render_header(self, backup_source, updated_at_text="(updated at 4:33 AM)"):
        params = _run(_settings(layout="agenda"), _device_config(api_key="key"), FakeApis())
        params.update(backup_source=backup_source, updated_at_text=updated_at_text)
        plugin = Weather({"id": "weather"})
        html = plugin.env.get_template("agenda.html").render(params)
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


class TestLayouts:

    def test_instances_saved_before_layouts_are_classic(self):
        params = _run(_settings(), _device_config(), FakeApis())

        assert params["layout"] == "classic"
        assert params["_template_files"] == ("weather.html", "weather.css")
        assert "agenda_events" not in params

    def test_agenda_layout_uses_its_own_template(self):
        params = _run(_settings(layout="agenda"), _device_config(), FakeApis())

        assert params["layout"] == "agenda"
        assert params["_template_files"] == ("agenda.html", "agenda.css")
        assert params["agenda_events"] == []

    def test_unknown_layout_falls_back_to_classic(self):
        params = _run(_settings(layout="poster"), _device_config(), FakeApis())

        assert params["_template_files"] == ("weather.html", "weather.css")

    def test_classic_metrics_are_all_present_in_order(self):
        params = _run(_settings(), _device_config(), FakeApis())

        assert [dp["label"] for dp in params["data_points"]] == [
            "Sunrise", "Sunset", "Wind", "Humidity", "Pressure", "UV Index", "Visibility", "Air Quality",
        ]
        assert params["feels_like"] == "10"

    def test_agenda_shows_sunrise_sunset_and_current_uv(self):
        params = _run(_settings(layout="agenda"), _device_config(), FakeApis())

        assert [dp["label"] for dp in params["sun_metrics"]] == ["Sunrise", "Sunset", "Current UV"]
        assert params["sun_metrics"][2]["measurement"] == "1.2"

    def test_owm_metrics_missing_from_the_response_show_as_unavailable(self):
        params = _run(_settings(weatherProvider="OpenWeatherMap"), _device_config(api_key="key"), FakeApis())
        metrics = {dp["label"]: dp for dp in params["data_points"]}

        assert metrics["Visibility"]["measurement"] == "N/A"
        assert metrics["Air Quality"]["measurement"] == "N/A"

    def test_refresh_time_setting_drives_the_agenda_updated_text(self):
        on = _run(_settings(layout="agenda", displayRefreshTime="true"), _device_config(), FakeApis())
        off = _run(_settings(layout="agenda", displayRefreshTime="false"), _device_config(), FakeApis())

        assert on["updated_at_text"].startswith("(updated at ")
        assert off["updated_at_text"] == ""


def _open_meteo_wind_param(apis):
    url = apis.called("/v1/forecast")[0]
    return url.split("wind_speed_unit=")[1].split("&")[0]


class TestWindUnit:

    def test_classic_metric_without_a_choice_stays_in_metres_per_second(self):
        apis = FakeApis()
        params = _run(_settings(units="metric"), _device_config(), apis)

        assert _open_meteo_wind_param(apis) == "ms"
        assert params["wind_unit"] == "m/s"

    def test_agenda_metric_without_a_choice_stays_in_kilometres_per_hour(self):
        apis = FakeApis()
        params = _run(_settings(units="metric", layout="agenda"), _device_config(), apis)

        assert _open_meteo_wind_param(apis) == "kmh"
        assert params["wind_unit"] == "km/h"

    def test_imperial_without_a_choice_is_miles_per_hour(self):
        for layout in ("classic", "agenda"):
            apis = FakeApis()
            _run(_settings(units="imperial", layout=layout), _device_config(), apis)
            assert _open_meteo_wind_param(apis) == "mph"

    def test_chosen_unit_is_used_in_either_layout(self):
        for layout in ("classic", "agenda"):
            for wind_unit, param in (("m/s", "ms"), ("km/h", "kmh"), ("mph", "mph")):
                apis = FakeApis()
                params = _run(_settings(units="metric", layout=layout, windUnit=wind_unit), _device_config(), apis)
                assert _open_meteo_wind_param(apis) == param
                assert params["wind_unit"] == wind_unit
                wind = next(dp for dp in params["data_points"] if dp["label"] == "Wind")
                assert wind["unit"] == wind_unit

    def test_owm_wind_is_converted_to_the_chosen_unit(self):
        apis = FakeApis()
        params = _run(
            _settings(weatherProvider="OpenWeatherMap", units="imperial", windUnit="km/h"),
            _device_config(api_key="key"), apis,
        )
        # OWM_DATA reports 3 mph now and 4 mph for the day.
        wind = next(dp for dp in params["data_points"] if dp["label"] == "Wind")
        assert wind["measurement"] == 4.8
        assert wind["unit"] == "km/h"
        assert params["forecast"][0]["wind_speed"] == 6

    def test_owm_wind_is_shown_as_reported_when_no_conversion_is_needed(self):
        params = _run(
            _settings(weatherProvider="OpenWeatherMap", units="metric"),
            _device_config(api_key="key"), FakeApis(),
        )
        wind = next(dp for dp in params["data_points"] if dp["label"] == "Wind")
        assert wind["measurement"] == 3
        assert wind["unit"] == "m/s"


class TestTimeZone:

    def test_open_meteo_follows_the_location_by_default(self):
        apis = FakeApis()
        _run(_settings(), _device_config(), apis)

        assert all("timezone=auto" in url for url in apis.called("open-meteo.com"))

    def test_open_meteo_follows_the_device_when_chosen(self):
        apis = FakeApis()
        _run(_settings(weatherTimeZone="localTimeZone"), _device_config(), apis)

        assert all("timezone=America%2FNew_York" in url for url in apis.called("open-meteo.com"))

    def test_open_meteo_times_are_read_in_the_zone_it_reports(self):
        plugin = Weather({"id": "weather"})
        tz = plugin._open_meteo_timezone({"timezone": "Asia/Tokyo"}, None)
        assert tz.zone == "Asia/Tokyo"
        assert plugin._open_meteo_timezone({}, "fallback") == "fallback"
        assert plugin._open_meteo_timezone({"timezone": "Not/AZone"}, "fallback") == "fallback"


class TestCoordinates:

    @pytest.mark.parametrize("lat, long", [("", "-74.0"), ("40.7", " "), (None, None)])
    def test_missing_coordinates_are_rejected(self, lat, long):
        with pytest.raises(RuntimeError, match="Latitude and Longitude are required."):
            _run(_settings(latitude=lat or "", longitude=long or ""), _device_config(), FakeApis())

    def test_non_numeric_coordinates_are_rejected(self):
        with pytest.raises(RuntimeError, match="must be valid numbers"):
            _run(_settings(latitude="north"), _device_config(), FakeApis())

    def test_zero_is_a_valid_coordinate(self):
        params = _run(_settings(latitude="0", longitude="0"), _device_config(), FakeApis())

        assert params["current_temperature"] == "10"
