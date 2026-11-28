import hashlib
import json
import logging
import math
import os
import time
from collections import Counter
from datetime import datetime, timedelta, timezone, date as date_type, time as time_type
from urllib.parse import quote as url_quote

import icalendar
import pytz
import recurring_ical_events
import requests
from astral import moon
from PIL import ImageColor

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)


def get_moon_phase_name(phase_age: float) -> str:
    """Determines the name of the lunar phase based on the age of the moon."""
    PHASES_THRESHOLDS = [
        (1.0, "newmoon"),
        (7.0, "waxingcrescent"),
        (8.5, "firstquarter"),
        (14.0, "waxinggibbous"),
        (15.5, "fullmoon"),
        (22.0, "waninggibbous"),
        (23.5, "lastquarter"),
        (29.0, "waningcrescent"),
    ]
    for threshold, phase_name in PHASES_THRESHOLDS:
        if phase_age <= threshold:
            return phase_name
    return "newmoon"


def _parse_local(iso_str, tz):
    """Parse an Open-Meteo time and attach the time zone it was requested in.

    Open-Meteo returns times without an offset, already in the requested time
    zone. pytz's `localize` attaches that zone without relying on the OS time zone.
    """
    return tz.localize(datetime.fromisoformat(iso_str))


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------

# Values of the "layout" setting. Instances saved before it existed are classic.
LAYOUT_CLASSIC = "classic"  # current conditions, metrics, hourly graph and a forecast row
LAYOUT_AGENDA = "agenda"    # current conditions beside a card per day with its calendar events
# Template and stylesheet for each layout.
LAYOUT_TEMPLATES = {
    LAYOUT_CLASSIC: ("weather.html", "weather.css"),
    LAYOUT_AGENDA: ("agenda.html", "agenda.css"),
}

# Values of the "agendaForecastStyle" setting: what each agenda card shows beside its events.
AGENDA_FORECAST_STYLE_CHART = "chart"      # one line of day stats over that day's chart
AGENDA_FORECAST_STYLE_SUMMARY = "summary"  # three rows of stats and a large weather icon
AGENDA_FORECAST_STYLE_TIMED = "timed"      # an outlook line, then rain and wind with when, and UV
# Days shown in the agenda layout, starting today.
AGENDA_DAYS = 4
# First hour drawn in each agenda card's day chart. The chart ends at the midnight
# that closes the day. The timed box's outlook line describes the same hours.
DAY_CHART_START_HOUR = 6

# --- Timed forecast box ---
# Rain in an hour, in display units, from which the hour counts as wet.
WET_HOUR_RAIN = {"imperial": 0.004, "metric": 0.1, "standard": 0.1}
# Chance of rain, in percent, from which a day with no wet hours is not called dry.
DRY_DAY_MAX_CHANCE = 30
COMPASS_POINTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
# Open-Meteo weather codes: the sky on dry hours, and the kind of rain or snow on wet ones.
# A dry hour with any other code counts as cloudy.
SKY_BY_CODE = {0: "clear", 1: "clear", 2: "partly", 3: "cloudy", 45: "fog", 48: "fog"}
PRECIPITATION_BY_CODE = {
    **dict.fromkeys((51, 53, 55, 56, 57), "drizzle"),
    **dict.fromkeys((61, 63, 65, 66, 67), "rain"),
    **dict.fromkeys((80, 81, 82), "showers"),
    **dict.fromkeys((71, 73, 75, 77, 85, 86), "snow"),
    **dict.fromkeys((95, 96, 99), "thunder"),
}
# How cloudy each sky is, to tell clearing from clouding over.
SKY_CLOUDINESS = {"clear": 0, "partly": 1, "cloudy": 2}
# Share of the outlook's hours one sky must hold for "all day", or a sky before and
# after one change must hold between them.
OUTLOOK_STEADY_SHARE = 0.8

# ---------------------------------------------------------------------------
# Hourly chart
# ---------------------------------------------------------------------------

# Values of the "graphHours" setting: how many hours ahead the hourly chart reaches.
CHART_HOURS_CHOICES = (24, 48, 72)
CHART_HOURS_DEFAULT = 24
# Rain bars on the hourly chart, whatever its span: each covers 1, 2 or 3 hours.
CHART_BARS = 24
# Hourly entries kept from the provider: the longest chart and the hour that closes it.
HOURLY_FORECAST_HOURS = max(CHART_HOURS_CHOICES) + 1
# Hours before now that the agenda's hourly chart shows, faded, left of its "now" line.
CHART_PAST_HOURS = 6

# Hazard flags over the agenda's hourly chart. Hours of one hazard this close together
# are drawn as a single flag.
CHART_FLAG_GAP_HOURS = 2
# Gusts at or above this are flagged, in each wind unit.
FLAG_GUST = {"mph": 30, "km/h": 50, "m/s": 13}
# A feels-like temperature at or above this is flagged, in each unit system.
FLAG_HEAT = {"imperial": 95, "metric": 35, "standard": 308}
# A temperature at or below this is flagged as freezing.
FLAG_FREEZING = {"imperial": 32, "metric": 0, "standard": 273}
# Open-Meteo: thunderstorm weather codes, or this much CAPE (J/kg) with this chance of rain (%).
STORM_CODES = (95, 96, 99)
STORM_CAPE = 1000
STORM_RAIN_CHANCE = 30
FOG_CODES = (45, 48)
# OpenWeatherMap condition ids: 2xx is a thunderstorm, 741 is fog.
OWM_FOG_ID = 741

# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

UNITS = {
    "standard": {"temperature": "K", "distance": "km"},
    "metric": {"temperature": "°C", "distance": "km"},
    "imperial": {"temperature": "°F", "distance": "mi"},
}
# Values of the "timeFormat" setting. Anything else, such as "device", follows the
# device's own clock setting.
TIME_FORMATS = ("12h", "24h")
# Values of the "windUnit" setting.
WIND_UNITS = ("m/s", "km/h", "mph")
# Wind unit for instances saved before the setting existed, with metric and standard
# units: what each layout showed. Imperial units showed mph in both.
METRIC_WIND_UNIT = {LAYOUT_CLASSIC: "m/s", LAYOUT_AGENDA: "km/h"}
# OpenWeatherMap reports wind in m/s, or in mph with imperial units. Factor from what it
# reports to each display unit.
OWM_WIND_FACTORS = {
    "m/s": {"m/s": 1, "km/h": 3.6, "mph": 2.236936},
    "mph": {"m/s": 0.44704, "km/h": 1.609344, "mph": 1},
}
OWM_AQI_LABELS = ["Good", "Fair", "Moderate", "Poor", "Very Poor"]
OPEN_METEO_AQI_LABELS = ["Good", "Fair", "Moderate", "Poor", "Very Poor", "Ext Poor"]

# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

WEATHER_URL = (
    "https://api.openweathermap.org/data/3.0/onecall"
    "?lat={lat}&lon={long}&units={units}&exclude=minutely&appid={api_key}"
)
AIR_QUALITY_URL = (
    "http://api.openweathermap.org/data/2.5/air_pollution"
    "?lat={lat}&lon={long}&appid={api_key}"
)
GEOCODING_URL = (
    "http://api.openweathermap.org/geo/1.0/reverse"
    "?lat={lat}&lon={long}&limit=1&appid={api_key}"
)
OPEN_METEO_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={long}"
    "&hourly=weather_code,temperature_2m,precipitation,precipitation_probability"
    ",relative_humidity_2m,surface_pressure,visibility,uv_index"
    ",wind_speed_10m,wind_gusts_10m,apparent_temperature,cape"
    "&daily=weathercode,temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_probability_max,uv_index_max"
    ",wind_speed_10m_max,wind_direction_10m_dominant,precipitation_sum"
    "&current=temperature,windspeed,winddirection,is_day,precipitation"
    ",weather_code,apparent_temperature,uv_index"
    "&timezone={timezone}&models=best_match&forecast_days={forecast_days}"
    # Yesterday's hours too, so the hourly chart always has the hours before now.
    "&past_days=1"
)
OPEN_METEO_AIR_QUALITY_URL = (
    "https://air-quality-api.open-meteo.com/v1/air-quality"
    "?latitude={lat}&longitude={long}"
    "&hourly=european_aqi,uv_index,uv_index_clear_sky&timezone={timezone}"
)
OPEN_METEO_UNIT_PARAMS = {
    "standard": "temperature_unit=celsius&precipitation_unit=mm",  # temperature is converted to Kelvin later
    "metric":   "temperature_unit=celsius&precipitation_unit=mm",
    "imperial": "temperature_unit=fahrenheit&precipitation_unit=inch",
}
# Open-Meteo's name for each wind speed unit.
OPEN_METEO_WIND_UNITS = {"m/s": "ms", "km/h": "kmh", "mph": "mph"}
# Today and the seven days after it.
OPEN_METEO_FORECAST_DAYS = 8
# (connect, read) seconds. urllib3 performs the TLS handshake inside connect(),
# so a server that accepts TCP but never completes TLS is cut off by the
# connect value; the read value bounds each wait for response data.
OPEN_METEO_TIMEOUT = (15, 30)
OPEN_METEO_MAX_ATTEMPTS = 3
OPEN_METEO_BACKOFF_SECONDS = 1
OPEN_METEO_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})
OPEN_METEO_RETRYABLE_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)
OPEN_METEO_CACHE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "open_meteo_cache.json")
)
OPEN_METEO_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
OPEN_METEO_CACHE_MAX_ENTRIES = 8


