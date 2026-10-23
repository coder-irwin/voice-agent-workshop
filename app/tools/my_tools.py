"""
my_tools.py â€” âœï¸  YOUR PLAYGROUND. Add your own voice-callable tools here!

How it works (3 rules):
  1. Write a normal Python function.
  2. Put @tool on top of it.
  3. Write a clear docstring â€” the AI reads it to decide WHEN to use your tool.

Restart the server and say it out loud. Done â€” no other file to edit.

Tips:
  â€¢ The first docstring line = what the tool does (be specific!)
  â€¢ "Args:" lines describe each parameter so the AI fills them correctly
  â€¢ Always return a SHORT string â€” the voice agent will say it back to you
  â€¢ Use `async def` if you call a browser or network; plain `def` is fine otherwise
"""

import asyncio
import logging
import random
from urllib.parse import quote

import aiohttp

from .registry import tool

logger = logging.getLogger(__name__)


@tool
def roll_dice(sides: int = 6) -> str:
    """Roll a dice and report the number that came up.

    Args:
        sides: how many sides the dice has (default 6)
    """
    return f"The dice rolled {random.randint(1, sides)}"


@tool
async def get_weather(city: str) -> str:
    """Get the current weather for a given city.

    Args:
        city: name of the city to get weather for
    """
    city_clean = str(city).strip() if city is not None else ""
    if not city_clean:
        return "Please specify a city name to get the weather."

    encoded_city = quote(city_clean)
    url = f"https://wttr.in/{encoded_city}?format=3"
    timeout = aiohttp.ClientTimeout(total=5.0)
    headers = {"User-Agent": "curl/8.0.0"}

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return f"Could not get weather for {city_clean}. Service returned status {resp.status}."
                text = (await resp.text()).strip()
                if (
                    not text
                    or "unknown location" in text.lower()
                    or "<html" in text.lower()
                    or "<!doctype" in text.lower()
                    or "\n" in text
                    or len(text) > 150
                ):
                    return f"Could not find weather information for {city_clean}."
                return text
    except asyncio.TimeoutError:
        return f"Weather service timed out while checking {city_clean}."
    except aiohttp.ClientError as e:
        logger.warning(f"Weather request failed for {city_clean}: {e}")
        return f"Unable to reach weather service for {city_clean}."
    except Exception as e:
        logger.error(f"Unexpected error getting weather for {city_clean}: {e}", exc_info=True)
        return f"An error occurred while fetching weather for {city_clean}."
