import requests
import logging
from datetime import datetime, timedelta, timezone, date as date_type, time as time_type
from astral import moon
import pytz
import math
import icalendar
import recurring_ical_events
from PIL import ImageColor
from urllib.parse import quote as url_quote

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)


def _parse_local(iso_str, tz):
    """Parse an Open-Meteo naive ISO string and attach the configured tz.

    Open-Meteo is queried with a specific timezone, so returned strings are
    already in that tz but carry no offset marker. pytz's `localize` attaches
    the tz without relying on the OS timezone.
    """
    return tz.localize(datetime.fromisoformat(iso_str))

# ---------------------------------------------------------------------------
# Moon phase helper (mirrored from weather plugin)
# ---------------------------------------------------------------------------

def get_moon_phase_name(phase_age: float) -> str:
    PHASES_THRESHOLDS = [
        (1.0, "newmoon"), (7.0, "waxingcrescent"), (8.5, "firstquarter"),
        (14.0, "waxinggibbous"), (15.5, "fullmoon"), (22.0, "waninggibbous"),
        (23.5, "lastquarter"), (29.0, "waningcrescent"),
    ]
    for threshold, phase_name in PHASES_THRESHOLDS:
        if phase_age <= threshold:
            return phase_name
    return "newmoon"


# ---------------------------------------------------------------------------
# Constants (mirrored from weather plugin)
# ---------------------------------------------------------------------------

