# Weather Tool — Voice Agent Workshop

## Overview

This contribution adds a `get_weather(city: str)` asynchronous tool to the Voice Agent Workshop project. It allows the agent's tool layer to retrieve current weather information for a specified city using the `wttr.in` weather service.

## What I Implemented

### 1. Weather Tool

**File:** `app/tools/my_tools.py`

Implemented `get_weather(city: str)` with the following features:

- Accepts a city name as input.
- URL-encodes the city name before making the request.
- Fetches weather information from `https://wttr.in/` using the `format=3` response format.
- Uses `aiohttp` for asynchronous HTTP requests.
- Validates empty or whitespace-only city names.
- Handles unsuccessful HTTP responses, empty or invalid responses, and unexpected response formats.
- Includes error handling for request timeouts, client errors, and unexpected exceptions.

### 2. Automated Tests

**File:** `tests/test_weather_tool.py`

Added automated tests covering the weather tool's expected behavior and error-handling scenarios.

The tests mock network requests, allowing the tool to be tested without making actual requests to the weather service.

## Testing

Run the weather-tool tests using:

```bash
python -m pytest tests/test_weather_tool.py -v
```

**Result:** 9 tests passed.

Also checked the changes for whitespace errors using:

```bash
git diff --check
```

The complete voice-agent workflow, including natural-language tool dispatch through Ollama, has not yet been verified.

## Dependencies

The implementation uses `aiohttp`, which is already included in the project's requirements.

No additional dependency was required for this contribution.

## Summary

This contribution introduces an asynchronous weather tool with input validation, response validation, error handling, and automated tests. It is designed to extend the existing voice agent's tool capabilities while keeping the implementation testable independently of the local language model.

