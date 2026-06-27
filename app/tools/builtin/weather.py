"""Weather built-in tool.

Provides weather data via AccuWeather API.

Requires:
    ACCUWEATHER_API_KEY in tool config
"""

from typing import Any, Dict

import httpx

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger


ACCUWEATHER_BASE_URL = "http://dataservice.accuweather.com"

SERVER_NAME = "AccuWeather Weather"

class Var:
    """Variable keys for the Weather tool."""
    API_KEY = "ACCUWEATHER_API_KEY"


TOOL_CONFIG: ToolConfig = {
    "name": "weather",
    "display_name": "Weather",
    "default_model_group": "low",
    "visible": True,
    "llm_parameters": {
        "tool_instructions": "Retrieve weather forecasts and current conditions for a location.",
    },
    "required_config": {
        Var.API_KEY: {
            "description": "AccuWeather API Key",
            "type": "string",
            "secret": True,
        },
    },
}


async def _search_location(api_key: str, location: str) -> Dict[str, Any] | None:
    """Search AccuWeather for a location and return its key and metadata."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{ACCUWEATHER_BASE_URL}/locations/v1/cities/search",
            params={"apikey": api_key, "q": location},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data:
            return None
        loc = data[0]
        return {
            "key": loc["Key"],
            "name": loc["LocalizedName"],
            "country": loc["Country"]["LocalizedName"],
            "administrative_area": loc.get("AdministrativeArea", {}).get("LocalizedName", ""),
        }


async def _get_current_conditions(api_key: str, location_key: str) -> Dict[str, Any] | None:
    """Fetch current weather conditions for a location."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{ACCUWEATHER_BASE_URL}/currentconditions/v1/{location_key}",
            params={"apikey": api_key, "details": "true"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data:
            return None
        return data[0]


async def _get_daily_forecast(api_key: str, location_key: str, days: int = 5) -> Dict[str, Any] | None:
    """Fetch daily weather forecast for a location."""
    endpoint = f"{ACCUWEATHER_BASE_URL}/forecasts/v1/daily/5day/{location_key}"

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            endpoint,
            params={"apikey": api_key, "details": "true", "metric": "true"},
        )
        if resp.status_code != 200:
            return None
        return resp.json()


class GetWeatherTool(BuiltInTool):
    name: str = "get_weather"
    description: str = "Fetches current weather conditions or forecast for a given location using AccuWeather."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "The city or location name to fetch weather for (e.g., 'Tokyo', 'New York').",
            },
            "weather_type": {
                "type": "string",
                "description": "The type of weather data: 'current' for current conditions or 'forecast' for daily forecast.",
                "enum": ["current", "forecast"],
            },
        },
    }

    def __init__(self, api_key: str = ""):
        self._api_key = api_key

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        logger.debug(arguments)
        location = arguments.get("location", "")
        weather_type = arguments.get("weather_type", "current")

        if not self._api_key:
            return BuiltInToolResult(
                structured_content={
                    "error": "Configuration error",
                    "message": "ACCUWEATHER_API_KEY is not set.",
                }
            )

        if not location:
            return BuiltInToolResult(
                structured_content={
                    "error": "Missing parameter",
                    "message": "Location is required.",
                }
            )

        try:
            # Search for location
            location_data = await _search_location(self._api_key, location)
            if not location_data:
                return BuiltInToolResult(
                    structured_content={
                        "error": "Location not found",
                        "message": f"Could not find location: {location}",
                    }
                )

            location_key = location_data["key"]
            location_name = (
                f"{location_data['name']}, "
                f"{location_data.get('administrative_area', '')}, "
                f"{location_data['country']}"
            )

            if weather_type == "current":
                weather_data = await _get_current_conditions(self._api_key, location_key)
                if not weather_data:
                    return BuiltInToolResult(
                        structured_content={
                            "error": "No data",
                            "message": f"Could not retrieve current weather for {location_name}",
                        }
                    )
                return BuiltInToolResult(
                    structured_content={
                        "type": "current_weather",
                        "location": location_name,
                        "current": weather_data,
                    }
                )

            elif weather_type == "forecast":
                forecast_data = await _get_daily_forecast(self._api_key, location_key)
                if not forecast_data:
                    return BuiltInToolResult(
                        structured_content={
                            "error": "No data",
                            "message": f"Could not retrieve forecast for {location_name}",
                        }
                    )
                return BuiltInToolResult(
                    structured_content={
                        "type": "daily_forecast",
                        "location": location_name,
                        "headline": forecast_data.get("Headline", {}).get("Text", ""),
                        "forecast": forecast_data.get("DailyForecasts", []),
                    }
                )

            else:
                return BuiltInToolResult(
                    structured_content={
                        "error": "Invalid weather_type",
                        "message": f"weather_type must be 'current' or 'forecast', got: {weather_type}",
                    }
                )

        except Exception as e:
            return BuiltInToolResult(
                structured_content={
                    "error": "API error",
                    "message": f"Failed to fetch weather data: {str(e)}",
                }
            )


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    api_key = config.get(Var.API_KEY, "")
    return [GetWeatherTool(api_key=api_key)]
