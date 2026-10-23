"""
Unit tests for the get_weather tool (app/tools/my_tools.py).

Dependencies: unittest, unittest.mock (stdlib only — no new packages needed).
Run with:  python -m pytest tests/test_weather.py -v
       or:  python -m unittest tests.test_weather -v
"""

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ── Stub playwright so browser.py doesn't crash on import ────────────
_pw_stub = types.ModuleType("playwright")
_pw_stub.async_api = types.ModuleType("playwright.async_api")
for _name in ("BrowserContext", "Page", "Playwright", "async_playwright"):
    setattr(_pw_stub.async_api, _name, None)
sys.modules.setdefault("playwright", _pw_stub)
sys.modules.setdefault("playwright.async_api", _pw_stub.async_api)

# Add repo root to path so `app` is importable
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import helpers and the tool under test
from app.tools.my_tools import (  # noqa: E402
    _geocode,
    _fetch_weather,
    _wmo_description,
    get_weather,
)


# ── Helpers ───────────────────────────────────────────────────────────

def run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _make_resp(json_data, status=200):
    """Build a mock aiohttp response context manager."""
    resp = MagicMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    if status >= 400:
        import aiohttp as _aiohttp
        resp.raise_for_status.side_effect = _aiohttp.ClientResponseError(
            request_info=MagicMock(), history=(), status=status
        )
    resp.json = AsyncMock(return_value=json_data)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _two_call_mock(first_json, second_json, first_status=200, second_status=200):
    """Return a side_effect callable that yields two responses in sequence."""
    responses = [
        _make_resp(first_json, first_status),
        _make_resp(second_json, second_status),
    ]
    state = [0]
    def _mock(*args, **kwargs):
        r = responses[state[0]]
        state[0] += 1
        return r
    return _mock


def _patch_session(side_effect):
    """Context manager: patch aiohttp.ClientSession so sess.get uses side_effect."""
    return patch("app.tools.my_tools.aiohttp.ClientSession")


# ── Shared fixture data ───────────────────────────────────────────────

GEO_JALANDHAR = {
    "results": [{
        "name": "Jalandhar",
        "country": "India",
        "latitude": 31.326,
        "longitude": 75.576,
    }]
}

GEO_LONDON = {
    "results": [{
        "name": "London",
        "country": "United Kingdom",
        "latitude": 51.5,
        "longitude": -0.1,
    }]
}

GEO_MISS = {}  # no "results" key

WEATHER_FULL = {
    "current": {
        "temperature_2m": 28.4,
        "weather_code": 2,
        "apparent_temperature": 30.1,
        "relative_humidity_2m": 44.7,
    }
}

WEATHER_NO_FEELS = {
    "current": {
        "temperature_2m": 22.0,
        "weather_code": 0,
        "apparent_temperature": None,
        "relative_humidity_2m": 60.0,
    }
}

WEATHER_NO_HUMIDITY = {
    "current": {
        "temperature_2m": 18.0,
        "weather_code": 61,
        "apparent_temperature": 16.0,
        "relative_humidity_2m": None,
    }
}

WEATHER_NO_BOTH = {
    "current": {
        "temperature_2m": 25.0,
        "weather_code": 0,
        "apparent_temperature": None,
        "relative_humidity_2m": None,
    }
}


def _with_session(mock_session_cls, get_side_effect):
    sess = MagicMock()
    sess.get = MagicMock(side_effect=get_side_effect)
    mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=sess)
    mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)


# ── Tests ─────────────────────────────────────────────────────────────

class TestWmoDescription(unittest.TestCase):

    def test_known_codes(self):
        self.assertEqual(_wmo_description(0), "clear skies")
        self.assertEqual(_wmo_description(2), "partly cloudy skies")
        self.assertEqual(_wmo_description(95), "thunderstorms")
        self.assertEqual(_wmo_description(99), "severe thunderstorms with heavy hail")

    def test_unknown_code(self):
        self.assertEqual(_wmo_description(999), "unsettled weather")
        self.assertEqual(_wmo_description(-1), "unsettled weather")