UNITS = {
    "standard": {"temperature": "K",  "speed": "km/h", "distance": "km"},
    "metric":   {"temperature": "°C", "speed": "km/h", "distance": "km"},
    "imperial": {"temperature": "°F", "speed": "mph", "distance": "mi"},
}

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
    "&daily=weathercode,temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_probability_max,uv_index_max"
    ",wind_speed_10m_max,wind_direction_10m_dominant,precipitation_sum"
    "&current=temperature,windspeed,winddirection,is_day,precipitation"
    ",weather_code,apparent_temperature"
    "&timezone={timezone}&models=best_match&forecast_days={forecast_days}"
)
OPEN_METEO_AIR_QUALITY_URL = (
    "https://air-quality-api.open-meteo.com/v1/air-quality"
    "?latitude={lat}&longitude={long}"
    "&hourly=european_aqi,uv_index,uv_index_clear_sky&timezone={timezone}"
)
OPEN_METEO_UNIT_PARAMS = {
    "standard": "temperature_unit=celsius&wind_speed_unit=kmh&precipitation_unit=mm",
    "metric":   "temperature_unit=celsius&wind_speed_unit=kmh&precipitation_unit=mm",
    "imperial": "temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch",
}


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class WeatherAgenda(BasePlugin):
    """Combines a condensed left-justified weather panel with a right-side
    calendar agenda list."""

    # ------------------------------------------------------------------
    # Settings template
    # ------------------------------------------------------------------

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = True
        template_params["api_key"] = {
            "required": True,
            "service": "OpenWeatherMap",
            "expected_key": "OPEN_WEATHER_MAP_SECRET",
        }
        return template_params

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def generate_image(self, settings, device_config):
        lat = float(settings.get("latitude") or 0)
        long = float(settings.get("longitude") or 0)
        if not lat or not long:
            raise RuntimeError("Latitude and Longitude are required.")

        units = settings.get("units", "imperial")
        if units not in ("metric", "imperial", "standard"):
            raise RuntimeError("Units must be metric, imperial, or standard.")

        weather_provider = settings.get("weatherProvider", "OpenMeteo")
        title = settings.get("customTitle", "")

        timezone_name = device_config.get_config("timezone", default="America/New_York")
        time_format = device_config.get_config("time_format", default="12h")
        tz = pytz.timezone(timezone_name)

        # --- Fetch & parse weather ---
        try:
            if weather_provider == "OpenWeatherMap":
                api_key = device_config.load_env_key("OPEN_WEATHER_MAP_SECRET")
                if not api_key:
                    raise RuntimeError("Open Weather Map API Key not configured.")
                weather_data = self._get_weather_data(api_key, units, lat, long)
                aqi_data = self._get_air_quality(api_key, lat, long)
                if settings.get("titleSelection", "location") == "location":
                    title = self._get_location(api_key, lat, long)
                if settings.get("weatherTimeZone", "locationTimeZone") == "locationTimeZone":
                    wtz = self._parse_timezone(weather_data)
                    template_params = self._parse_owm_data(weather_data, aqi_data, wtz, units, time_format, lat)
                else:
                    template_params = self._parse_owm_data(weather_data, aqi_data, tz, units, time_format, lat)
            elif weather_provider == "OpenMeteo":
                weather_data = self._get_open_meteo_data(lat, long, units, 8, timezone_name)
                aqi_data = self._get_open_meteo_air_quality(lat, long, timezone_name)
                template_params = self._parse_open_meteo_data(weather_data, aqi_data, tz, units, time_format, lat)
            else:
                raise RuntimeError(f"Unknown weather provider: {weather_provider}")
            template_params["title"] = title
        except Exception as e:
            logger.error(f"{weather_provider} request failed: {str(e)}")
            raise RuntimeError(f"{weather_provider} request failure, please check logs.")

        # --- Fetch & parse calendar ---
        calendar_urls = settings.get("calendarURLs[]", [])
        calendar_colors = settings.get("calendarColors[]", [])
        if not isinstance(calendar_urls, list):
            calendar_urls = [calendar_urls] if calendar_urls else []
        if not isinstance(calendar_colors, list):
            calendar_colors = [calendar_colors] if calendar_colors else []

        agenda_days = 4

        if calendar_urls:
            now = datetime.now(tz)
            start = tz.localize(datetime(now.year, now.month, now.day))
            end = start + timedelta(days=agenda_days)
            try:
                raw_events = self._fetch_ics_events(calendar_urls, calendar_colors, tz, start, end)
                template_params["agenda_events"] = self._group_events_by_day(
                    raw_events, tz, time_format, agenda_days
                )
            except Exception as e:
                logger.warning(f"Calendar fetch failed: {str(e)}")
                template_params["agenda_events"] = []
        else:
            template_params["agenda_events"] = []

        # --- Attach forecast to agenda days by matching date ---
        forecast = template_params.get("forecast", [])
        forecast_by_date = {f["date"]: f for f in forecast if f.get("date")}
        for day_group in template_params.get("agenda_events", []):
            day_group["forecast"] = forecast_by_date.get(day_group.get("date"))

        # --- Shared icon paths for agenda weather rows ---
        template_params["agenda_wx_icons"] = {
            "precip": self.get_plugin_dir("icons/09d.png"),
            "uvi":    self.get_plugin_dir("icons/uvi.png"),
            "wind":   self.get_plugin_dir("icons/wind.png"),
        }

        # --- Select metrics shown beneath the current temperature ---
        data_points_by_label = {dp["label"]: dp for dp in template_params.get("data_points", [])}
        below_labels = []
        template_params["metrics_below"] = [data_points_by_label[l] for l in below_labels if l in data_points_by_label]

        # --- Sunrise / sunset / peak UV for the header area ---
        sun_labels = ["Sunrise", "Peak UV", "Sunset"]
        template_params["sun_metrics"] = [data_points_by_label[l] for l in sun_labels if l in data_points_by_label]

        # --- Dimensions & render ---
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        template_params["plugin_settings"] = settings

        now = datetime.now(tz)
        if settings.get("displayUpdatedAt") == "true":
            if time_format == "24h":
                updated_at_text = now.strftime("(updated at %H:%M)")
            else:
                time_part = now.strftime("%I:%M %p").lstrip("0")
                updated_at_text = f"(updated at {time_part})"
        else:
            updated_at_text = ""
        template_params["updated_at_text"] = updated_at_text

        image = self.render_image(
            dimensions, "weather_agenda.html", "weather_agenda.css", template_params
        )
        if not image:
            raise RuntimeError("Failed to take screenshot, please check logs.")
        return image

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
    # Weather API methods
    # ==================================================================

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

    def _get_open_meteo_data(self, lat, long, units, forecast_days, timezone_name):
        unit_params = OPEN_METEO_UNIT_PARAMS[units]
        url = OPEN_METEO_FORECAST_URL.format(
            lat=lat, long=long, forecast_days=forecast_days,
            timezone=url_quote(timezone_name, safe=""),
        ) + f"&{unit_params}"
        response = requests.get(url, timeout=30)
        if not 200 <= response.status_code < 300:
            logger.error(f"Failed to retrieve Open-Meteo data: {response.content}")
            raise RuntimeError("Failed to retrieve Open-Meteo weather data.")
        return response.json()

    def _get_open_meteo_air_quality(self, lat, long, timezone_name):
        url = OPEN_METEO_AIR_QUALITY_URL.format(
            lat=lat, long=long, timezone=url_quote(timezone_name, safe=""),
        )
        response = requests.get(url, timeout=30)
        if not 200 <= response.status_code < 300:
            logger.error(f"Failed to retrieve Open-Meteo air quality: {response.content}")
            raise RuntimeError("Failed to retrieve Open-Meteo air quality data.")
        return response.json()

    # ==================================================================
    # Weather parsing helpers
    # ==================================================================

    def _parse_timezone(self, weatherdata):
        if "timezone" in weatherdata:
            return pytz.timezone(weatherdata["timezone"])
        raise RuntimeError("Timezone not found in weather data.")

    def _format_time(self, dt, time_format, hour_only=False, include_am_pm=True):
        if time_format == "24h":
            return dt.strftime("%H:00" if hour_only else "%H:%M")
        if include_am_pm:
            fmt = "%I %p" if hour_only else "%I:%M %p"
        else:
            fmt = "%I" if hour_only else "%I:%M"
        return dt.strftime(fmt).lstrip("0")

    def _map_weather_code_to_icon(self, weather_code, is_day):
        mapping = [
            ([0],            "01d"),
            ([1],            "022d"),
            ([2],            "02d"),
            ([3],            "04d"),
            ([51, 61, 80],   "51d"),
            ([53, 63, 81],   "53d"),
            ([55, 65, 82],   "09d"),
            ([45],           "50d"),
            ([48],           "48d"),
            ([56, 66],       "56d"),
            ([57, 67],       "57d"),
            ([71, 85],       "71d"),
            ([73],           "73d"),
            ([75, 86],       "13d"),
            ([77],           "77d"),
            ([95, 96, 99],   "11d"),
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

    def _get_wind_arrow(self, wind_deg):
        directions = [
            ("↓", 22.5), ("↙", 67.5), ("←", 112.5), ("↖", 157.5),
            ("↑", 202.5), ("↗", 247.5), ("→", 292.5), ("↘", 337.5), ("↓", 360.0),
        ]
        wind_deg = wind_deg % 360
        for arrow, upper in directions:
            if wind_deg < upper:
                return arrow
        return "↑"

    # --- OWM data parsing ---

    def _parse_owm_data(self, weather_data, aqi_data, tz, units, time_format, lat):
        current = weather_data.get("current")
        daily_forecast = weather_data.get("daily", [])
        dt = datetime.fromtimestamp(current.get("dt"), tz=timezone.utc).astimezone(tz)
        current_icon = current.get("weather")[0].get("icon")
        icon_codes_to_preserve = ["01", "02", "10"]
        icon_code = current_icon[:2]
        current_suffix = current_icon[-1]
        if icon_code not in icon_codes_to_preserve and current_icon.endswith("n"):
            current_icon = current_icon.replace("n", "d")

        raw_temp   = round(current.get("temp"))
        alt_temp,  alt_unit = self._alt_temp(raw_temp, units)
        data = {
            "current_date": dt.strftime("%A, %B %d"),
            "current_day_icon": self.get_plugin_dir(f"icons/{current_icon}.png"),
            "current_temperature": str(raw_temp),
            "current_temperature_alt": alt_temp,
            "temperature_unit": UNITS[units]["temperature"],
            "temperature_unit_alt": alt_unit,
            "units": units,
            "time_format": time_format,
        }
        data["forecast"] = self._parse_owm_forecast(daily_forecast, tz, current_suffix, lat, units)
        data["data_points"] = self._parse_owm_data_points(weather_data, aqi_data, tz, units, time_format)
        hourly_list = weather_data.get("hourly", [])
        data["hourly_forecast"] = self._parse_owm_hourly(
            hourly_list, tz, time_format, units, daily_forecast
        )
        sun_dt_list = []
        for day in daily_forecast:
            sr = day.get("sunrise")
            ss = day.get("sunset")
            if sr:
                sun_dt_list.append((datetime.fromtimestamp(sr, tz=timezone.utc).astimezone(tz), "sunrise"))
            if ss:
                sun_dt_list.append((datetime.fromtimestamp(ss, tz=timezone.utc).astimezone(tz), "sunset"))
        if hourly_list:
            start_dt = datetime.fromtimestamp(hourly_list[0]["dt"], tz=timezone.utc).astimezone(tz)
            hours_count = min(24, len(hourly_list))
            data["sun_events"] = self._compute_sun_events(start_dt, hours_count, sun_dt_list, time_format)
        else:
            data["sun_events"] = []
        return data

    def _parse_owm_forecast(self, daily_forecast, tz, current_suffix, lat, units="imperial"):
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
            wind_speed_raw = day.get("wind_speed", 0)
            wind_deg = day.get("wind_deg", 0)
            if units != "imperial" and isinstance(wind_speed_raw, (int, float)):
                wind_speed_raw = wind_speed_raw * 3.6
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
                "wind_speed": round(wind_speed_raw) if isinstance(wind_speed_raw, (int, float)) else wind_speed_raw,
                "wind_arrow": self._get_wind_arrow(wind_deg),
                "wind_unit": UNITS[units]["speed"],
                "moon_phase_pct": f"{illum * 100:.0f}",
                "moon_phase_icon": self._get_moon_phase_icon_path(phase_name, lat),
            })
        return forecast

    def _compute_sun_events(self, start_dt, hours_count, sun_dt_list, time_format):
        """Return sun events that fall within the hourly chart's time window.

        sun_dt_list is a sequence of (datetime, type) tuples where type is
        "sunrise" or "sunset". Each returned event includes a fractional
        ``hour_offset`` representing its position on the chart's index axis
        (0 → first hour shown, hours_count - 1 → last hour shown), so the
        renderer can interpolate between hourly tick positions.
        """
        if hours_count <= 0 or start_dt is None:
            return []
        max_offset = hours_count - 1
        events = []
        for evt_dt, evt_type in sun_dt_list:
            offset = (evt_dt - start_dt).total_seconds() / 3600
            if offset < 0 or offset > max_offset:
                continue
            events.append({
                "type": evt_type,
                "icon": self.get_plugin_dir(f"icons/{evt_type}.png"),
                "time": self._format_time(evt_dt, time_format),
                "hour_offset": round(offset, 4),
            })
        events.sort(key=lambda e: e["hour_offset"])
        return events

    def _parse_owm_hourly(self, hourly_list, tz, time_format, units, daily_forecast):
        hourly = []
        icon_codes_preserve = ["01", "02", "10"]
        sun_map = {}
        for day in daily_forecast:
            day_date = datetime.fromtimestamp(day["dt"], tz=timezone.utc).astimezone(tz).date()
            sun_map[day_date] = (day["sunrise"], day["sunset"])

        for hour in hourly_list[:24]:
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
                "temperature": int(hour.get("temp")),
                "precipitation": hour.get("pop"),
                "rain": round(precip_value, 2),
                "uv_index": 0,
                "icon": self.get_plugin_dir(f"icons/{icon_name}.png"),
            })
        return hourly

    def _parse_owm_data_points(self, weather, air_quality, tz, units, time_format):
        data_points = []

        def add(label, measurement, unit, icon_file, arrow=None):
            dp = {"label": label, "measurement": measurement, "unit": unit,
                  "icon": self.get_plugin_dir(f"icons/{icon_file}")}
            if arrow is not None:
                dp["arrow"] = arrow
            data_points.append(dp)

        sunrise_epoch = weather.get("current", {}).get("sunrise")
        if sunrise_epoch:
            sr_dt = datetime.fromtimestamp(sunrise_epoch, tz=timezone.utc).astimezone(tz)
            add("Sunrise", self._format_time(sr_dt, time_format, include_am_pm=False),
                "" if time_format == "24h" else sr_dt.strftime("%p"), "sunrise.png")

        sunset_epoch = weather.get("current", {}).get("sunset")
        if sunset_epoch:
            ss_dt = datetime.fromtimestamp(sunset_epoch, tz=timezone.utc).astimezone(tz)
            add("Sunset", self._format_time(ss_dt, time_format, include_am_pm=False),
                "" if time_format == "24h" else ss_dt.strftime("%p"), "sunset.png")

        wind_speed = weather.get("current", {}).get("wind_speed", 0)
        wind_deg = weather.get("current", {}).get("wind_deg", 0)
        if units != "imperial" and isinstance(wind_speed, (int, float)):
            wind_speed = wind_speed * 3.6
        add("Wind", round(wind_speed) if isinstance(wind_speed, (int, float)) else wind_speed,
            UNITS[units]["speed"], "wind.png", self._get_wind_arrow(wind_deg))

        daily_list = weather.get("daily", [])
        today_pop = round(daily_list[0].get("pop", 0) * 100) if daily_list else 0
        add("Precip", today_pop, "%", "09d.png")

        peak_uvi = daily_list[0].get("uvi") if daily_list else weather.get("current", {}).get("uvi")
        add("Peak UV", round(peak_uvi) if peak_uvi is not None else "N/A", "", "uvi.png")


        return data_points

    # --- Open-Meteo data parsing ---

    def _parse_open_meteo_data(self, weather_data, aqi_data, tz, units, time_format, lat):
        current = weather_data.get("current", {})
        daily = weather_data.get("daily", {})
        dt = _parse_local(current.get("time"), tz) if current.get("time") else datetime.now(tz)
        weather_code = current.get("weather_code", 0)
        is_day = current.get("is_day", 1)
        current_icon = self._map_weather_code_to_icon(weather_code, is_day)
        temp_offset = 273.15 if units == "standard" else 0.0

        raw_temp  = round(current.get("temperature", 0) + temp_offset)
        alt_temp,  alt_unit = self._alt_temp(raw_temp, units)
        data = {
            "current_date": dt.strftime("%A, %B %d"),
            "current_day_icon": self.get_plugin_dir(f"icons/{current_icon}.png"),
            "current_temperature": str(raw_temp),
            "current_temperature_alt": alt_temp,
            "temperature_unit": UNITS[units]["temperature"],
            "temperature_unit_alt": alt_unit,
            "units": units,
            "time_format": time_format,
        }
        data["forecast"] = self._parse_open_meteo_forecast(daily, units, tz, is_day, lat)
        data["data_points"] = self._parse_open_meteo_data_points(weather_data, aqi_data, units, tz, time_format)
        hourly_data = weather_data.get("hourly", {})
        data["hourly_forecast"] = self._parse_open_meteo_hourly(
            hourly_data, units, tz, time_format,
            daily.get("sunrise", []), daily.get("sunset", [])
        )
        times = hourly_data.get("time", [])
        sun_dt_list = []
        for sr_s in daily.get("sunrise", []):
            try:
                sun_dt_list.append((_parse_local(sr_s, tz), "sunrise"))
            except ValueError:
                continue
        for ss_s in daily.get("sunset", []):
            try:
                sun_dt_list.append((_parse_local(ss_s, tz), "sunset"))
            except ValueError:
                continue
        if times:
            start_index = self._open_meteo_hourly_start_index(times, tz)
            start_dt = _parse_local(times[start_index], tz) if start_index < len(times) else None
            hours_count = max(0, min(24, len(times) - start_index))
            data["sun_events"] = self._compute_sun_events(start_dt, hours_count, sun_dt_list, time_format)
        else:
            data["sun_events"] = []
        return data

    def _parse_open_meteo_forecast(self, daily_data, units, tz, is_day, lat):
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
            dt = datetime.fromisoformat(times[i])
            code = weather_codes[i] if i < len(weather_codes) else 0
            icon_name = self._map_weather_code_to_icon(code, is_day=1)
            target_date = dt.date() + timedelta(days=1)
            try:
                phase_age = moon.phase(target_date)
                phase_name = get_moon_phase_name(phase_age)
                LUNAR_CYCLE = 29.530588853
                illum_pct = (1 - math.cos(2 * math.pi * phase_age / LUNAR_CYCLE)) / 2 * 100
            except Exception as e:
                logger.error(f"Moon phase error for {target_date}: {e}")
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
                "day": dt.strftime("%a"),
                "date": dt.date().isoformat(),
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
                "wind_speed": round(wind_speed) if isinstance(wind_speed, (int, float)) else wind_speed,
                "wind_arrow": self._get_wind_arrow(wind_deg) if isinstance(wind_deg, (int, float)) else "",
                "wind_unit": UNITS[units]["speed"],
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

        s_times = times[start_index:]
        s_temps = temperatures[start_index:]
        s_probs = precip_probs[start_index:]
        s_rain = rain[start_index:]
        s_codes = codes[start_index:]
        s_uv = uv_index[start_index:]

        for i in range(min(24, len(s_times))):
            dt = _parse_local(s_times[i], tz)
            sunrise, sunset = sun_map.get(dt.date(), (None, None))
            is_day = 1 if (sunrise and sunset and sunrise <= dt < sunset) else 0
            code = s_codes[i] if i < len(s_codes) else 0
            icon_name = self._map_weather_code_to_icon(code, is_day)
            hourly.append({
                "time": self._format_time(dt, time_format, True),
                "temperature": int(s_temps[i]) if i < len(s_temps) else 0,
                "precipitation": (s_probs[i] / 100) if i < len(s_probs) else 0,
                "rain": s_rain[i] if i < len(s_rain) else 0,
                "uv_index": round(s_uv[i], 1) if i < len(s_uv) else 0,
                "icon": self.get_plugin_dir(f"icons/{icon_name}.png"),
            })
        return hourly

    def _parse_open_meteo_data_points(self, weather_data, aqi_data, units, tz, time_format):
        data_points = []
        daily_data = weather_data.get("daily", {})
        current_data = weather_data.get("current", {})
        hourly_data = weather_data.get("hourly", {})
        current_time = datetime.now(tz)

        def add(label, measurement, unit, icon_file, arrow=None):
            dp = {"label": label, "measurement": measurement, "unit": unit,
                  "icon": self.get_plugin_dir(f"icons/{icon_file}")}
            if arrow is not None:
                dp["arrow"] = arrow
            data_points.append(dp)

        def find_current_hourly(key):
            times = hourly_data.get("time", [])
            values = hourly_data.get(key, [])
            for i, time_str in enumerate(times):
                try:
                    if _parse_local(time_str, tz).hour == current_time.hour:
                        return values[i] if i < len(values) else "N/A"
                except ValueError:
                    continue
            return "N/A"

        sunrise_times = daily_data.get("sunrise", [])
        if sunrise_times:
            sr_dt = _parse_local(sunrise_times[0], tz)
            add("Sunrise", self._format_time(sr_dt, time_format, include_am_pm=False),
                "" if time_format == "24h" else sr_dt.strftime("%p"), "sunrise.png")

        sunset_times = daily_data.get("sunset", [])
        if sunset_times:
            ss_dt = _parse_local(sunset_times[0], tz)
            add("Sunset", self._format_time(ss_dt, time_format, include_am_pm=False),
                "" if time_format == "24h" else ss_dt.strftime("%p"), "sunset.png")

        wind_speed = current_data.get("windspeed", 0)
        wind_deg = current_data.get("winddirection", 0)
        add("Wind", round(wind_speed) if isinstance(wind_speed, (int, float)) else wind_speed,
            UNITS[units]["speed"], "wind.png", self._get_wind_arrow(wind_deg))

        pop_max_list = daily_data.get("precipitation_probability_max", [])
        precip_chance = int(pop_max_list[0]) if pop_max_list else 0
        add("Precip", precip_chance, "%", "09d.png")

        uv_max_list = daily_data.get("uv_index_max", [])
        peak_uv = round(uv_max_list[0]) if uv_max_list else "N/A"
        add("Peak UV", peak_uv, "", "uvi.png")


        return data_points
