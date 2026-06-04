"""
AccuWeather API client for fetching weather data.
"""
import httpx
from typing import Optional, Dict, Any, List
from app.utils.logger import logger


class AccuWeatherClient:
    """Client for interacting with AccuWeather API."""

    BASE_URL = "https://dataservice.accuweather.com"

    def __init__(self, api_key: str):
        """
        Initialize the AccuWeather client.

        Args:
            api_key: AccuWeather API key
        """
        if not api_key:
            raise ValueError("AccuWeather API key is required")
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    async def _make_request(self,
                            endpoint: str,
                            params: Optional[Dict[str,
                                                  Any]] = None,
                            use_bearer_auth: bool = False) -> Any:
        """
        Make a request to the AccuWeather API.

        Args:
            endpoint: API endpoint
            params: Query parameters
            use_bearer_auth: Whether to use Bearer token authentication instead of apikey parameter

        Returns:
            JSON response from the API
        """
        if params is None:
            params = {}

        url = f"{self.BASE_URL}{endpoint}"
        headers = {}

        if use_bearer_auth:
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            params["apikey"] = self.api_key

        try:
            logger.debug(f"Making request to: {url}")
            response = await self.client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error occurred: {e.response.status_code} - {e.response.text}")
            raise Exception(f"AccuWeather API error: {e.response.status_code}")
        except Exception as e:
            logger.error(f"Error making request to AccuWeather API: {str(e)}")
            raise

    async def search_location(self, query: str) -> Optional[Dict[str, Any]]:
        """
        Search for a location by city name.

        Args:
            query: City name or location query

        Returns:
            Location data including location key, or None if not found
        """
        endpoint = "/locations/v1/search"
        params = {"q": query}

        try:
            results = await self._make_request(endpoint, params, use_bearer_auth=True)

            if results and len(results) > 0:
                location = results[0]
                logger.info(
                    f"Found location: {
                        location.get('LocalizedName')}, {
                        location.get(
                            'Country',
                            {}).get('LocalizedName')}")
                return {
                    "key": location.get("Key"),
                    "name": location.get("LocalizedName"),
                    "country": location.get("Country", {}).get("LocalizedName"),
                    "administrative_area": location.get("AdministrativeArea", {}).get("LocalizedName"),
                }
            else:
                logger.warning(f"No location found for query: {query}")
                return None
        except Exception as e:
            logger.error(f"Error searching location: {str(e)}")
            raise

    async def get_hourly_forecast(self, location_key: str, hours: int = 12) -> Dict[str, Any]:
        """
        Get hourly weather forecast.

        Args:
            location_key: AccuWeather location key
            hours: Number of hours (12 or 1-120 depending on API subscription)

        Returns:
            Hourly forecast data
        """
        # Use 12hour endpoint as specified in requirements
        endpoint = f"/forecasts/v1/hourly/12hour/{location_key}"
        params = {"details": "true", "metric": "true"}

        try:
            forecast = await self._make_request(endpoint, params)

            # Parse and format the hourly data
            hourly_data = []
            for hour in forecast[:hours]:
                hourly_data.append({
                    "datetime": hour.get("DateTime"),
                    "temperature": hour.get("Temperature", {}).get("Value"),
                    "condition": hour.get("IconPhrase"),
                    "rain_probability": hour.get("RainProbability", hour.get("PrecipitationProbability", 0)),
                    "wind_speed": hour.get("Wind", {}).get("Speed", {}).get("Value"),
                    "wind_direction": hour.get("Wind", {}).get("Direction", {}).get("Localized"),
                    "humidity": hour.get("RelativeHumidity"),
                    "uv_index": hour.get("UVIndex"),
                    "precipitation": hour.get("TotalLiquid", {}).get("Value", 0)
                })

            return {
                "data": hourly_data
            }
        except Exception as e:
            logger.error(f"Error getting hourly forecast: {str(e)}")
            raise

    async def get_daily_forecast(self, location_key: str, days: int = 7) -> Dict[str, Any]:
        """
        Get daily weather forecast.

        Args:
            location_key: AccuWeather location key
            days: Number of days (1, 5, 10, or 15 depending on API subscription)

        Returns:
            Daily forecast data
        """
        # Map days to appropriate endpoint
        if days <= 1:
            endpoint_days = "1day"
        elif days <= 5:
            endpoint_days = "5day"
        elif days <= 10:
            endpoint_days = "10day"
        else:
            endpoint_days = "15day"

        endpoint = f"/forecasts/v1/daily/{endpoint_days}/{location_key}"
        params = {"details": "true", "metric": "true"}

        try:
            forecast = await self._make_request(endpoint, params)

            # Parse and format the daily data
            daily_data = []
            daily_forecasts = forecast.get("DailyForecasts", [])

            for day in daily_forecasts[:days]:
                daily_data.append(
                    {
                        "date": day.get("Date"), "temp_min": day.get(
                            "Temperature", {}).get(
                            "Minimum", {}).get("Value"), "temp_max": day.get(
                            "Temperature", {}).get(
                            "Maximum", {}).get("Value"), "day": {
                            "condition": day.get(
                                "Day", {}).get("IconPhrase"), "rain_probability": day.get(
                                    "Day", {}).get(
                                        "RainProbability", day.get(
                                            "Day", {}).get(
                                                "PrecipitationProbability", 0)), "wind_speed": day.get(
                                                    "Day", {}).get(
                                                        "Wind", {}).get(
                                                            "Speed", {}).get("Value"), "wind_direction": day.get(
                                                                "Day", {}).get(
                                                                    "Wind", {}).get(
                                                                        "Direction", {}).get("Localized")}, "night": {
                                                                            "condition": day.get(
                                                                                "Night", {}).get("IconPhrase"), "rain_probability": day.get(
                                                                                    "Night", {}).get(
                                                                                        "RainProbability", day.get(
                                                                                            "Night", {}).get(
                                                                                                "PrecipitationProbability", 0))}})

            return {
                "headline": forecast.get("Headline", {}).get("Text", ""),
                "data": daily_data
            }
        except Exception as e:
            logger.error(f"Error getting daily forecast: {str(e)}")
            raise

    async def get_current_conditions(self, location_key: str) -> Optional[Dict[str, Any]]:
        """
        Get current weather conditions.

        Args:
            location_key: AccuWeather location key

        Returns:
            Current weather conditions data
        """
        endpoint = f"/currentconditions/v1/{location_key}"
        params = {"details": "true"}

        try:
            conditions = await self._make_request(endpoint, params)

            if conditions and len(conditions) > 0:
                current = conditions[0]

                return {
                    "observation_time": current.get("LocalObservationDateTime"),
                    "condition": current.get("WeatherText"),
                    "temperature": current.get(
                        "Temperature",
                        {}).get(
                        "Metric",
                        {}).get("Value"),
                    "real_feel": current.get(
                        "RealFeelTemperature",
                        {}).get(
                        "Metric",
                        {}).get("Value"),
                    "humidity": current.get("RelativeHumidity"),
                    "uv_index": current.get("UVIndex"),
                    "uv_index_text": current.get("UVIndexText"),
                    "precipitation": current.get(
                        "PrecipitationSummary",
                        {}).get(
                            "Precipitation",
                            {}).get(
                                "Metric",
                                {}).get(
                                    "Value",
                                    0),
                    "wind_speed": current.get(
                        "Wind",
                        {}).get(
                        "Speed",
                        {}).get(
                        "Metric",
                        {}).get("Value"),
                    "wind_direction": current.get(
                        "Wind",
                        {}).get(
                        "Direction",
                        {}).get("Localized"),
                    "has_precipitation": current.get(
                        "HasPrecipitation",
                        False),
                    "precipitation_type": current.get("PrecipitationType")}
            else:
                logger.warning(f"No current conditions found for location key: {location_key}")
                return None
        except Exception as e:
            logger.error(f"Error getting current conditions: {str(e)}")
            raise

    async def get_historical_24h(self, location_key: str) -> Dict[str, Any]:
        """
        Get historical weather data for the past 24 hours.

        Args:
            location_key: AccuWeather location key

        Returns:
            Historical weather data for past 24 hours
        """
        endpoint = f"/currentconditions/v1/{location_key}/historical/24"
        params = {"details": "true"}

        try:
            historical_data = await self._make_request(endpoint, params)

            # Parse and format the historical data
            history = []
            for entry in historical_data:
                history.append({
                    "observation_time": entry.get("LocalObservationDateTime"),
                    "condition": entry.get("WeatherText"),
                    "temperature": entry.get("Temperature", {}).get("Metric", {}).get("Value"),
                    "real_feel": entry.get("RealFeelTemperature", {}).get("Metric", {}).get("Value"),
                    "humidity": entry.get("RelativeHumidity"),
                    "wind_speed": entry.get("Wind", {}).get("Speed", {}).get("Metric", {}).get("Value"),
                    "wind_direction": entry.get("Wind", {}).get("Direction", {}).get("Localized"),
                    "has_precipitation": entry.get("HasPrecipitation", False)
                })

            return {
                "data": history
            }
        except Exception as e:
            logger.error(f"Error getting historical data: {str(e)}")
            raise