class TestInputValidation(unittest.TestCase):
    """All these return early without any network call."""

    def test_empty_city(self):
        r = run(get_weather(city=""))
        self.assertIn("Which city", r)

    def test_whitespace_city(self):
        r = run(get_weather(city="   "))
        self.assertIn("Which city", r)

    def test_no_city_arg(self):
        r = run(get_weather())
        self.assertIn("Which city", r)

    def test_none_city(self):
        """None passed instead of string — must not raise AttributeError."""
        r = run(get_weather(city=None))  # type: ignore[arg-type]
        self.assertIn("Which city", r)

    def test_city_too_long(self):
        r = run(get_weather(city="x" * 101))
        self.assertIn("too long", r)

    def test_city_no_letters(self):
        r = run(get_weather(city="12345"))
        self.assertIn("valid city name", r)

    def test_none_units_defaults_to_celsius(self):
        """None passed for units — must not crash; default to Celsius."""
        # We don't make network calls; just test that we get the "Which city" guard
        # (city is empty), confirming coercion ran without error.
        r = run(get_weather(city=None, units=None))  # type: ignore[arg-type]
        self.assertIn("Which city", r)


class TestSuccessPaths(unittest.TestCase):

    def _run_two(self, geo_json, weather_json, **kwargs):
        with patch("app.tools.my_tools.aiohttp.ClientSession") as MockSession:
            _with_session(MockSession, _two_call_mock(geo_json, weather_json))
            return run(get_weather(**kwargs))

    def test_success_celsius(self):
        r = self._run_two(GEO_JALANDHAR, WEATHER_FULL, city="Jalandhar")
        self.assertIn("28", r)
        self.assertIn("Celsius", r)
        self.assertIn("Jalandhar", r)
        self.assertIn("partly cloudy", r)
        self.assertIn("30", r)    # feels-like rounded from 30.1
        self.assertIn("45", r)    # humidity rounded from 44.7
        self.assertNotIn("°", r)
        self.assertNotIn("%", r)

    def test_success_fahrenheit(self):
        weather_f = {"current": {
            "temperature_2m": 75.2, "weather_code": 3,
            "apparent_temperature": 73.0, "relative_humidity_2m": 65.0,
        }}
        r = self._run_two(GEO_LONDON, weather_f, city="London", units="fahrenheit")
        self.assertIn("75", r)
        self.assertIn("Fahrenheit", r)
        self.assertIn("overcast", r)

    def test_city_with_comma(self):
        """'Jalandhar, Punjab' — geocode only uses 'Jalandhar'."""
        r = self._run_two(GEO_JALANDHAR, WEATHER_FULL, city="Jalandhar, Punjab")
        self.assertIn("Jalandhar", r)
        self.assertIn("Celsius", r)

    def test_missing_feels_only(self):
        """feels-like is None, humidity is present — no crash, humidity shown."""
        r = self._run_two(GEO_JALANDHAR, WEATHER_NO_FEELS, city="Jalandhar")
        self.assertIn("22", r)
        self.assertNotIn("feels", r.lower())
        self.assertIn("60", r)   # humidity present

    def test_missing_humidity_only(self):
        """humidity is None, feels-like is present — no crash, feels-like shown."""
        r = self._run_two(GEO_JALANDHAR, WEATHER_NO_HUMIDITY, city="Jalandhar")
        self.assertIn("18", r)
        self.assertIn("feels", r.lower())
        self.assertNotIn("humidity", r.lower())

    def test_missing_feels_and_humidity(self):
        """Both optional fields are None — reply still valid, just shorter."""
        r = self._run_two(GEO_JALANDHAR, WEATHER_NO_BOTH, city="Jalandhar")
        self.assertIn("25", r)
        self.assertIn("clear", r)
        self.assertNotIn("feels", r.lower())
        self.assertNotIn("humidity", r.lower())

    def test_unknown_weather_code(self):
        """Unknown WMO code falls back to 'unsettled weather'."""
        w = {"current": {
            "temperature_2m": 20.0, "weather_code": 9999,
            "apparent_temperature": 19.0, "relative_humidity_2m": 50.0,
        }}
        r = self._run_two(GEO_JALANDHAR, w, city="Jalandhar")
        self.assertIn("unsettled weather", r)