def _request_open_meteo(url):
    """Fetch an Open-Meteo URL with bounded retries for transient failures.

    TLS handshake failures surface as requests.ReadTimeout even though urllib3
    applies the connect timeout while establishing the connection. Retry both
    timeout and connection exception families to cover that case.
    """
    for attempt in range(OPEN_METEO_MAX_ATTEMPTS):
        try:
            response = requests.get(url, timeout=OPEN_METEO_TIMEOUT)
        except OPEN_METEO_RETRYABLE_EXCEPTIONS as error:
            if attempt == OPEN_METEO_MAX_ATTEMPTS - 1:
                raise
            failure = str(error)
        else:
            retryable_status = response.status_code in OPEN_METEO_RETRYABLE_STATUSES
            if not retryable_status or attempt == OPEN_METEO_MAX_ATTEMPTS - 1:
                return response
            failure = f"HTTP {response.status_code}"

        delay = OPEN_METEO_BACKOFF_SECONDS * (2 ** attempt)
        logger.warning(
            "Open-Meteo request attempt %d/%d failed (%s); retrying in %ds.",
            attempt + 1,
            OPEN_METEO_MAX_ATTEMPTS,
            failure,
            delay,
        )
        time.sleep(delay)


def _open_meteo_cache_key(lat, long, units, wind_unit, request_tz):
    """Identify all request settings that affect the cached provider response."""
    serialized = json.dumps(
        [lat, long, units, wind_unit, request_tz], separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _read_open_meteo_cache():
    try:
        with open(OPEN_METEO_CACHE_PATH, encoding="utf-8") as cache_file:
            cache = json.load(cache_file)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        logger.warning(f"Unable to read Open-Meteo cache: {error}")
        return {}
    return cache if isinstance(cache, dict) else {}


def _load_open_meteo_payload(cache_key):
    cache = _read_open_meteo_cache()
    entries = cache.get("entries")
    if cache.get("version") != 1 or not isinstance(entries, dict):
        return None
    entry = entries.get(cache_key)
    if not isinstance(entry, dict):
        return None
    try:
        age = time.time() - float(entry["fetched_at"])
        weather_data = entry["weather_data"]
        aqi_data = entry["air_quality_data"]
    except (KeyError, TypeError, ValueError):
        return None
    if age > OPEN_METEO_CACHE_MAX_AGE_SECONDS:
        return None
    if not isinstance(weather_data, dict) or not isinstance(aqi_data, dict):
        return None
    return weather_data, aqi_data, float(entry["fetched_at"])


def _store_open_meteo_payload(cache_key, weather_data, aqi_data):
    """Persist a last-good payload without turning a cache error into a refresh error."""
    cache = _read_open_meteo_cache()
    entries = cache.get("entries")
    if cache.get("version") != 1 or not isinstance(entries, dict):
        entries = {}
    entries[cache_key] = {
        "fetched_at": time.time(),
        "weather_data": weather_data,
        "air_quality_data": aqi_data,
    }

    def fetched_at(item):
        try:
            return float(item[1].get("fetched_at", 0))
        except (AttributeError, TypeError, ValueError):
            return 0

    entries = dict(sorted(entries.items(), key=fetched_at, reverse=True)[:OPEN_METEO_CACHE_MAX_ENTRIES])
    temporary_path = f"{OPEN_METEO_CACHE_PATH}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(OPEN_METEO_CACHE_PATH), exist_ok=True)
        with open(temporary_path, "w", encoding="utf-8") as cache_file:
            json.dump({"version": 1, "entries": entries}, cache_file)
        os.replace(temporary_path, OPEN_METEO_CACHE_PATH)
    except (OSError, TypeError, ValueError) as error:
        logger.warning(f"Unable to write Open-Meteo cache: {error}")
        try:
            os.remove(temporary_path)
        except OSError:
            pass


class Weather(BasePlugin):
    """Current conditions and forecast, in one of two layouts.

    Classic: current conditions and metrics, an hourly graph and a forecast row.
    Agenda: current conditions and an hourly graph beside a card for each of the
    next few days, holding that day's calendar events and forecast.
    """

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["api_key"] = {
            "required": True,
            "service": "OpenWeatherMap",
            "expected_key": "OPEN_WEATHER_MAP_SECRET",
        }
        template_params["style_settings"] = True
        return template_params

    # ==================================================================
    # Main entry point
    # ==================================================================

    def generate_image(self, settings, device_config):
        lat, long = self._parse_coordinates(settings)

        units = settings.get("units")
        if units not in UNITS:
            raise RuntimeError("Units are required.")

        layout = settings.get("layout", LAYOUT_CLASSIC)
        if layout not in LAYOUT_TEMPLATES:
            layout = LAYOUT_CLASSIC
        wind_unit = self._wind_unit(settings, units, layout)

        weather_provider = settings.get("weatherProvider", "OpenWeatherMap")
        title = settings.get("customTitle", "")

        timezone_name = device_config.get_config("timezone", default="America/New_York")
        time_format = self._time_format(settings, device_config)
        tz = pytz.timezone(timezone_name)
        # Weather times follow the location's time zone unless the device's is chosen.
        use_location_tz = settings.get("weatherTimeZone", "locationTimeZone") == "locationTimeZone"

        backup_source = None
        try:
            if weather_provider == "OpenWeatherMap":
                api_key = device_config.load_env_key("OPEN_WEATHER_MAP_SECRET")
                if not api_key:
                    raise RuntimeError("Open Weather Map API Key not configured.")
                template_params = self._get_owm_params(
                    api_key, lat, long, units, wind_unit, tz, use_location_tz, time_format
                )
                if settings.get("titleSelection", "location") == "location":
                    title = self._get_location(api_key, lat, long)
            elif weather_provider == "OpenMeteo":
                try:
                    template_params = self._get_open_meteo_params(
                        lat, long, units, wind_unit, tz, use_location_tz, time_format
                    )
                except Exception as e:
                    try:
                        template_params = self._get_owm_fallback(
                            e, device_config, lat, long, units, wind_unit, tz, use_location_tz, time_format
                        )
                        backup_source = "OpenWeatherMap"
                    except Exception as owm_error:
                        template_params = self._get_cached_open_meteo_params(
                            lat, long, units, wind_unit, tz, use_location_tz, time_format
                        )
                        if template_params is None:
                            raise
                        backup_source = "Open-Meteo cache"
                        logger.warning(f"Using cached Open-Meteo data after live providers failed: {owm_error}")
            else:
                raise RuntimeError(f"Unknown weather provider: {weather_provider}")
            template_params["title"] = title
        except Exception as e:
            logger.error(f"{weather_provider} request failed: {str(e)}")
            raise RuntimeError(f"{weather_provider} request failure, please check logs.")
        # Provider name shown in the agenda layout's "backup source" tag; None when
        # the configured provider was used.
        template_params["backup_source"] = backup_source
        template_params["layout"] = layout
        template_params["wind_unit"] = wind_unit
        template_params["plugin_settings"] = settings
        chart_hours = self._chart_hours(settings)
        chart_step = chart_hours // CHART_BARS
        hourly = template_params.get("hourly_forecast", [])
        template_params["hourly_chart"] = self._chart_points(hourly, chart_step)
        # For the agenda's chart: the hours before now, and the hazards over its span.
        template_params["chart_step"] = chart_step
        template_params["chart_past"] = self._chart_past_points(template_params.get("hourly_past", []), chart_step)
        template_params["chart_flags"] = self._chart_flags(hourly[:chart_hours], units, wind_unit)

        now = datetime.now(tz)
        if time_format == "24h":
            template_params["last_refresh_time"] = now.strftime("%Y-%m-%d %H:%M")
        else:
            template_params["last_refresh_time"] = now.strftime("%Y-%m-%d %I:%M %p")

        if layout == LAYOUT_AGENDA:
            # When this weather was fetched: now, unless it came from the cache.
            fetched_at = template_params.get("weather_fetched_at")
            weather_time = datetime.fromtimestamp(fetched_at, tz) if fetched_at is not None else now
            self._add_agenda_params(template_params, settings, tz, time_format, now, weather_time)

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        html_file, css_file = LAYOUT_TEMPLATES[layout]
        image = self.render_image(dimensions, html_file, css_file, template_params)
        if not image:
            raise RuntimeError("Failed to take screenshot, please check logs.")
        return image

    @staticmethod
    def _parse_coordinates(settings):
        raw_lat = str(settings.get("latitude", "")).strip()
        raw_long = str(settings.get("longitude", "")).strip()
        if not raw_lat or not raw_long:
            raise RuntimeError("Latitude and Longitude are required.")
        try:
            return float(raw_lat), float(raw_long)
        except (ValueError, TypeError):
            raise RuntimeError("Latitude and Longitude must be valid numbers.")

    @staticmethod
    def _time_format(settings, device_config):
        """The chosen clock, "12h" or "24h", or the device's when none is chosen."""
        time_format = settings.get("timeFormat")
        if time_format in TIME_FORMATS:
            return time_format
        return device_config.get_config("time_format", default="12h")

    @staticmethod
    def _wind_unit(settings, units, layout):
        """The chosen wind speed unit, or what the layout showed before there was a choice."""
        wind_unit = settings.get("windUnit")
        if wind_unit in WIND_UNITS:
            return wind_unit
        return "mph" if units == "imperial" else METRIC_WIND_UNIT[layout]

    @staticmethod
    def _owm_wind_factor(units, wind_unit):
        """Factor from OpenWeatherMap's wind speed to the display unit."""
        return OWM_WIND_FACTORS["mph" if units == "imperial" else "m/s"][wind_unit]

    @staticmethod
    def _chart_hours(settings):
        """Hours the hourly chart reaches ahead: the chosen span, or 24."""
        try:
            hours = int(settings.get("graphHours", CHART_HOURS_DEFAULT))
        except (TypeError, ValueError):
            return CHART_HOURS_DEFAULT
        return hours if hours in CHART_HOURS_CHOICES else CHART_HOURS_DEFAULT

    @staticmethod
    def _chart_points(hourly, step):
        """The hourly chart's points: one every `step` hours, CHART_BARS + 1 of them.

        A point keeps its hour's time, temperature, wind, UV and icon. Its rain is the
        rain bar that runs from it to the next point: the total over its `step` hours,
        and their highest chance of rain. Its `uv_index_max` is the highest UV over the
        same hours. The last point only closes the line, so its bar is not drawn. Fewer
        points come back when the hourly data runs out first.
        """
        points = []
        for start in range(0, min(len(hourly), (CHART_BARS + 1) * step), step):
            hours = hourly[start:start + step]
            points.append({
                **hours[0],
                "rain": round(sum(h.get("rain") or 0 for h in hours), 2),
                "precipitation": max(h.get("precipitation") or 0 for h in hours),
                "uv_index_max": max(h.get("uv_index") or 0 for h in hours),
            })
        return points

    @classmethod
    def _chart_past_points(cls, past, step):
        """Chart points for the hours before now, oldest first; they end where "now" begins.

        Up to CHART_PAST_HOURS hours, cut to a whole number of `step`-hour points so
        the last one's rain bar ends at now. Empty when the provider has no past hours.
        """
        keep = min(len(past), CHART_PAST_HOURS) // step * step
        return cls._chart_points(past[len(past) - keep:], step) if keep else []

    @staticmethod
    def _chart_flags(hourly, units, wind_unit):
        """Hazards over the hourly chart, each with the hours from now that it covers.

        `hourly` is one entry per hour of the chart. A flag is {"kind", "label",
        "start", "end"}: it runs from hour `start` to hour `end`, counted from now.
        Hours of one hazard no more than CHART_FLAG_GAP_HOURS apart share a flag.
        Temperatures are judged as the chart shows them, in whole degrees.
        """
        def reading(hour, key):
            value = hour.get(key)
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        def at_least(key, limit):
            return lambda hour: reading(hour, key) is not None and reading(hour, key) >= limit

        def at_most(key, limit):
            return lambda hour: reading(hour, key) is not None and reading(hour, key) <= limit

        degree = "" if units == "standard" else "°"
        gust, heat, freezing = FLAG_GUST[wind_unit], FLAG_HEAT[units], FLAG_FREEZING[units]
        rules = [
            ("storm", "Storms possible", lambda hour: bool(hour.get("storm"))),
            ("heat", f"Feels {heat}{degree} or more", at_least("feels_like", heat)),
            ("gust", f"Gusts {gust}+ {wind_unit.replace('km/h', 'kph')}", at_least("wind_gust", gust)),
            ("freezing", "Freezing", at_most("temperature", freezing)),
            ("fog", "Fog", lambda hour: bool(hour.get("fog"))),
        ]
        flags = []
        for kind, label, test in rules:
            flag = None
            for i, hour in enumerate(hourly):
                if not test(hour):
                    continue
                if flag and i - flag["end"] <= CHART_FLAG_GAP_HOURS:
                    flag["end"] = i + 1
                else:
                    flag = {"kind": kind, "label": label, "start": i, "end": i + 1}
                    flags.append(flag)
        return sorted(flags, key=lambda flag: flag["start"])

    # ==================================================================
    # Agenda layout
    # ==================================================================

    def _add_agenda_params(self, template_params, settings, tz, time_format, now, weather_time):
        """Add the calendar days, their forecasts and charts, and the header details."""
        calendar_urls = settings.get("calendarURLs[]", [])
        calendar_colors = settings.get("calendarColors[]", [])
        if not isinstance(calendar_urls, list):
            calendar_urls = [calendar_urls] if calendar_urls else []
        if not isinstance(calendar_colors, list):
            calendar_colors = [calendar_colors] if calendar_colors else []

        agenda_events = []
        if calendar_urls:
            start = tz.localize(datetime(now.year, now.month, now.day))
            end = start + timedelta(days=AGENDA_DAYS)
            try:
                raw_events = self._fetch_ics_events(calendar_urls, calendar_colors, tz, start, end)
                agenda_events = self._group_events_by_day(raw_events, tz, time_format, AGENDA_DAYS)
            except Exception as e:
                logger.warning(f"Calendar fetch failed: {str(e)}")
        template_params["agenda_events"] = agenda_events

        # Each day's forecast, with its hourly chart or its timed details when that box is
        # chosen. Either is None when the provider has no hourly data for the day, and the
        # card shows the summary box instead.
        forecast_by_date = {f["date"]: f for f in template_params.get("forecast", []) if f.get("date")}
        style = settings.get("agendaForecastStyle", AGENDA_FORECAST_STYLE_CHART)
        day_charts = {}
        day_details = {}
        if style == AGENDA_FORECAST_STYLE_TIMED:
            day_details = template_params.get("day_details", {})
        elif style != AGENDA_FORECAST_STYLE_SUMMARY:
            day_charts = template_params.get("day_charts", {})
        for day_group in agenda_events:
            day_group["forecast"] = forecast_by_date.get(day_group.get("date"))
            has_forecast = day_group["forecast"] is not None
            day_group["chart"] = day_charts.get(day_group.get("date")) if has_forecast else None
            day_group["details"] = day_details.get(day_group.get("date")) if has_forecast else None
        template_params["agenda_chart_wind"] = settings.get("agendaChartWind") == "true"

        template_params["agenda_wx_icons"] = {
            "precip": self.get_plugin_dir("icons/09d.png"),
            "uvi":    self.get_plugin_dir("icons/uvi.png"),
            "wind":   self.get_plugin_dir("icons/wind.png"),
        }

        # Sunrise, sunset and the current UV index under the date.
        data_points_by_label = {dp["label"]: dp for dp in template_params.get("data_points", [])}
        sun_metrics = [data_points_by_label[label] for label in ("Sunrise", "Sunset") if label in data_points_by_label]
        sun_metrics.append({
            "label": "Current UV",
            "measurement": template_params.get("current_uv", "N/A"),
            "unit": "",
            "icon": self.get_plugin_dir("icons/uvi.png"),
        })
        template_params["sun_metrics"] = sun_metrics

        # When the weather was fetched, which can be earlier than this refresh when it
        # comes from the cache. A time on another day names its day.
        if settings.get("displayRefreshTime") == "true":
            when = self._format_time(weather_time, time_format)
            if weather_time.date() != now.date():
                when = f"{weather_time.strftime('%a')} {when}"
            template_params["updated_at_text"] = f"(weather updated {when})"
        else:
            template_params["updated_at_text"] = ""

    # ==================================================================
    # Calendar helpers
    # ==================================================================

    def _format_event_time_range(self, start_dt, end_str, time_format, tz):
        """Return a compact start–end time string for agenda display.

        12h: no leading zero on the hour, :mm only when the minutes aren't zero, and
        a or p after each time (e.g. 7p-8:30p, 11a-1p).
        24h: HH:MM or HH:MM–HH:MM.
        """
        def _fmt_part(dt):
            mins = f":{dt.strftime('%M')}" if dt.minute != 0 else ""
            return f"{dt.hour % 12 or 12}{mins}"

        if time_format == "24h":
            result = start_dt.strftime("%H:%M")
            if end_str:
                try:
                    end_dt = datetime.fromisoformat(end_str)
                    end_dt = end_dt.astimezone(tz) if end_dt.tzinfo else tz.localize(end_dt)
                    result += "–" + end_dt.strftime("%H:%M")
                except Exception:
                    pass
            return result

        start_part = _fmt_part(start_dt)
        start_period = "a" if start_dt.hour < 12 else "p"

        if not end_str:
            return f"{start_part}{start_period}"

        try:
            end_dt = datetime.fromisoformat(end_str)
            end_dt = end_dt.astimezone(tz) if end_dt.tzinfo else tz.localize(end_dt)
        except Exception:
            return f"{start_part}{start_period}"

        end_part = _fmt_part(end_dt)
        end_period = "a" if end_dt.hour < 12 else "p"
        return f"{start_part}{start_period}-{end_part}{end_period}"

    def _event_covered_dates(self, start_str, end_str, tz):
        """Return the list of local dates this event occupies (iCal semantics)."""
        if "T" in start_str:
            ev_start = datetime.fromisoformat(start_str)
            ev_start = tz.localize(ev_start) if ev_start.tzinfo is None else ev_start.astimezone(tz)
            if not end_str or "T" not in end_str:
                return [ev_start.date()]
            ev_end = datetime.fromisoformat(end_str)
            ev_end = tz.localize(ev_end) if ev_end.tzinfo is None else ev_end.astimezone(tz)
            last = ev_end.date()
            if ev_end.time() == time_type(0, 0) and last > ev_start.date():
                last -= timedelta(days=1)
            dates, d = [], ev_start.date()
            while d <= last:
                dates.append(d)
                d += timedelta(days=1)
            return dates
        ev_start_date = date_type.fromisoformat(start_str)
        if not end_str:
            return [ev_start_date]
        try:
            ev_end_date = date_type.fromisoformat(end_str)
        except ValueError:
            return [ev_start_date]
        last = max(ev_end_date - timedelta(days=1), ev_start_date)
        dates, d = [], ev_start_date
        while d <= last:
            dates.append(d)
            d += timedelta(days=1)
        return dates

    def _group_events_by_day(self, events, tz, time_format, agenda_days):
        """Group and sort events by calendar day for agenda display."""
        now = datetime.now(tz)
        today = now.date()

        prepared = []
        for event in events:
            start_str = event.get("start", "")
            end_str = event.get("end")
            try:
                covered = self._event_covered_dates(start_str, end_str, tz)
            except Exception as e:
                logger.warning(f"Failed to parse event date '{start_str}': {e}")
                continue
            timed = "T" in start_str
            ev_start_dt = None
            if timed:
                ev_start_dt = datetime.fromisoformat(start_str)
                ev_start_dt = tz.localize(ev_start_dt) if ev_start_dt.tzinfo is None else ev_start_dt.astimezone(tz)
            prepared.append({
                "covered": set(covered),
                "start_date": covered[0] if covered else None,
                "timed": timed,
                "start_dt": ev_start_dt,
                "end_str": end_str,
                "title": event.get("title", ""),
                "color": event.get("backgroundColor", "#007BFF"),
            })

        days = []
        for i in range(agenda_days):
            target_date = today + timedelta(days=i)
            label = target_date.strftime("%a %-m/%-d").upper()
            date_iso = target_date.isoformat()

            day_events = []
            for p in prepared:
                if target_date not in p["covered"]:
                    continue
                is_start_day = p["timed"] and p["start_date"] == target_date
                if is_start_day:
                    time_str = self._format_event_time_range(
                        p["start_dt"], p["end_str"], time_format, tz
                    )
                    day_events.append({
                        "time": time_str,
                        "title": p["title"],
                        "color": p["color"],
                        "all_day": False,
                        "_sort_key": p["start_dt"].hour * 60 + p["start_dt"].minute,
                    })
                else:
                    day_events.append({
                        "time": "All Day",
                        "title": p["title"],
                        "color": p["color"],
                        "all_day": True,
                    })

            day_events.sort(key=lambda e: (not e["all_day"], e.get("_sort_key", 0)))
            for ev in day_events:
                ev.pop("_sort_key", None)
            days.append({"label": label, "date": date_iso, "events": day_events})
        return days

    def _fetch_ics_events(self, calendar_urls, colors, tz, start_range, end_range):
        parsed_events = []
        padded_colors = list(colors) + ["#007BFF"] * max(0, len(calendar_urls) - len(colors))
        for calendar_url, color in zip(calendar_urls, padded_colors):
            if not calendar_url.strip():
                continue
            cal = self._fetch_calendar(calendar_url)
            events = recurring_ical_events.of(cal).between(start_range, end_range)
            contrast_color = self._get_contrast_color(color)
            for event in events:
                start, end, all_day = self._parse_cal_event(event, tz)
                parsed_event = {
                    "title": str(event.get("summary", "")),
                    "start": start,
                    "backgroundColor": color,
                    "textColor": contrast_color,
                    "allDay": all_day,
                }
                if end:
                    parsed_event["end"] = end
                parsed_events.append(parsed_event)
        return parsed_events

    def _fetch_calendar(self, calendar_url):
        if calendar_url.startswith("webcal://"):
            calendar_url = calendar_url.replace("webcal://", "https://")
        try:
            response = requests.get(calendar_url, timeout=30)
            response.raise_for_status()
            return icalendar.Calendar.from_ical(response.text)
        except Exception as e:
            raise RuntimeError(f"Failed to fetch iCalendar url: {str(e)}")

    def _parse_cal_event(self, event, tz):
        all_day = False
        dtstart = event.decoded("dtstart")
        if isinstance(dtstart, datetime):
            start = dtstart.astimezone(tz).isoformat()
        else:
            start = dtstart.isoformat()
            all_day = True
        end = None
        if "dtend" in event:
            dtend = event.decoded("dtend")
            if isinstance(dtend, datetime):
                end = dtend.astimezone(tz).isoformat()
            else:
                end = dtend.isoformat()
        elif "duration" in event:
            duration = event.decoded("duration")
            end = (dtstart + duration).isoformat()
        return start, end, all_day

    def _get_contrast_color(self, color):
        try:
            r, g, b = ImageColor.getrgb(color)
            yiq = (r * 299 + g * 587 + b * 114) / 1000
            return "#000000" if yiq >= 150 else "#ffffff"
        except Exception:
            return "#ffffff"

    # ==================================================================
    # Weather APIs
    # ==================================================================

    def _get_owm_params(self, api_key, lat, long, units, wind_unit, tz, use_location_tz, time_format):
        """Fetch OpenWeatherMap and return the template params it provides."""
        weather_data = self._get_weather_data(api_key, units, lat, long)
        aqi_data = self._get_air_quality(api_key, lat, long)
        weather_tz = self._parse_timezone(weather_data) if use_location_tz else tz
        return self._parse_owm_data(weather_data, aqi_data, weather_tz, units, wind_unit, time_format, lat)

    def _get_open_meteo_params(self, lat, long, units, wind_unit, tz, use_location_tz, time_format):
        """Fetch Open-Meteo and return the template params it provides.

        With the location's time zone, Open-Meteo picks the zone ("auto") and names it
        in the response; otherwise it is asked for the device's.
        """
        request_tz = "auto" if use_location_tz else tz.zone
        weather_data = self._get_open_meteo_data(lat, long, units, wind_unit, OPEN_METEO_FORECAST_DAYS, request_tz)
        aqi_data = self._get_open_meteo_air_quality(lat, long, request_tz)
        weather_tz = self._open_meteo_timezone(weather_data, tz) if use_location_tz else tz
        template_params = self._parse_open_meteo_data(
            weather_data, aqi_data, weather_tz, units, wind_unit, time_format, lat
        )
        _store_open_meteo_payload(
            _open_meteo_cache_key(lat, long, units, wind_unit, request_tz), weather_data, aqi_data
        )
        return template_params

    def _get_cached_open_meteo_params(self, lat, long, units, wind_unit, tz, use_location_tz, time_format):
        """Render from the last good matching Open-Meteo payload, if available."""
        request_tz = "auto" if use_location_tz else tz.zone
        payload = _load_open_meteo_payload(
            _open_meteo_cache_key(lat, long, units, wind_unit, request_tz)
        )
        if payload is None:
            return None
        weather_data, aqi_data, fetched_at = payload
        weather_tz = self._open_meteo_timezone(weather_data, tz) if use_location_tz else tz
        try:
            template_params = self._parse_open_meteo_data(
                weather_data, aqi_data, weather_tz, units, wind_unit, time_format, lat
            )
            template_params["weather_fetched_at"] = fetched_at
            return template_params
        except Exception as error:
            logger.warning(f"Cached Open-Meteo data could not be used: {error}")
            return None

    def _get_owm_fallback(self, error, device_config, lat, long, units, wind_unit, tz, use_location_tz, time_format):
        """Return OpenWeatherMap template params after an Open-Meteo failure.

        Uses the same time zone choice and leaves the title alone so the screen
        looks as it does under Open-Meteo. Both parsers show standard units as
        Kelvin and metric as °C. Raises if no OpenWeatherMap key is configured or
        it fails too.
        """
        api_key = device_config.load_env_key("OPEN_WEATHER_MAP_SECRET")
        if not api_key:
            raise RuntimeError(f"{error} (no OpenWeatherMap API key configured for fallback)") from error
        logger.warning(f"OpenMeteo request failed: {error}. Falling back to OpenWeatherMap.")
        try:
            return self._get_owm_params(api_key, lat, long, units, wind_unit, tz, use_location_tz, time_format)
        except Exception as owm_error:
            raise RuntimeError(
                f"{error}; fallback to OpenWeatherMap also failed: {owm_error}"
            ) from owm_error

    def _get_weather_data(self, api_key, units, lat, long):
        url = WEATHER_URL.format(lat=lat, long=long, units=units, api_key=api_key)
        response = requests.get(url, timeout=30)
        if not 200 <= response.status_code < 300:
            logger.error(f"Failed to retrieve weather data: {response.content}")
            raise RuntimeError("Failed to retrieve weather data.")
        return response.json()

    def _get_air_quality(self, api_key, lat, long):
        url = AIR_QUALITY_URL.format(lat=lat, long=long, api_key=api_key)
        response = requests.get(url, timeout=30)
        if not 200 <= response.status_code < 300:
            logger.error(f"Failed to get air quality data: {response.content}")
            raise RuntimeError("Failed to retrieve air quality data.")
        return response.json()

    def _get_location(self, api_key, lat, long):
        url = GEOCODING_URL.format(lat=lat, long=long, api_key=api_key)
        response = requests.get(url, timeout=30)
        if not 200 <= response.status_code < 300:
            logger.error(f"Failed to get location: {response.content}")
            raise RuntimeError("Failed to retrieve location.")
        location_data = response.json()[0]
        return f"{location_data.get('name')}, {location_data.get('state', location_data.get('country'))}"

    def _get_open_meteo_data(self, lat, long, units, wind_unit, forecast_days, timezone_name):
        url = OPEN_METEO_FORECAST_URL.format(
            lat=lat, long=long, forecast_days=forecast_days,
            timezone=url_quote(timezone_name, safe=""),
        ) + f"&{OPEN_METEO_UNIT_PARAMS[units]}&wind_speed_unit={OPEN_METEO_WIND_UNITS[wind_unit]}"
        response = _request_open_meteo(url)
        if not 200 <= response.status_code < 300:
            logger.error(f"Failed to retrieve Open-Meteo data: {response.content}")
            raise RuntimeError("Failed to retrieve Open-Meteo weather data.")
        return self._drop_past_days(response.json())

    @staticmethod
    def _drop_past_days(weather_data):
        """Remove the daily rows from before today that `past_days` adds.

        Only the hourly chart wants the past, and it reads it from the hourly rows,
        which stay. The daily rows start at today, as they did before.
        """
        if not isinstance(weather_data, dict):
            return weather_data
        daily = weather_data.get("daily")
        current_time = (weather_data.get("current") or {}).get("time")
        if not isinstance(daily, dict) or not current_time:
            return weather_data
        today = str(current_time)[:10]
        times = daily.get("time") or []
        past = next((i for i, day in enumerate(times) if str(day) >= today), len(times))
        if 0 < past < len(times):
            for key, values in daily.items():
                if isinstance(values, list):
                    daily[key] = values[past:]
        return weather_data

    def _get_open_meteo_air_quality(self, lat, long, timezone_name):
        url = OPEN_METEO_AIR_QUALITY_URL.format(
            lat=lat, long=long, timezone=url_quote(timezone_name, safe=""),
        )
        response = _request_open_meteo(url)
        if not 200 <= response.status_code < 300:
            logger.error(f"Failed to retrieve Open-Meteo air quality: {response.content}")
            raise RuntimeError("Failed to retrieve Open-Meteo air quality data.")
        return response.json()

    # ==================================================================
    # Parsing helpers
    # ==================================================================

    def _parse_timezone(self, weatherdata):
        if "timezone" in weatherdata:
            return pytz.timezone(weatherdata["timezone"])
        raise RuntimeError("Timezone not found in weather data.")

    @staticmethod
    def _open_meteo_timezone(weather_data, fallback):
        """The time zone Open-Meteo chose for its times, or `fallback` if it named none."""
        try:
            return pytz.timezone(weather_data["timezone"])
        except (KeyError, TypeError, pytz.exceptions.UnknownTimeZoneError):
            return fallback

    def _format_time(self, dt, time_format, hour_only=False, include_am_pm=True):
        """Format datetime based on 12h or 24h preference"""
        if time_format == "24h":
            return dt.strftime("%H:00" if hour_only else "%H:%M")
        if include_am_pm:
            fmt = "%I %p" if hour_only else "%I:%M %p"
        else:
            fmt = "%I" if hour_only else "%I:%M"
        return dt.strftime(fmt).lstrip("0")

    def _map_weather_code_to_icon(self, weather_code, is_day):
        mapping = [
            ([0],            "01d"),   # Clear sky
            ([1],            "022d"),  # Mainly clear
            ([2],            "02d"),   # Partly cloudy
            ([3],            "04d"),   # Overcast
            ([51, 61, 80],   "51d"),   # Drizzle, showers, rain: light
            ([53, 63, 81],   "53d"),   # Drizzle, showers, rain: moderate
            ([55, 65, 82],   "09d"),   # Drizzle, showers, rain: heavy
            ([45],           "50d"),   # Fog
            ([48],           "48d"),   # Icy fog
            ([56, 66],       "56d"),   # Light freezing drizzle
            ([57, 67],       "57d"),   # Freezing drizzle
            ([71, 85],       "71d"),   # Snow fall: slight
            ([73],           "73d"),   # Snow fall: moderate
            ([75, 86],       "13d"),   # Snow fall: heavy
            ([77],           "77d"),   # Snow grains
            ([95, 96, 99],   "11d"),   # Thunderstorm, with or without hail
        ]
        icon = "01d"
        for codes, ico in mapping:
            if weather_code in codes:
                icon = ico
                break
        if is_day == 0:
            night_map = {"01d": "01n", "022d": "022n", "02d": "02n", "10d": "10n"}
            icon = night_map.get(icon, icon)
        return icon

    def _get_moon_phase_icon_path(self, phase_name, lat):
        """Path to the moon icon, mirrored in the Southern Hemisphere."""
        if lat < 0:
            flips = {
                "waxingcrescent": "waningcrescent", "waxinggibbous": "waninggibbous",
                "waningcrescent": "waxingcrescent", "waninggibbous": "waxinggibbous",
                "firstquarter": "lastquarter", "lastquarter": "firstquarter",
            }
            phase_name = flips.get(phase_name, phase_name)
        return self.get_plugin_dir(f"icons/{phase_name}.png")

    def _alt_temp(self, temp, units):
        """Return (value_str, unit_str) converted to the other common display unit."""
        if units == "imperial":
            return str(round((temp - 32) * 5 / 9)), "°C"
        elif units == "metric":
            return str(round(temp * 9 / 5 + 32)), "°F"
        else:  # standard (K)
            return str(round(temp - 273.15)), "°C"

    def _format_rain_amount(self, amount, units):
        """Return (amount_str, unit_str) for daily rainfall already in display units,
        or (None, unit_str) when the rounded value is zero.

        Metric/standard: whole mm. Imperial: 1 decimal inches.
        """
        unit_str = "in" if units == "imperial" else "mm"
        if not isinstance(amount, (int, float)):
            return None, unit_str
        if units == "imperial":
            value = round(amount, 1)
            return (f"{value:.1f}", unit_str) if value > 0 else (None, unit_str)
        value = round(amount)
        return (str(value), unit_str) if value > 0 else (None, unit_str)

    @staticmethod
    def _format_visibility(value, divisor, maximum):
        """Visibility in display units to one decimal, marked ≥ at the provider's maximum."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "N/A"
        visibility = value / divisor
        text = f"{visibility:.1f}"
        return "≥" + text if visibility >= maximum else text

    def _get_wind_arrow(self, wind_deg):
        directions = [
            ("↓", 22.5),    # North (N)
            ("↙", 67.5),    # North-East (NE)
            ("←", 112.5),   # East (E)
            ("↖", 157.5),   # South-East (SE)
            ("↑", 202.5),   # South (S)
            ("↗", 247.5),   # South-West (SW)
            ("→", 292.5),   # West (W)
            ("↘", 337.5),   # North-West (NW)
            ("↓", 360.0),   # Wrap back to North
        ]
        wind_deg = wind_deg % 360
        for arrow, upper in directions:
            if wind_deg < upper:
                return arrow
        return "↑"

    @staticmethod
    def _compass_point(wind_deg):
        """The compass point the wind blows from, such as "SW", or "" without a direction."""
        if isinstance(wind_deg, bool) or not isinstance(wind_deg, (int, float)):
            return ""
        return COMPASS_POINTS[int((wind_deg % 360 + 22.5) // 45) % len(COMPASS_POINTS)]

    @staticmethod
    def _hourly_wind(value, factor=1):
        """Round an hourly wind reading for the chart; None when the provider has none."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return round(value * factor)

    def _data_point(self, label, measurement, unit, icon_file, arrow=None):
        data_point = {
            "label": label,
            "measurement": measurement,
            "unit": unit,
            "icon": self.get_plugin_dir(f"icons/{icon_file}"),
        }
        if arrow is not None:
            data_point["arrow"] = arrow
        return data_point

    # --- OpenWeatherMap ---

    def _parse_owm_data(self, weather_data, aqi_data, tz, units, wind_unit, time_format, lat):
        current = weather_data.get("current")
        daily_forecast = weather_data.get("daily", [])
        dt = datetime.fromtimestamp(current.get("dt"), tz=timezone.utc).astimezone(tz)
        current_icon = current.get("weather")[0].get("icon")
        icon_codes_to_preserve = ["01", "02", "10"]
        icon_code = current_icon[:2]
        current_suffix = current_icon[-1]
        if icon_code not in icon_codes_to_preserve and current_icon.endswith("n"):
            current_icon = current_icon.replace("n", "d")

        raw_temp = round(current.get("temp"))
        alt_temp, alt_unit = self._alt_temp(raw_temp, units)
        current_uvi = current.get("uvi")
        data = {
            "current_date": dt.strftime("%A, %B %d"),
            "current_day_icon": self.get_plugin_dir(f"icons/{current_icon}.png"),
            "current_temperature": str(raw_temp),
            "current_temperature_alt": alt_temp,
            "feels_like": str(round(current.get("feels_like", current.get("temp")))),
            "temperature_unit": UNITS[units]["temperature"],
            "temperature_unit_alt": alt_unit,
            "current_uv": f"{current_uvi:.1f}" if current_uvi is not None else "N/A",
            "units": units,
            "time_format": time_format,
        }
        data["forecast"] = self._parse_owm_forecast(daily_forecast, tz, current_suffix, lat, units, wind_unit)
        data["data_points"] = self._parse_owm_data_points(weather_data, aqi_data, tz, units, wind_unit, time_format)
        hourly_list = weather_data.get("hourly", [])
        data["hourly_forecast"] = self._parse_owm_hourly(
            hourly_list, tz, time_format, units, wind_unit, daily_forecast
        )
        return data

    def _parse_owm_forecast(self, daily_forecast, tz, current_suffix, lat, units, wind_unit):
        PHASES = [
            (0.0, "newmoon"), (0.25, "firstquarter"),
            (0.5, "fullmoon"), (0.75, "lastquarter"), (1.0, "newmoon"),
        ]

        def choose_phase(phase):
            for target, name in PHASES:
                if math.isclose(phase, target, abs_tol=1e-3):
                    return name
            if 0.0 < phase < 0.25: return "waxingcrescent"
            if 0.25 < phase < 0.5:  return "waxinggibbous"
            if 0.5 < phase < 0.75:  return "waninggibbous"
            return "waningcrescent"

        forecast = []
        icon_codes_day = ["01", "02", "10"]
        for day in daily_forecast:
            weather_icon = day["weather"][0]["icon"]
            icon_code = weather_icon[:2]
            if icon_code in icon_codes_day:
                weather_icon = weather_icon[:-1] + current_suffix
            elif weather_icon.endswith("n"):
                weather_icon = weather_icon.replace("n", "d")
            weather_icon = f"{icon_code}d"
            moon_phase = float(day["moon_phase"])
            phase_name = choose_phase(moon_phase)
            illum = (1 - math.cos(2 * math.pi * moon_phase)) / 2
            dt = datetime.fromtimestamp(day["dt"], tz=timezone.utc).astimezone(tz)
            high = int(day["temp"]["max"])
            low  = int(day["temp"]["min"])
            high_alt, alt_unit = self._alt_temp(high, units)
            low_alt,  _        = self._alt_temp(low, units)
            wind_speed = day.get("wind_speed", 0)
            if isinstance(wind_speed, (int, float)):
                wind_speed = round(wind_speed * self._owm_wind_factor(units, wind_unit))
            rain_mm = (day.get("rain") or 0) + (day.get("snow") or 0)
            rain_display = (rain_mm / 25.4) if units == "imperial" else rain_mm
            rain_amount, rain_unit = self._format_rain_amount(rain_display, units)
            forecast.append({
                "day": dt.strftime("%a"),
                "date": dt.date().isoformat(),
                "high": high,
                "low":  low,
                "high_alt": high_alt,
                "low_alt":  low_alt,
                "alt_unit": alt_unit,
                "icon": self.get_plugin_dir(f"icons/{weather_icon}.png"),
                "rain_pct": round(day.get("pop", 0) * 100),
                "rain_amount": rain_amount,
                "rain_unit": rain_unit,
                "uvi": round(day.get("uvi", 0)),
                "wind_speed": wind_speed,
                "wind_arrow": self._get_wind_arrow(day.get("wind_deg", 0)),
                "wind_dir": self._compass_point(day.get("wind_deg")),
                "wind_unit": wind_unit,
                "moon_phase_pct": f"{illum * 100:.0f}",
                "moon_phase_icon": self._get_moon_phase_icon_path(phase_name, lat),
            })
        return forecast

    def _parse_owm_hourly(self, hourly_list, tz, time_format, units, wind_unit, daily_forecast):
        hourly = []
        icon_codes_preserve = ["01", "02", "10"]
        sun_map = {}
        for day in daily_forecast:
            day_date = datetime.fromtimestamp(day["dt"], tz=timezone.utc).astimezone(tz).date()
            sun_map[day_date] = (day["sunrise"], day["sunset"])

        wind_factor = self._owm_wind_factor(units, wind_unit)
        # Up to 48 hours: what OpenWeatherMap's hourly forecast covers.
        for hour in hourly_list[:HOURLY_FORECAST_HOURS]:
            dt_epoch = hour.get("dt")
            dt = datetime.fromtimestamp(dt_epoch, tz=timezone.utc).astimezone(tz)
            rain_mm = hour.get("rain", {}).get("1h", 0.0)
            snow_mm = hour.get("snow", {}).get("1h", 0.0)
            total_precip_mm = rain_mm + snow_mm
            sunrise, sunset = sun_map.get(dt.date(), (0, 0))
            is_day = sunrise <= dt_epoch < sunset
            suffix = "d" if is_day else "n"
            raw_icon = hour.get("weather", [{}])[0].get("icon", "01d")
            icon_base = raw_icon[:2]
            icon_name = f"{icon_base}{suffix}" if icon_base in icon_codes_preserve else f"{icon_base}d"
            precip_value = (total_precip_mm / 25.4) if units == "imperial" else total_precip_mm
            feels_like = hour.get("feels_like")
            condition_id = hour.get("weather", [{}])[0].get("id", 0)
            hourly.append({
                "time": self._format_time(dt, time_format, hour_only=True),
                "day": dt.strftime("%a"),
                "temperature": int(hour.get("temp")),
                "wind_speed": self._hourly_wind(hour.get("wind_speed"), wind_factor),
                "wind_gust": self._hourly_wind(hour.get("wind_gust"), wind_factor),
                "precipitation": hour.get("pop"),
                "rain": round(precip_value, 2),
                "uv_index": round(hour.get("uvi", 0), 1),
                "icon": self.get_plugin_dir(f"icons/{icon_name}.png"),
                "feels_like": round(feels_like) if isinstance(feels_like, (int, float)) else None,
                "storm": 200 <= condition_id < 300,
                "fog": condition_id == OWM_FOG_ID,
            })
        return hourly

    def _parse_owm_data_points(self, weather, air_quality, tz, units, wind_unit, time_format):
        current = weather.get("current", {})
        data_points = []

        sunrise_epoch = current.get("sunrise")
        if sunrise_epoch:
            sr_dt = datetime.fromtimestamp(sunrise_epoch, tz=timezone.utc).astimezone(tz)
            data_points.append(self._data_point(
                "Sunrise", self._format_time(sr_dt, time_format, include_am_pm=False),
                "" if time_format == "24h" else sr_dt.strftime("%p"), "sunrise.png"))
        else:
            logger.error("Sunrise not found in OpenWeatherMap response, this is expected for polar areas in midnight sun and polar night periods.")

        sunset_epoch = current.get("sunset")
        if sunset_epoch:
            ss_dt = datetime.fromtimestamp(sunset_epoch, tz=timezone.utc).astimezone(tz)
            data_points.append(self._data_point(
                "Sunset", self._format_time(ss_dt, time_format, include_am_pm=False),
                "" if time_format == "24h" else ss_dt.strftime("%p"), "sunset.png"))
        else:
            logger.error("Sunset not found in OpenWeatherMap response, this is expected for polar areas in midnight sun and polar night periods.")

        # Shown as reported unless it has to be converted to the display unit.
        wind_speed = current.get("wind_speed")
        wind_factor = self._owm_wind_factor(units, wind_unit)
        if wind_factor != 1 and isinstance(wind_speed, (int, float)):
            wind_speed = round(wind_speed * wind_factor, 1)
        data_points.append(self._data_point(
            "Wind", wind_speed, wind_unit, "wind.png", self._get_wind_arrow(current.get("wind_deg", 0))))

        data_points.append(self._data_point("Humidity", current.get("humidity"), "%", "humidity.png"))
        data_points.append(self._data_point("Pressure", current.get("pressure"), "hPa", "pressure.png"))
        data_points.append(self._data_point("UV Index", current.get("uvi"), "", "uvi.png"))

        # Reported in metres.
        if units == "imperial":
            visibility = self._format_visibility(current.get("visibility"), 1609., 6.2)
        else:
            visibility = self._format_visibility(current.get("visibility"), 1000., 10)
        data_points.append(self._data_point("Visibility", visibility, UNITS[units]["distance"], "visibility.png"))

        aqi_list = (air_quality or {}).get("list") or []
        aqi = aqi_list[0].get("main", {}).get("aqi") if aqi_list else None
        if isinstance(aqi, int) and 1 <= aqi <= len(OWM_AQI_LABELS):
            data_points.append(self._data_point("Air Quality", aqi, OWM_AQI_LABELS[aqi - 1], "aqi.png"))
        else:
            data_points.append(self._data_point("Air Quality", "N/A", "", "aqi.png"))

        return data_points

    # --- Open-Meteo ---

    def _parse_open_meteo_data(self, weather_data, aqi_data, tz, units, wind_unit, time_format, lat):
        current = weather_data.get("current", {})
        daily = weather_data.get("daily", {})
        dt = _parse_local(current.get("time"), tz) if current.get("time") else datetime.now(tz)
        weather_code = current.get("weather_code", 0)
        is_day = current.get("is_day", 1)
        current_icon = self._map_weather_code_to_icon(weather_code, is_day)
        temp_offset = 273.15 if units == "standard" else 0.0

        raw_temp = round(current.get("temperature", 0) + temp_offset)
        alt_temp, alt_unit = self._alt_temp(raw_temp, units)
        current_uv = current.get("uv_index")
        data = {
            "current_date": dt.strftime("%A, %B %d"),
            "current_day_icon": self.get_plugin_dir(f"icons/{current_icon}.png"),
            "current_temperature": str(raw_temp),
            "current_temperature_alt": alt_temp,
            "feels_like": str(round(current.get("apparent_temperature", current.get("temperature", 0)) + temp_offset)),
            "temperature_unit": UNITS[units]["temperature"],
            "temperature_unit_alt": alt_unit,
            "current_uv": f"{current_uv:.1f}" if current_uv is not None else "N/A",
            "units": units,
            "time_format": time_format,
        }
        data["forecast"] = self._parse_open_meteo_forecast(daily, units, wind_unit, lat)
        data["data_points"] = self._parse_open_meteo_data_points(weather_data, aqi_data, units, wind_unit, tz, time_format)
        hourly_data = weather_data.get("hourly", {})
        data["hourly_past"], data["hourly_forecast"] = self._parse_open_meteo_hours(
            hourly_data, units, tz, time_format,
            daily.get("sunrise", []), daily.get("sunset", [])
        )
        data["day_charts"] = self._parse_open_meteo_day_charts(hourly_data, units)
        data["day_details"] = self._parse_open_meteo_day_details(hourly_data, data["forecast"], units, time_format, tz)
        return data

    def _parse_open_meteo_forecast(self, daily_data, units, wind_unit, lat):
        """Daily forecast, with the moon phase and illumination worked out locally by 'astral'."""
        times = daily_data.get("time", [])
        weather_codes = daily_data.get("weathercode", [])
        temp_max = daily_data.get("temperature_2m_max", [])
        temp_min = daily_data.get("temperature_2m_min", [])
        pop_max = daily_data.get("precipitation_probability_max", [])
        uvi_max = daily_data.get("uv_index_max", [])
        wind_speed_max = daily_data.get("wind_speed_10m_max", [])
        wind_dir_dom = daily_data.get("wind_direction_10m_dominant", [])
        precip_sum = daily_data.get("precipitation_sum", [])
        if units == "standard":
            temp_max = [T + 273.15 for T in temp_max]
            temp_min = [T + 273.15 for T in temp_min]

        forecast = []
        for i in range(len(times)):
            local_date = date_type.fromisoformat(times[i])
            code = weather_codes[i] if i < len(weather_codes) else 0
            icon_name = self._map_weather_code_to_icon(code, is_day=1)
            try:
                phase_age = moon.phase(local_date)
                phase_name = get_moon_phase_name(phase_age)
                LUNAR_CYCLE_DAYS = 29.530588853
                illum_pct = (1 - math.cos(2 * math.pi * phase_age / LUNAR_CYCLE_DAYS)) / 2 * 100
            except Exception as e:
                logger.error(f"Error calculating moon phase for {local_date}: {e}")
                illum_pct, phase_name = 0, "newmoon"

            high = int(temp_max[i]) if i < len(temp_max) else 0
            low  = int(temp_min[i]) if i < len(temp_min) else 0
            high_alt, alt_unit = self._alt_temp(high, units)
            low_alt,  _        = self._alt_temp(low, units)
            wind_speed = wind_speed_max[i] if i < len(wind_speed_max) else 0
            wind_deg = wind_dir_dom[i] if i < len(wind_dir_dom) else 0
            rain_total = precip_sum[i] if i < len(precip_sum) else 0
            rain_amount, rain_unit = self._format_rain_amount(rain_total, units)
            forecast.append({
                "day": local_date.strftime("%a"),
                "date": local_date.isoformat(),
                "high": high,
                "low":  low,
                "high_alt": high_alt,
                "low_alt":  low_alt,
                "alt_unit": alt_unit,
                "icon": self.get_plugin_dir(f"icons/{icon_name}.png"),
                "rain_pct": int(pop_max[i]) if i < len(pop_max) else 0,
                "rain_amount": rain_amount,
                "rain_unit": rain_unit,
                "uvi": round(uvi_max[i]) if i < len(uvi_max) else 0,
                # Already in the display unit, as requested from Open-Meteo.
                "wind_speed": round(wind_speed) if isinstance(wind_speed, (int, float)) else wind_speed,
                "wind_arrow": self._get_wind_arrow(wind_deg) if isinstance(wind_deg, (int, float)) else "",
                "wind_dir": self._compass_point(wind_deg),
                "wind_unit": wind_unit,
                "moon_phase_pct": f"{illum_pct:.0f}",
                "moon_phase_icon": self._get_moon_phase_icon_path(phase_name, lat),
            })
        return forecast

    def _open_meteo_hourly_start_index(self, times, tz):
        """Return the index of the first hourly entry at or after the current hour."""
        now = datetime.now(tz)
        for i, time_str in enumerate(times):
            try:
                dt_h = _parse_local(time_str, tz)
                if dt_h.date() == now.date() and dt_h.hour >= now.hour:
                    return i
                if dt_h.date() > now.date():
                    break
            except ValueError:
                logger.warning(f"Could not parse time string {time_str} in hourly data.")
                continue
        return 0

    def _parse_open_meteo_hourly(self, hourly_data, units, tz, time_format, sunrises, sunsets):
        """The hours from now on, as far as the longest chart reaches."""
        return self._parse_open_meteo_hours(hourly_data, units, tz, time_format, sunrises, sunsets)[1]

    def _parse_open_meteo_hours(self, hourly_data, units, tz, time_format, sunrises, sunsets):
        """Hourly entries either side of now: (the hours before it, the hours from it on).

        The first list holds up to CHART_PAST_HOURS hours, oldest first, and is shorter
        when the data starts later than that. The second starts at the current hour.
        """
        times = hourly_data.get("time", [])
        temperatures = hourly_data.get("temperature_2m", [])
        apparent = hourly_data.get("apparent_temperature", [])
        temp_offset = 273.15 if units == "standard" else 0
        precip_probs = hourly_data.get("precipitation_probability", [])
        rain = hourly_data.get("precipitation", [])
        codes = hourly_data.get("weather_code", [])
        cape = hourly_data.get("cape", [])
        uv_index = hourly_data.get("uv_index", [])
        # Already in the display unit, as requested from Open-Meteo.
        wind_speeds = hourly_data.get("wind_speed_10m", [])
        wind_gusts = hourly_data.get("wind_gusts_10m", [])

        sun_map = {}
        for sr_s, ss_s in zip(sunrises, sunsets):
            sr_dt = _parse_local(sr_s, tz)
            ss_dt = _parse_local(ss_s, tz)
            sun_map[sr_dt.date()] = (sr_dt, ss_dt)

        def number(values, i):
            value = values[i] if i < len(values) else None
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

        def entry(i):
            dt = _parse_local(times[i], tz)
            sunrise, sunset = sun_map.get(dt.date(), (None, None))
            is_day = 1 if (sunrise and sunset and sunrise <= dt < sunset) else 0
            code = codes[i] if i < len(codes) else 0
            icon_name = self._map_weather_code_to_icon(code, is_day)
            feels_like = apparent[i] if i < len(apparent) else None
            # Open-Meteo gives each hour's rain and chance of rain at the hour's end.
            # Read them from the next time so each entry holds the hour it starts.
            chance = number(precip_probs, i + 1)
            return {
                "time": self._format_time(dt, time_format, True),
                "day": dt.strftime("%a"),
                "temperature": int(temperatures[i] + temp_offset) if i < len(temperatures) else 0,
                "wind_speed": self._hourly_wind(wind_speeds[i] if i < len(wind_speeds) else None),
                "wind_gust": self._hourly_wind(wind_gusts[i] if i < len(wind_gusts) else None),
                "precipitation": chance / 100,
                "rain": number(rain, i + 1),
                "uv_index": round(uv_index[i], 1) if i < len(uv_index) else 0,
                "icon": self.get_plugin_dir(f"icons/{icon_name}.png"),
                "feels_like": round(feels_like + temp_offset) if isinstance(feels_like, (int, float)) else None,
                "storm": code in STORM_CODES or (number(cape, i) >= STORM_CAPE and chance >= STORM_RAIN_CHANCE),
                "fog": code in FOG_CODES,
            }

        start_index = self._open_meteo_hourly_start_index(times, tz)
        past = [entry(i) for i in range(max(0, start_index - CHART_PAST_HOURS), start_index)]
        future = [entry(i) for i in range(start_index, min(start_index + HOURLY_FORECAST_HOURS, len(times)))]
        return past, future

    def _parse_open_meteo_day_charts(self, hourly_data, units):
        """Hourly series for the chart in each agenda card, keyed by ISO date.

        A day runs from DAY_CHART_START_HOUR to the midnight that ends it. Days the
        hourly data does not cover in full are left out, and their cards fall back to
        the summary box.
        """
        index_by_time = {}
        for i, time_str in enumerate(hourly_data.get("time", [])):
            try:
                index_by_time[datetime.fromisoformat(time_str)] = i
            except (TypeError, ValueError):
                continue

        def number(key, index):
            values = hourly_data.get(key, [])
            value = values[index] if index < len(values) else None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return value

        temp_offset = 273.15 if units == "standard" else 0.0
        charts = {}
        for day in sorted({dt.date() for dt in index_by_time}):
            first_hour = datetime.combine(day, time_type(DAY_CHART_START_HOUR))
            hours = [first_hour + timedelta(hours=h) for h in range(24 - DAY_CHART_START_HOUR + 1)]
            indices = [index_by_time.get(hour) for hour in hours]
            if any(index is None for index in indices):
                continue
            temperatures = [number("temperature_2m", index) for index in indices]
            if any(t is None for t in temperatures):
                continue
            charts[day.isoformat()] = {
                "temperature": [round(t + temp_offset, 1) for t in temperatures],
                "rain": [round(number("precipitation", index) or 0, 2) for index in indices],
                "uv": [round(number("uv_index", index) or 0, 1) for index in indices],
                # Already in the display unit, as requested from Open-Meteo.
                "wind": [self._hourly_wind(number("wind_speed_10m", index)) for index in indices],
                "gust": [self._hourly_wind(number("wind_gusts_10m", index)) for index in indices],
            }
        return charts

    def _parse_open_meteo_day_details(self, hourly_data, forecast, units, time_format, tz):
        """What the timed forecast box says about each day, keyed by ISO date.

        Each day has an outlook line, and the details shown beside its rain chance and
        wind speed. Days the hourly data does not cover from midnight to 11 PM
        are left out, and their cards fall back to the summary box.
        """
        index_by_time = {}
        for i, time_str in enumerate(hourly_data.get("time", [])):
            try:
                index_by_time[datetime.fromisoformat(time_str)] = i
            except (TypeError, ValueError):
                continue

        def number(key, index):
            values = hourly_data.get(key, [])
            value = values[index] if index is not None and index < len(values) else None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return value

        details = {}
        for day in forecast:
            day_date = date_type.fromisoformat(day["date"])
            indices = [index_by_time.get(datetime.combine(day_date, time_type(hour))) for hour in range(24)]
            if any(index is None for index in indices):
                continue
            codes = [number("weather_code", index) for index in indices]
            # Open-Meteo gives each hour's rain at the hour's end: read it from the next time.
            rain = [number("precipitation", index + 1) or 0 for index in indices]
            gusts = [gust for gust in (number("wind_gusts_10m", index) for index in indices) if gust is not None]
            wet_hours = [hour for hour in range(24) if rain[hour] >= WET_HOUR_RAIN[units]]
            details[day["date"]] = {
                "outlook": self._day_outlook(codes, wet_hours, time_format),
                "rain": self._rain_detail(day, wet_hours, day_date, time_format, tz),
                "wind": self._wind_detail(day, gusts),
            }
        return details

    def _day_outlook(self, codes, wet_hours, time_format):
        """One line on the day's weather from DAY_CHART_START_HOUR to midnight.

        Rain or snow in those hours gives its kind and when, such as "Showers 2 PM–6 PM".
        A dry day gives its sky, and the one change in it that best explains the day,
        such as "Fog until 10 AM, then sunny" or "Cloudy, clearing from 1 PM".
        """
        window = list(range(DAY_CHART_START_HOUR, 24))
        wet = [hour for hour in wet_hours if hour in window]
        if wet:
            noun = self._precipitation_noun(
                [codes[hour] for hour in wet] + [codes[hour + 1] for hour in wet if hour + 1 < 24]
            )
            first, end = wet[0], wet[-1] + 1
            if len(wet) >= OUTLOOK_STEADY_SHARE * len(window):
                return f"{noun} all day"
            if end - first >= 10 and len(wet) < (end - first) / 2:
                return f"{noun} on and off"
            return f"{noun} {self._hour_span(first, end, time_format)}"

        skies = [SKY_BY_CODE.get(codes[hour], "cloudy") for hour in window]
        steady_sky, steady_hours = Counter(skies).most_common(1)[0]
        # The single change that leaves the most hours matching their part's sky.
        change = None
        for split in range(2, len(skies) - 1):
            before, before_hours = Counter(skies[:split]).most_common(1)[0]
            after, after_hours = Counter(skies[split:]).most_common(1)[0]
            if before != after and (change is None or before_hours + after_hours > change[0]):
                change = (before_hours + after_hours, window[split], before, after)

        needed = OUTLOOK_STEADY_SHARE * len(skies)
        if steady_hours >= needed:
            return f"{self._sky_word(steady_sky, window[0])} all day"
        if change is None or change[0] < needed:
            return self._mostly_sky(steady_sky, steady_hours, len(skies))

        _, hour, before, after = change
        when = self._clock_hour(hour, time_format)
        if before == "fog":
            return f"Fog until {when}, then {self._sky_word(after, hour).lower()}"
        if after == "fog":
            return f"{self._sky_word(before, window[0])}, fog from {when}"
        if SKY_CLOUDINESS[after] < SKY_CLOUDINESS[before]:
            return f"{self._sky_word(before, window[0])}, clearing from {when}"
        return f"{self._sky_word(before, window[0])}, clouding over from {when}"

    @staticmethod
    def _sky_word(sky, from_hour):
        """The sky as a word: a clear sky is sunny unless it only starts in the evening."""
        if sky == "clear":
            return "Clear" if from_hour >= 18 else "Sunny"
        return {"partly": "Partly cloudy", "cloudy": "Cloudy", "fog": "Fog"}[sky]

    def _mostly_sky(self, sky, sky_hours, hours):
        """The outlook for a day whose sky keeps changing."""
        if sky_hours * 2 < hours:
            return "Mixed sun and cloud"
        return {"clear": "Mostly sunny", "partly": "Partly cloudy",
                "cloudy": "Mostly cloudy", "fog": "Fog at times"}[sky]

    @staticmethod
    def _precipitation_noun(codes):
        """What falls, from the weather codes of the wet hours."""
        kinds = {PRECIPITATION_BY_CODE.get(code) for code in codes} - {None}
        if "thunder" in kinds:
            return "Thunderstorms"
        if "snow" in kinds:
            return "Rain and snow" if kinds & {"rain", "showers", "drizzle"} else "Snow"
        if "showers" in kinds:
            return "Showers"
        if "drizzle" in kinds and "rain" not in kinds:
            return "Drizzle"
        return "Rain"

    @staticmethod
    def _clock_hour(hour, time_format):
        """An hour of the day for a sentence: "1 PM", "13:00", or midnight for 24.

        A no-break space keeps the hour and its AM or PM on one line.
        """
        if hour % 24 == 0 and hour > 0:
            return "midnight"
        if time_format == "24h":
            return f"{hour:02d}:00"
        return f"{hour % 12 or 12}\u00a0{'AM' if hour < 12 else 'PM'}"

    def _hour_span(self, start, end, time_format):
        """Hours start to end for a sentence: "2 PM–6 PM", "8 PM–midnight" or "14:00–18:00".

        Word joiners around the dash keep the range on one line.
        """
        return f"{self._clock_hour(start, time_format)}\u2060–\u2060{self._clock_hour(end, time_format)}"

    def _hour_range(self, start, end, day_date, time_format, tz):
        """Hours start to end in short form: "2p-6p", as event times are written, or "14–18"."""
        if time_format == "24h":
            return f"{start:02d}–{end:02d}"
        start_dt = datetime.combine(day_date, time_type(start))
        end_dt = datetime.combine(day_date, time_type(0)) + timedelta(hours=end)
        return self._format_event_time_range(start_dt, end_dt.isoformat(), time_format, tz)

    def _rain_detail(self, day, wet_hours, day_date, time_format, tz):
        """The day's rain total and when it falls, or that it stays dry."""
        if not wet_hours:
            return "dry all day" if day.get("rain_pct", 0) < DRY_DAY_MAX_CHANCE else "little expected"
        when = self._hour_range(wet_hours[0], wet_hours[-1] + 1, day_date, time_format, tz)
        if day.get("rain_amount"):
            return f"{day['rain_amount']} {day['rain_unit']} · {when}"
        return when

    @staticmethod
    def _wind_detail(day, gusts):
        """Where the wind blows from, and the day's strongest gust."""
        parts = []
        if day.get("wind_dir"):
            parts.append(day["wind_dir"])
        if gusts:
            parts.append(f"gusts {round(max(gusts))}")
        return " · ".join(parts)

    def _parse_open_meteo_data_points(self, weather_data, aqi_data, units, wind_unit, tz, time_format):
        """Current conditions for the classic layout's metrics."""
        daily_data = weather_data.get("daily", {})
        current_data = weather_data.get("current", {})
        hourly_data = weather_data.get("hourly", {})
        aqi_hourly = (aqi_data or {}).get("hourly", {})
        current_hour = datetime.now(tz).hour
        data_points = []

        def value_now(hourly, key):
            """This hour's value of an hourly series, or None."""
            values = hourly.get(key, [])
            for i, time_str in enumerate(hourly.get("time", [])):
                try:
                    if _parse_local(time_str, tz).hour == current_hour:
                        return values[i] if i < len(values) else None
                except ValueError:
                    logger.warning(f"Could not parse time string {time_str} for {key}.")
                    continue
            return None

        sunrise_times = daily_data.get("sunrise", [])
        if sunrise_times:
            sr_dt = _parse_local(sunrise_times[0], tz)
            data_points.append(self._data_point(
                "Sunrise", self._format_time(sr_dt, time_format, include_am_pm=False),
                "" if time_format == "24h" else sr_dt.strftime("%p"), "sunrise.png"))
        else:
            logger.error("Sunrise not found in Open-Meteo response, this is expected for polar areas in midnight sun and polar night periods.")

        sunset_times = daily_data.get("sunset", [])
        if sunset_times:
            ss_dt = _parse_local(sunset_times[0], tz)
            data_points.append(self._data_point(
                "Sunset", self._format_time(ss_dt, time_format, include_am_pm=False),
                "" if time_format == "24h" else ss_dt.strftime("%p"), "sunset.png"))
        else:
            logger.error("Sunset not found in Open-Meteo response, this is expected for polar areas in midnight sun and polar night periods.")

        # Already in the display unit, as requested from Open-Meteo.
        data_points.append(self._data_point(
            "Wind", current_data.get("windspeed", 0), wind_unit, "wind.png",
            self._get_wind_arrow(current_data.get("winddirection", 0))))

        humidity = value_now(hourly_data, "relative_humidity_2m")
        data_points.append(self._data_point(
            "Humidity", int(humidity) if humidity is not None else "N/A", "%", "humidity.png"))

        pressure = value_now(hourly_data, "surface_pressure")
        data_points.append(self._data_point(
            "Pressure", int(pressure) if pressure is not None else "N/A", "hPa", "pressure.png"))

        uv_index = value_now(aqi_hourly, "uv_index")
        data_points.append(self._data_point(
            "UV Index", uv_index if uv_index is not None else "N/A", "", "uvi.png"))

        # Reported in feet with imperial units, metres otherwise.
        if units == "imperial":
            visibility = self._format_visibility(value_now(hourly_data, "visibility"), 5280., 6.2)
        else:
            visibility = self._format_visibility(value_now(hourly_data, "visibility"), 1000., 10.)
        data_points.append(self._data_point("Visibility", visibility, UNITS[units]["distance"], "visibility.png"))

        aqi = value_now(aqi_hourly, "european_aqi")
        if aqi is None:
            data_points.append(self._data_point("Air Quality", "N/A", "", "aqi.png"))
        else:
            aqi = round(aqi, 1)
            scale = OPEN_METEO_AQI_LABELS[int(min(aqi // 20, 5))] if aqi else ""
            data_points.append(self._data_point("Air Quality", aqi, scale, "aqi.png"))

        return data_points
