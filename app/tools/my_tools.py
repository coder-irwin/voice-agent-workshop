"""
my_tools.py — ✏️  YOUR PLAYGROUND. Add your own voice-callable tools here!

How it works (3 rules):
  1. Write a normal Python function.
  2. Put @tool on top of it.
  3. Write a clear docstring — the AI reads it to decide WHEN to use your tool.

Restart the server and say it out loud. Done — no other file to edit.

Tips:
  • The first docstring line = what the tool does (be specific!)
  • "Args:" lines describe each parameter so the AI fills them correctly
  • Always return a SHORT string — the voice agent will say it back to you
  • Use `async def` if you call a browser or network; plain `def` is fine otherwise
"""

# ── Standard library ─────────────────────────────────────────────────
import asyncio
import logging
import random

# ── Third-party (all already in requirements.txt) ────────────────────
import aiohttp

from .registry import tool

# ── Module-level logger shared by weather helpers ────────────────────
_weather_logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Existing tools (DO NOT TOUCH)
# ─────────────────────────────────────────────────────────────────────

@tool
def roll_dice(sides: int = 6) -> str:
    """Roll a dice and report the number that came up.

    Args:
        sides: how many sides the dice has (default 6)
    """
    return f"The dice rolled {random.randint(1, sides)}"


# ── Ideas for the workshop (pick one and build it!) ─────────────────
#   • tell_joke()                 -> return a random joke from a list
#   • open_maps(place: str)       -> use browser.page() to open Google Maps
#   • play_spotify(song: str)     -> open open.spotify.com/search/<song>
#   • take_screenshot()           -> page.screenshot(path="shot.png")
#   • convert_currency(...)       -> call a free public API with aiohttp


# ─────────────────────────────────────────────────────────────────────
# Weather tool — Open-Meteo (free, no API key needed)
# ─────────────────────────────────────────────────────────────────────

# WMO weather interpretation codes → short spoken phrase
_WMO_DESCRIPTIONS: dict[int, str] = {
    0:  "clear skies",
    1:  "mainly clear skies",
    2:  "partly cloudy skies",
    3:  "overcast skies",
    45: "foggy conditions",
    48: "foggy conditions with rime",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "heavy drizzle",
    56: "freezing light drizzle",
    57: "freezing heavy drizzle",
    61: "light rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "freezing light rain",
    67: "freezing heavy rain",
    71: "light snow",
    73: "moderate snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light rain showers",
    81: "moderate rain showers",
    82: "heavy rain showers",
    85: "light snow showers",
    86: "heavy snow showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "severe thunderstorms with heavy hail",
}

_GEO_URL     = "https://geocoding-api.open-meteo.com/v1/search"
_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT     = aiohttp.ClientTimeout(total=8, connect=3)


def _wmo_description(code: int) -> str:
    """Map a WMO weather_code integer to a short spoken phrase."""
    return _WMO_DESCRIPTIONS.get(code, "unsettled weather")


async def _geocode(session: aiohttp.ClientSession, city: str) -> dict | None:
    """
    Geocode a city name via Open-Meteo.
    Returns the first result dict, or None if the city is not found.
    Raises aiohttp.ClientError or asyncio.TimeoutError on network failure.
    """
    params = {"name": city, "count": 1, "language": "en", "format": "json"}
    async with session.get(_GEO_URL, params=params, timeout=_TIMEOUT) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    results = data.get("results")
    return results[0] if results else None


async def _fetch_weather(
    session: aiohttp.ClientSession, lat: float, lon: float, unit_key: str
) -> dict:
    """
    Fetch current weather from Open-Meteo forecast endpoint.
    Returns the 'current' sub-dict of the JSON response.
    Raises aiohttp.ClientError or asyncio.TimeoutError on network failure.
    """
    params: dict = {
        "latitude":    lat,
        "longitude":   lon,
        "current":     "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code",
        "forecast_days": 1,
    }
    if unit_key == "fahrenheit":
        params["temperature_unit"] = "fahrenheit"
    async with session.get(_WEATHER_URL, params=params, timeout=_TIMEOUT) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    return data["current"]


