import logging
import math
import time
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
# Days shown in the agenda layout, starting today.
AGENDA_DAYS = 4
# First hour drawn in each agenda card's day chart. The chart ends at the midnight
# that closes the day.
DAY_CHART_START_HOUR = 6

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

# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

UNITS = {
    "standard": {"temperature": "K", "distance": "km"},
    "metric": {"temperature": "°C", "distance": "km"},
    "imperial": {"temperature": "°F", "distance": "mi"},
}
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
    ",wind_speed_10m,wind_gusts_10m"
    "&daily=weathercode,temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_probability_max,uv_index_max"
    ",wind_speed_10m_max,wind_direction_10m_dominant,precipitation_sum"
    "&current=temperature,windspeed,winddirection,is_day,precipitation"
    ",weather_code,apparent_temperature,uv_index"
    "&timezone={timezone}&models=best_match&forecast_days={forecast_days}"
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
        time_format = device_config.get_config("time_format", default="12h")
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
                    template_params = self._get_owm_fallback(
                        e, device_config, lat, long, units, wind_unit, tz, use_location_tz, time_format
                    )
                    backup_source = "OpenWeatherMap"
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
        template_params["hourly_chart"] = self._chart_points(
            template_params.get("hourly_forecast", []), self._chart_hours(settings) // CHART_BARS
        )

        now = datetime.now(tz)
        if time_format == "24h":
            template_params["last_refresh_time"] = now.strftime("%Y-%m-%d %H:%M")
        else:
            template_params["last_refresh_time"] = now.strftime("%Y-%m-%d %I:%M %p")

        if layout == LAYOUT_AGENDA:
            self._add_agenda_params(template_params, settings, tz, time_format, now)

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
        and their highest chance of rain. The last point only closes the line, so its
        bar is not drawn. Fewer points come back when the hourly data runs out first.
        """
        points = []
        for start in range(0, min(len(hourly), (CHART_BARS + 1) * step), step):
            hours = hourly[start:start + step]
            points.append({
                **hours[0],
                "rain": round(sum(h.get("rain") or 0 for h in hours), 2),
                "precipitation": max(h.get("precipitation") or 0 for h in hours),
            })
        return points

    # ==================================================================
    # Agenda layout
    # ==================================================================

    def _add_agenda_params(self, template_params, settings, tz, time_format, now):
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

        # Each day's forecast, and its hourly chart unless the summary box is chosen.
        forecast_by_date = {f["date"]: f for f in template_params.get("forecast", []) if f.get("date")}
        show_day_charts = settings.get("agendaForecastStyle", AGENDA_FORECAST_STYLE_CHART) != AGENDA_FORECAST_STYLE_SUMMARY
        day_charts = template_params.get("day_charts", {}) if show_day_charts else {}
        for day_group in agenda_events:
            day_group["forecast"] = forecast_by_date.get(day_group.get("date"))
            # That day's hourly chart; None when the provider has no hourly data for it.
            day_group["chart"] = day_charts.get(day_group.get("date")) if day_group["forecast"] else None
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

        if settings.get("displayRefreshTime") == "true":
            if time_format == "24h":
                template_params["updated_at_text"] = now.strftime("(updated at %H:%M)")
            else:
                template_params["updated_at_text"] = f"(updated at {now.strftime('%I:%M %p').lstrip('0')})"
        else:
            template_params["updated_at_text"] = ""

    # ==================================================================
    # Calendar helpers
    # ==================================================================

    def _format_event_time_range(self, start_dt, end_str, time_format, tz):
        """Return a compact start–end time string for agenda display.

        12h rules:
          - No leading zero on hour; omit :mm when minutes == 0.
          - Same AM/PM period → suffix only on end  (e.g. 7-8:30a).
          - Crosses noon      → suffix on both       (e.g. 11a-1p).
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

        if start_period == end_period:
            return f"{start_part}-{end_part}{end_period}"
        else:
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
        return self._parse_open_meteo_data(weather_data, aqi_data, weather_tz, units, wind_unit, time_format, lat)

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
        return response.json()

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
        data["hourly_forecast"] = self._parse_open_meteo_hourly(
            hourly_data, units, tz, time_format,
            daily.get("sunrise", []), daily.get("sunset", [])
        )
        data["day_charts"] = self._parse_open_meteo_day_charts(hourly_data, units)
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
        hourly = []
        times = hourly_data.get("time", [])
        temperatures = hourly_data.get("temperature_2m", [])
        if units == "standard":
            temperatures = [t + 273.15 for t in temperatures]
        precip_probs = hourly_data.get("precipitation_probability", [])
        rain = hourly_data.get("precipitation", [])
        codes = hourly_data.get("weather_code", [])

        sun_map = {}
        for sr_s, ss_s in zip(sunrises, sunsets):
            sr_dt = _parse_local(sr_s, tz)
            ss_dt = _parse_local(ss_s, tz)
            sun_map[sr_dt.date()] = (sr_dt, ss_dt)

        start_index = self._open_meteo_hourly_start_index(times, tz)

        uv_index = hourly_data.get("uv_index", [])
        # Already in the display unit, as requested from Open-Meteo.
        wind_speeds = hourly_data.get("wind_speed_10m", [])
        wind_gusts = hourly_data.get("wind_gusts_10m", [])

        s_times = times[start_index:]
        s_temps = temperatures[start_index:]
        s_probs = precip_probs[start_index:]
        s_rain = rain[start_index:]
        s_codes = codes[start_index:]
        s_uv = uv_index[start_index:]
        s_wind = wind_speeds[start_index:]
        s_gust = wind_gusts[start_index:]

        def number(values, i):
            value = values[i] if i < len(values) else None
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

        for i in range(min(HOURLY_FORECAST_HOURS, len(s_times))):
            dt = _parse_local(s_times[i], tz)
            sunrise, sunset = sun_map.get(dt.date(), (None, None))
            is_day = 1 if (sunrise and sunset and sunrise <= dt < sunset) else 0
            code = s_codes[i] if i < len(s_codes) else 0
            icon_name = self._map_weather_code_to_icon(code, is_day)
            hourly.append({
                "time": self._format_time(dt, time_format, True),
                "day": dt.strftime("%a"),
                "temperature": int(s_temps[i]) if i < len(s_temps) else 0,
                "wind_speed": self._hourly_wind(s_wind[i] if i < len(s_wind) else None),
                "wind_gust": self._hourly_wind(s_gust[i] if i < len(s_gust) else None),
                # Open-Meteo gives each hour's rain and chance of rain at the hour's end.
                # Read them from the next time so each entry holds the hour it starts.
                "precipitation": number(s_probs, i + 1) / 100,
                "rain": number(s_rain, i + 1),
                "uv_index": round(s_uv[i], 1) if i < len(s_uv) else 0,
                "icon": self.get_plugin_dir(f"icons/{icon_name}.png"),
            })
        return hourly

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