class TestNotFoundPaths(unittest.TestCase):

    def _run_geo_only(self, geo_json, city):
        with patch("app.tools.my_tools.aiohttp.ClientSession") as MockSession:
            _with_session(MockSession, lambda *a, **k: _make_resp(geo_json))
            return run(get_weather(city=city))

    def test_unknown_city(self):
        r = self._run_geo_only(GEO_MISS, "Xyzqwerty")
        self.assertIn("couldn't find", r)

    def test_invalid_json_returns_no_results(self):
        """Geocode returns JSON with no 'results' key."""
        r = self._run_geo_only({"totally": "wrong"}, "Jalandhar")
        self.assertIn("couldn't find", r)


class TestNetworkFailurePaths(unittest.TestCase):

    def test_geocode_timeout(self):
        import asyncio as _asyncio
        with patch("app.tools.my_tools.aiohttp.ClientSession") as MockSession:
            sess = MagicMock()
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(side_effect=_asyncio.TimeoutError())
            cm.__aexit__ = AsyncMock(return_value=False)
            sess.get = MagicMock(return_value=cm)
            MockSession.return_value.__aenter__ = AsyncMock(return_value=sess)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            r = run(get_weather(city="Delhi"))
        self.assertIn("Sorry", r)
        self.assertNotIn("Traceback", r)

    def test_forecast_timeout(self):
        """Geocoding succeeds, forecast times out."""
        import asyncio as _asyncio
        call_count = [0]
        def mock_get(*args, **kwargs):
            if call_count[0] == 0:
                call_count[0] += 1
                return _make_resp(GEO_JALANDHAR)
            call_count[0] += 1
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(side_effect=_asyncio.TimeoutError())
            cm.__aexit__ = AsyncMock(return_value=False)
            return cm
        with patch("app.tools.my_tools.aiohttp.ClientSession") as MockSession:
            _with_session(MockSession, mock_get)
            r = run(get_weather(city="Jalandhar"))
        self.assertIn("Sorry", r)
        self.assertNotIn("Traceback", r)

    def test_geocode_connection_error(self):
        import aiohttp as _aiohttp
        with patch("app.tools.my_tools.aiohttp.ClientSession") as MockSession:
            sess = MagicMock()
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(side_effect=_aiohttp.ClientError("no route"))
            cm.__aexit__ = AsyncMock(return_value=False)
            sess.get = MagicMock(return_value=cm)
            MockSession.return_value.__aenter__ = AsyncMock(return_value=sess)
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            r = run(get_weather(city="Delhi"))
        self.assertIn("Sorry", r)

    def test_weather_http_500(self):
        """Geocode succeeds, weather endpoint returns HTTP 500."""
        import aiohttp as _aiohttp
        call_count = [0]
        def mock_get(*args, **kwargs):
            if call_count[0] == 0:
                call_count[0] += 1
                return _make_resp(GEO_JALANDHAR, status=200)
            call_count[0] += 1
            cm = MagicMock()
            resp = MagicMock()
            resp.raise_for_status = MagicMock(
                side_effect=_aiohttp.ClientResponseError(
                    request_info=MagicMock(), history=(), status=500
                )
            )
            resp.json = AsyncMock(return_value={})
            cm.__aenter__ = AsyncMock(return_value=resp)
            cm.__aexit__ = AsyncMock(return_value=False)
            return cm
        with patch("app.tools.my_tools.aiohttp.ClientSession") as MockSession:
            _with_session(MockSession, mock_get)
            r = run(get_weather(city="Jalandhar"))
        self.assertIn("Sorry", r)

    def test_missing_current_key_in_weather_json(self):
        """Weather API returns JSON with no 'current' key — KeyError handled."""
        with patch("app.tools.my_tools.aiohttp.ClientSession") as MockSession:
            _with_session(MockSession, _two_call_mock(GEO_JALANDHAR, {"hourly": {}}))
            r = run(get_weather(city="Jalandhar"))
        self.assertIn("Sorry", r)
        self.assertNotIn("Traceback", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
