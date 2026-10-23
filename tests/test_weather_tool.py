import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

# Ensure optional browser dependency (playwright) does not block importing app.tools in lightweight test environments
for mod in ["playwright", "playwright.async_api"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import aiohttp
import app.tools

from app.tools.my_tools import get_weather
from app.tools.registry import TOOLS, run_tool


class MockResponse:
    def __init__(self, status=200, text="London: ⛅️ +15°C"):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class MockClientSession:
    def __init__(self, response=None, get_side_effect=None, timeout=None):
        self.response = response or MockResponse()
        self.get_side_effect = get_side_effect
        self.timeout = timeout
        self.last_url = None
        self.last_headers = None

    def get(self, url, headers=None):
        self.last_url = url
        self.last_headers = headers
        if self.get_side_effect:
            raise self.get_side_effect
        return self.response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


def test_tool_registration():
    """Verify get_weather is auto-loaded and properly registered in registry.TOOLS."""
    assert "get_weather" in TOOLS
    schema = TOOLS["get_weather"]["schema"]["function"]
    assert schema["name"] == "get_weather"
    assert "parameters" in schema
    params = schema["parameters"]
    assert "city" in params["properties"]
    assert params["properties"]["city"]["type"] == "string"
    assert "city" in params["required"]


def test_get_weather_empty_city():
    """Verify empty or whitespace-only city input is rejected without network calls."""
    with patch("aiohttp.ClientSession") as mock_session:
        res1 = asyncio.run(get_weather(""))
        assert "Please specify a city name" in res1

        res2 = asyncio.run(get_weather("   "))
        assert "Please specify a city name" in res2

        res3 = asyncio.run(get_weather(None))
        assert "Please specify a city name" in res3

        # Assert no network request was ever initiated
        mock_session.assert_not_called()


def test_get_weather_success():
    """Verify successful weather fetch with mocked aiohttp request."""
    session_instance = MockClientSession(response=MockResponse(status=200, text="Tokyo: ☀️ +20°C\n"))

    with patch("aiohttp.ClientSession", return_value=session_instance):
        result = asyncio.run(get_weather("Tokyo"))
        assert result == "Tokyo: ☀️ +20°C"
        assert session_instance.last_url == "https://wttr.in/Tokyo?format=3"


def test_get_weather_url_encoding():
    """Verify city names with spaces or special characters are URL-encoded."""
    session_instance = MockClientSession(response=MockResponse(status=200, text="San Francisco: ⛅️ +18°C"))

    with patch("aiohttp.ClientSession", return_value=session_instance):
        result = asyncio.run(get_weather("San Francisco"))
        assert result == "San Francisco: ⛅️ +18°C"
        assert session_instance.last_url == "https://wttr.in/San%20Francisco?format=3"


def test_get_weather_http_error():
    """Verify handling of non-200 HTTP status code."""
    session_instance = MockClientSession(response=MockResponse(status=500, text="Internal Server Error"))

    with patch("aiohttp.ClientSession", return_value=session_instance):
        result = asyncio.run(get_weather("Berlin"))
        assert "Service returned status 500" in result


def test_get_weather_unknown_location_or_html():
    """Verify handling of unknown locations, HTML error pages, multi-line error traces, and empty text."""
    # Unknown location message with 200 OK
    session_instance = MockClientSession(response=MockResponse(status=200, text="Unknown location; please try..."))
    with patch("aiohttp.ClientSession", return_value=session_instance):
        result = asyncio.run(get_weather("NonExistentCity12345"))
        assert "Could not find weather information" in result

    # HTML error page with 200 OK
    session_html = MockClientSession(response=MockResponse(status=200, text="<!DOCTYPE html><html><body>Error</body></html>"))
    with patch("aiohttp.ClientSession", return_value=session_html):
        result2 = asyncio.run(get_weather("BadResponseCity"))
        assert "Could not find weather information" in result2

    # Multiline error trace with 200 OK
    session_multiline = MockClientSession(response=MockResponse(status=200, text="Traceback (most recent call last):\n  File ..."))
    with patch("aiohttp.ClientSession", return_value=session_multiline):
        result3 = asyncio.run(get_weather("CrashingCity"))
        assert "Could not find weather information" in result3

    # Empty response with 200 OK
    session_empty = MockClientSession(response=MockResponse(status=200, text="   "))
    with patch("aiohttp.ClientSession", return_value=session_empty):
        result4 = asyncio.run(get_weather("EmptyCity"))
        assert "Could not find weather information" in result4


def test_get_weather_timeout():
    """Verify timeout is caught and a user-friendly error is returned."""
    session_instance = MockClientSession(get_side_effect=asyncio.TimeoutError())

    with patch("aiohttp.ClientSession", return_value=session_instance):
        result = asyncio.run(get_weather("London"))
        assert "timed out" in result


def test_get_weather_client_error():
    """Verify network connection errors are handled cleanly."""
    session_instance = MockClientSession(get_side_effect=aiohttp.ClientConnectionError("Connection failed"))

    with patch("aiohttp.ClientSession", return_value=session_instance):
        result = asyncio.run(get_weather("London"))
        assert "Unable to reach weather service" in result


def test_run_tool_integration():
    """Verify get_weather executes seamlessly via registry.run_tool."""
    session_instance = MockClientSession(response=MockResponse(status=200, text="Paris: 🌧 +11°C"))

    with patch("aiohttp.ClientSession", return_value=session_instance):
        ok, res = asyncio.run(run_tool("get_weather", {"city": "Paris"}))
        assert ok is True
        assert res == "Paris: 🌧 +11°C"