@tool
async def get_weather(city: str = "", units: str = "celsius") -> str:
    """Get the current weather for a city. Use when the user asks about weather, temperature, or rain in a place; leave city empty if no city was said.

    Args:
        city: city to get weather for, e.g. Jalandhar, Delhi, London. Leave empty only if the user did not name a city.
        units: celsius (default) or fahrenheit
    """
    # ── 1. Safe input coercion (guard against None or non-string from the LLM) ──
    city_raw   = str(city  or "").strip()
    unit_key   = "fahrenheit" if str(units or "").strip().lower() == "fahrenheit" else "celsius"
    unit_label = unit_key.capitalize()   # "Celsius" or "Fahrenheit"

    # Handle "City, Region" inputs — geocode only the city part
    city_clean = (city_raw.split(",")[0].strip() or city_raw)

    # ── 2. Input validation ───────────────────────────────────────────
    if not city_clean:
        return "Which city would you like the weather for?"
    if len(city_clean) > 100:
        return "That city name is too long. Could you try a shorter name?"
    if not any(ch.isalpha() for ch in city_clean):
        return "I didn't catch a valid city name. Could you say it again?"

    # ── 3. Call APIs ──────────────────────────────────────────────────
    try:
        async with aiohttp.ClientSession() as session:

            # Step A: geocode
            try:
                geo = await _geocode(session, city_clean)
            except asyncio.TimeoutError:
                _weather_logger.warning("[weather] geocoding timed out for city=%r", city_clean)
                return "Sorry, I couldn't retrieve the weather right now. Please try again."
            except aiohttp.ClientError as exc:
                _weather_logger.warning("[weather] geocoding network error: %s", exc)
                return "Sorry, I couldn't retrieve the weather right now. Please try again."

            if geo is None:
                return "I couldn't find that location. Could you check the city name?"

            lat          = geo.get("latitude")
            lon          = geo.get("longitude")
            display_name = geo.get("name", city_clean)
            country      = geo.get("country", "")

            if lat is None or lon is None:
                _weather_logger.warning("[weather] geocoding returned no lat/lon for city=%r", city_clean)
                return "I couldn't find that location. Could you check the city name?"

            location_phrase = f"{display_name}, {country}" if country else display_name

            # Step B: fetch current weather
            try:
                current = await _fetch_weather(session, lat, lon, unit_key)
            except asyncio.TimeoutError:
                _weather_logger.warning("[weather] forecast timed out for city=%r", city_clean)
                return "Sorry, I couldn't retrieve the weather right now. Please try again."
            except aiohttp.ClientError as exc:
                _weather_logger.warning("[weather] forecast network error: %s", exc)
                return "Sorry, I couldn't retrieve the weather right now. Please try again."

    except Exception as exc:
        _weather_logger.error("[weather] unexpected error: %s", exc, exc_info=True)
        return "Sorry, I couldn't retrieve the weather right now. Please try again."

    # ── 4. Parse response (treat missing/null fields gracefully) ──────
    try:
        temp_raw     = current.get("temperature_2m")
        code_raw     = current.get("weather_code")
        feels_raw    = current.get("apparent_temperature")
        humidity_raw = current.get("relative_humidity_2m")

        if temp_raw is None or code_raw is None:
            _weather_logger.warning("[weather] API missing temperature or code for city=%r", city_clean)
            return "Sorry, I couldn't retrieve the weather right now. Please try again."

        temp        = round(float(temp_raw))
        description = _wmo_description(int(code_raw))

        # ── 5. Build spoken reply (no symbols, no markdown) ───────────
        reply = (
            f"It is currently {temp} degrees {unit_label} in {location_phrase}, "
            f"with {description}."
        )

        extras: list[str] = []
        if feels_raw is not None:
            extras.append(f"It feels like {round(float(feels_raw))} degrees")
        if humidity_raw is not None:
            extras.append(f"Humidity is {round(float(humidity_raw))} percent")

        if len(extras) == 2:
            reply += f" {extras[0]}, and {extras[1].lower()}."
        elif len(extras) == 1:
            reply += f" {extras[0]}."

        return reply

    except (KeyError, ValueError, TypeError) as exc:
        _weather_logger.error("[weather] failed to parse API response: %s", exc, exc_info=True)
        return "Sorry, I couldn't retrieve the weather right now. Please try again."
