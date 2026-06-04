"""Google Places built-in tool.

Provides nearby place search via Google Places API (New) and
Google Maps direction link generation.

Uses embedding-based semantic filtering to narrow the 336 Google Places
included_types down to the most relevant ones for each query, reducing
token usage and improving LLM tool-call accuracy.

Requires:
    GOOGLE_MAPS_API_KEY in tool config
"""

import urllib.parse
from typing import Any, Callable, Dict, List, Optional

import httpx

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger


SERVER_NAME = "Google Places"

class Var:
    """Variable keys for the Google Places tool."""
    API_KEY = "GOOGLE_MAPS_API_KEY"


TOOL_CONFIG: ToolConfig = {
    "name": "gg_places",
    "display_name": "Google Places",
    "default_model_group": "low",
    "visible": False,
    # Requires the ``google`` feature (googleapiclient + google-auth). See
    # app.features.manifest.FEATURES.
    "requires_feature": "google",
    "llm_parameters": {
        "tool_instructions": (
            "A places assistant that searches for nearby places using Google Maps "
            "and generates direction links. Use search_places to find nearby locations "
            "and map_direction_link to create navigation URLs."
        ),
    },
    "required_config": {
        Var.API_KEY: {
            "description": "Google Maps API Key",
            "type": "string",
            "secret": True,
        },
    },
    "arguments": {
        "type": "object",
        "required": ["latitude", "longitude"],
        "properties": {
            "latitude": {
                "type": "number",
                "description": "User's current latitude coordinate",
            },
            "longitude": {
                "type": "number",
                "description": "User's current longitude coordinate",
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Google Places included types (336 types across 20+ categories)
# See: https://developers.google.com/maps/documentation/places/web-service/place-types
# ---------------------------------------------------------------------------

INCLUDED_TYPES_TABLE: dict[str, str] = {
    # Automotive
    "car_dealer": "car_dealer",
    "car_rental": "car_rental",
    "car_repair": "car_repair",
    "car_wash": "car_wash",
    "electric_vehicle_charging_station": "electric_vehicle_charging_station",
    "gas_station": "gas_station",
    "parking": "parking",
    "rest_stop": "rest_stop",
    # Business
    "corporate_office": "corporate_office",
    "farm": "farm",
    "ranch": "ranch",
    # Culture
    "art_gallery": "art_gallery",
    "art_studio": "art_studio",
    "auditorium": "auditorium",
    "cultural_landmark": "cultural_landmark",
    "historical_place": "historical_place",
    "monument": "monument",
    "museum": "museum",
    "performing_arts_theater": "performing_arts_theater",
    "sculpture": "sculpture",
    # Education
    "library": "library",
    "preschool": "preschool",
    "primary_school": "primary_school",
    "school": "school",
    "secondary_school": "secondary_school",
    "university": "university",
    # Entertainment and Recreation
    "adventure_sports_center": "adventure_sports_center",
    "amphitheatre": "amphitheatre",
    "amusement_center": "amusement_center",
    "amusement_park": "amusement_park",
    "aquarium": "aquarium",
    "banquet_hall": "banquet_hall",
    "barbecue_area": "barbecue_area",
    "botanical_garden": "botanical_garden",
    "bowling_alley": "bowling_alley",
    "casino": "casino",
    "childrens_camp": "childrens_camp",
    "comedy_club": "comedy_club",
    "community_center": "community_center",
    "concert_hall": "concert_hall",
    "convention_center": "convention_center",
    "cultural_center": "cultural_center",
    "cycling_park": "cycling_park",
    "dance_hall": "dance_hall",
    "dog_park": "dog_park",
    "event_venue": "event_venue",
    "ferris_wheel": "ferris_wheel",
    "garden": "garden",
    "hiking_area": "hiking_area",
    "historical_landmark": "historical_landmark",
    "internet_cafe": "internet_cafe",
    "karaoke": "karaoke",
    "marina": "marina",
    "movie_rental": "movie_rental",
    "movie_theater": "movie_theater",
    "national_park": "national_park",
    "night_club": "night_club",
    "observation_deck": "observation_deck",
    "off_roading_area": "off_roading_area",
    "opera_house": "opera_house",
    "park": "park",
    "philharmonic_hall": "philharmonic_hall",
    "picnic_ground": "picnic_ground",
    "planetarium": "planetarium",
    "plaza": "plaza",
    "roller_coaster": "roller_coaster",
    "skateboard_park": "skateboard_park",
    "state_park": "state_park",
    "tourist_attraction": "tourist_attraction",
    "video_arcade": "video_arcade",
    "visitor_center": "visitor_center",
    "water_park": "water_park",
    "wedding_venue": "wedding_venue",
    "wildlife_park": "wildlife_park",
    "wildlife_refuge": "wildlife_refuge",
    "zoo": "zoo",
    # Facilities
    "public_bath": "public_bath",
    "public_bathroom": "public_bathroom",
    "stable": "stable",
    # Finance
    "accounting": "accounting",
    "atm": "atm",
    "bank": "bank",
    # Food and Drink
    "acai_shop": "acai_shop",
    "afghani_restaurant": "afghani_restaurant",
    "african_restaurant": "african_restaurant",
    "american_restaurant": "american_restaurant",
    "asian_restaurant": "asian_restaurant",
    "bagel_shop": "bagel_shop",
    "bakery": "bakery",
    "bar": "bar",
    "bar_and_grill": "bar_and_grill",
    "barbecue_restaurant": "barbecue_restaurant",
    "brazilian_restaurant": "brazilian_restaurant",
    "breakfast_restaurant": "breakfast_restaurant",
    "brunch_restaurant": "brunch_restaurant",
    "buffet_restaurant": "buffet_restaurant",
    "cafe": "cafe",
    "cafeteria": "cafeteria",
    "candy_store": "candy_store",
    "cat_cafe": "cat_cafe",
    "chinese_restaurant": "chinese_restaurant",
    "chocolate_factory": "chocolate_factory",
    "chocolate_shop": "chocolate_shop",
    "coffee_shop": "coffee_shop",
    "confectionery": "confectionery",
    "deli": "deli",
    "dessert_restaurant": "dessert_restaurant",
    "dessert_shop": "dessert_shop",
    "diner": "diner",
    "dog_cafe": "dog_cafe",
    "donut_shop": "donut_shop",
    "fast_food_restaurant": "fast_food_restaurant",
    "fine_dining_restaurant": "fine_dining_restaurant",
    "food_court": "food_court",
    "french_restaurant": "french_restaurant",
    "greek_restaurant": "greek_restaurant",
    "hamburger_restaurant": "hamburger_restaurant",
    "ice_cream_shop": "ice_cream_shop",
    "indian_restaurant": "indian_restaurant",
    "indonesian_restaurant": "indonesian_restaurant",
    "italian_restaurant": "italian_restaurant",
    "japanese_restaurant": "japanese_restaurant",
    "juice_shop": "juice_shop",
    "korean_restaurant": "korean_restaurant",
    "lebanese_restaurant": "lebanese_restaurant",
    "meal_delivery": "meal_delivery",
    "meal_takeaway": "meal_takeaway",
    "mediterranean_restaurant": "mediterranean_restaurant",
    "mexican_restaurant": "mexican_restaurant",
    "middle_eastern_restaurant": "middle_eastern_restaurant",
    "pizza_restaurant": "pizza_restaurant",
    "pub": "pub",
    "ramen_restaurant": "ramen_restaurant",
    "restaurant": "restaurant",
    "sandwich_shop": "sandwich_shop",
    "seafood_restaurant": "seafood_restaurant",
    "spanish_restaurant": "spanish_restaurant",
    "steak_house": "steak_house",
    "sushi_restaurant": "sushi_restaurant",
    "tea_house": "tea_house",
    "thai_restaurant": "thai_restaurant",
    "turkish_restaurant": "turkish_restaurant",
    "vegan_restaurant": "vegan_restaurant",
    "vegetarian_restaurant": "vegetarian_restaurant",
    "vietnamese_restaurant": "vietnamese_restaurant",
    "wine_bar": "wine_bar",
    # Geographical Areas
    "administrative_area_level_1": "administrative_area_level_1",
    "administrative_area_level_2": "administrative_area_level_2",
    "country": "country",
    "locality": "locality",
    "postal_code": "postal_code",
    "school_district": "school_district",
    # Government
    "city_hall": "city_hall",
    "courthouse": "courthouse",
    "embassy": "embassy",
    "fire_station": "fire_station",
    "government_office": "government_office",
    "local_government_office": "local_government_office",
    "neighborhood_police_station": "neighborhood_police_station",
    "police": "police",
    "post_office": "post_office",
    # Health and Wellness
    "chiropractor": "chiropractor",
    "dental_clinic": "dental_clinic",
    "dentist": "dentist",
    "doctor": "doctor",
    "drugstore": "drugstore",
    "hospital": "hospital",
    "massage": "massage",
    "medical_lab": "medical_lab",
    "pharmacy": "pharmacy",
    "physiotherapist": "physiotherapist",
    "sauna": "sauna",
    "skin_care_clinic": "skin_care_clinic",
    "spa": "spa",
    "tanning_studio": "tanning_studio",
    "wellness_center": "wellness_center",
    "yoga_studio": "yoga_studio",
    # Housing
    "apartment_building": "apartment_building",
    "apartment_complex": "apartment_complex",
    "condominium_complex": "condominium_complex",
    "housing_complex": "housing_complex",
    # Lodging
    "bed_and_breakfast": "bed_and_breakfast",
    "budget_japanese_inn": "budget_japanese_inn",
    "campground": "campground",
    "camping_cabin": "camping_cabin",
    "cottage": "cottage",
    "extended_stay_hotel": "extended_stay_hotel",
    "farmstay": "farmstay",
    "guest_house": "guest_house",
    "hostel": "hostel",
    "hotel": "hotel",
    "inn": "inn",
    "japanese_inn": "japanese_inn",
    "lodging": "lodging",
    "mobile_home_park": "mobile_home_park",
    "motel": "motel",
    "private_guest_room": "private_guest_room",
    "resort_hotel": "resort_hotel",
    "rv_park": "rv_park",
    # Natural Features
    "beach": "beach",
    # Places of Worship
    "church": "church",
    "hindu_temple": "hindu_temple",
    "mosque": "mosque",
    "synagogue": "synagogue",
    # Services
    "astrologer": "astrologer",
    "barber_shop": "barber_shop",
    "beautician": "beautician",
    "beauty_salon": "beauty_salon",
    "body_art_service": "body_art_service",
    "catering_service": "catering_service",
    "cemetery": "cemetery",
    "child_care_agency": "child_care_agency",
    "consultant": "consultant",
    "courier_service": "courier_service",
    "electrician": "electrician",
    "florist": "florist",
    "food_delivery": "food_delivery",
    "foot_care": "foot_care",
    "funeral_home": "funeral_home",
    "hair_care": "hair_care",
    "hair_salon": "hair_salon",
    "insurance_agency": "insurance_agency",
    "laundry": "laundry",
    "lawyer": "lawyer",
    "locksmith": "locksmith",
    "makeup_artist": "makeup_artist",
    "moving_company": "moving_company",
    "nail_salon": "nail_salon",
    "painter": "painter",
    "plumber": "plumber",
    "psychic": "psychic",
    "real_estate_agency": "real_estate_agency",
    "roofing_contractor": "roofing_contractor",
    "storage": "storage",
    "summer_camp_organizer": "summer_camp_organizer",
    "tailor": "tailor",
    "telecommunications_service_provider": "telecommunications_service_provider",
    "tour_agency": "tour_agency",
    "tourist_information_center": "tourist_information_center",
    "travel_agency": "travel_agency",
    "veterinary_care": "veterinary_care",
    # Shopping
    "asian_grocery_store": "asian_grocery_store",
    "auto_parts_store": "auto_parts_store",
    "bicycle_store": "bicycle_store",
    "book_store": "book_store",
    "butcher_shop": "butcher_shop",
    "cell_phone_store": "cell_phone_store",
    "clothing_store": "clothing_store",
    "convenience_store": "convenience_store",
    "department_store": "department_store",
    "discount_store": "discount_store",
    "electronics_store": "electronics_store",
    "food_store": "food_store",
    "furniture_store": "furniture_store",
    "gift_shop": "gift_shop",
    "grocery_store": "grocery_store",
    "hardware_store": "hardware_store",
    "home_goods_store": "home_goods_store",
    "home_improvement_store": "home_improvement_store",
    "jewelry_store": "jewelry_store",
    "liquor_store": "liquor_store",
    "market": "market",
    "pet_store": "pet_store",
    "shoe_store": "shoe_store",
    "shopping_mall": "shopping_mall",
    "sporting_goods_store": "sporting_goods_store",
    "store": "store",
    "supermarket": "supermarket",
    "warehouse_store": "warehouse_store",
    "wholesaler": "wholesaler",
    # Sports
    "arena": "arena",
    "athletic_field": "athletic_field",
    "fishing_charter": "fishing_charter",
    "fishing_pond": "fishing_pond",
    "fitness_center": "fitness_center",
    "golf_course": "golf_course",
    "gym": "gym",
    "ice_skating_rink": "ice_skating_rink",
    "playground": "playground",
    "ski_resort": "ski_resort",
    "sports_activity_location": "sports_activity_location",
    "sports_club": "sports_club",
    "sports_coaching": "sports_coaching",
    "sports_complex": "sports_complex",
    "stadium": "stadium",
    "swimming_pool": "swimming_pool",
    # Transportation
    "airport": "airport",
    "airstrip": "airstrip",
    "bus_station": "bus_station",
    "bus_stop": "bus_stop",
    "ferry_terminal": "ferry_terminal",
    "heliport": "heliport",
    "international_airport": "international_airport",
    "light_rail_station": "light_rail_station",
    "park_and_ride": "park_and_ride",
    "subway_station": "subway_station",
    "taxi_stand": "taxi_stand",
    "train_station": "train_station",
    "transit_depot": "transit_depot",
    "transit_station": "transit_station",
    "truck_stop": "truck_stop",
}


# ---------------------------------------------------------------------------
# Embedding-based type filtering
# ---------------------------------------------------------------------------

_GG_PLACES_COLLECTION = "gg_places_types"


# Common place types used when Vector Embedding is disabled — keeps the
# `search_places` enum small (~32 types) so the LLM tool schema stays cheap.
COMMON_INCLUDED_TYPES: list[str] = [
    "restaurant", "cafe", "bakery", "bar", "fast_food_restaurant",
    "supermarket", "convenience_store", "shopping_mall", "department_store",
    "gas_station", "parking", "atm", "bank",
    "hospital", "pharmacy", "dental_clinic", "doctor",
    "hotel", "lodging",
    "park", "tourist_attraction", "museum", "movie_theater",
    "gym", "spa", "hair_salon",
    "bus_station", "train_station", "subway_station", "airport",
    "school", "library", "post_office",
]


def _build_embedding_table(vector_store=None):
    """Build the embedding table for INCLUDED_TYPES_TABLE at startup.

    When ``vector_store`` is provided and embedding is enabled, cached
    embeddings are loaded from the vector store on subsequent startups —
    skipping the 336 encode calls entirely.

    Returns ``(embedding_vendor, embedding_table)``, or ``(None, None)`` when
    embedding is disabled or unavailable. Callers handle the ``None`` case
    by switching to the static ``COMMON_INCLUDED_TYPES`` list.
    """
    from app.config.settings import BaseConfig

    if not BaseConfig.is_embedding_enabled():
        return None, None

    try:
        from app.lib.embedding import LocalEmbeddings
        from app.vectorstores import get_or_build_embedding_table

        embedding_vendor = LocalEmbeddings()
        table = get_or_build_embedding_table(
            vector_store=vector_store,
            embedding=embedding_vendor,
            data=INCLUDED_TYPES_TABLE,
            collection_name=_GG_PLACES_COLLECTION,
        )
        if table and len(table) > 0:
            logger.info(f"[gg_places] Built embedding table with {len(table)} place types")
            return embedding_vendor, table
        logger.warning("[gg_places] Embedding table is empty, type filtering disabled")
    except Exception as e:
        logger.warning(f"[gg_places] Embedding service unavailable, type filtering disabled: {e}")
    return None, None


def create_prepare_tools(embedding_vendor, embedding_table) -> Optional[Callable]:
    """Create a ``prepare_tools`` callback that narrows the ``search_places``
    type enum.

    - When embeddings are available: filters per query down to the top 50
      semantically similar types.
    - When embeddings are unavailable (disabled / failed to load): returns a
      callback that statically swaps in ``COMMON_INCLUDED_TYPES`` so the LLM
      still gets a manageable list instead of all 336 types.
    """
    if embedding_vendor is None or embedding_table is None:
        def prepare_tools_static(
            query: str,  # noqa: ARG001
            tools: List[Dict[str, Any]],
            *,
            arguments: Optional[Dict[str, Any]] = None,  # noqa: ARG001
            **_: Any,
        ) -> List[Dict[str, Any]]:
            for tool in tools:
                if tool["function"]["name"] == "search_places":
                    tool["function"]["parameters"]["properties"]["type"]["enum"] = COMMON_INCLUDED_TYPES
                    break
            return tools

        return prepare_tools_static

    def prepare_tools(
        query: str,
        tools: List[Dict[str, Any]],
        *,
        arguments: Optional[Dict[str, Any]] = None,  # noqa: ARG001 — unused, accepted for adapter signature
        **_: Any,  # forward-compat: adapter also passes context_id, profile
    ) -> List[Dict[str, Any]]:
        """Filter search_places type enum to the top 50 semantically relevant types."""
        try:
            from app.utils.common import find_similar_items
            included_types = find_similar_items(
                query=query,
                embedding_vendor=embedding_vendor,
                embedding_table=embedding_table,
                limit=50,
            )
            for tool in tools:
                if tool["function"]["name"] == "search_places":
                    tool["function"]["parameters"]["properties"]["type"]["enum"] = included_types
                    break
            logger.debug(f"[gg_places] Filtered to {len(included_types)} place types for query")
        except Exception as e:
            logger.warning(f"[gg_places] Type filtering failed, using unfiltered types: {e}")
        return tools

    return prepare_tools


# ---------------------------------------------------------------------------
# Tool classes
# ---------------------------------------------------------------------------

class SearchPlacesTool(BuiltInTool):
    name: str = "search_places"
    description: str = (
        "Search for places based on a specific place type. Supports finding restaurants, "
        "cafes, shopping centers, gyms, parks, hospitals, pharmacies, gas stations, banks, "
        "hotels, movie theaters, and other points of interest. The default search radius "
        "is 500 meters, you can adjust it as needed."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "description": (
            "Search for places nearby. Providing coordinates is unnecessary, "
            "as the coordinates will be automatically obtained from the system."
        ),
        "properties": {
            "type": {
                "type": "string",
                "description": "The type of place to search for",
            },
            "radius": {
                "type": "number",
                "description": "The radius (in meters) to search within",
                "default": 500,
            },
            "latitude": {
                "type": "number",
                "description": "Latitude of the center point for the search",
            },
            "longitude": {
                "type": "number",
                "description": "Longitude of the center point for the search",
            },
        },
        "required": ["type"],
    }

    def __init__(self, api_key: str = ""):
        self._api_key = api_key

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        logger.info(f"[gg_places] Searching for places: {arguments}")

        place_type_str = arguments.get("type", "restaurant")
        latitude = arguments.get("latitude")
        longitude = arguments.get("longitude")
        radius = arguments.get("radius", 500)

        if not self._api_key:
            return BuiltInToolResult(structured_content={
                "status": "error",
                "msg": "Google Maps API key not configured",
            })

        if latitude is None or longitude is None:
            return BuiltInToolResult(structured_content={
                "status": "error",
                "msg": "Latitude and longitude are required for place search",
            })

        url = "https://places.googleapis.com/v1/places:searchNearby"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": "places.displayName,places.location,places.formattedAddress",
        }
        data = {
            "includedTypes": [place_type_str],
            "maxResultCount": 10,
            "locationRestriction": {
                "circle": {
                    "center": {
                        "latitude": latitude,
                        "longitude": longitude,
                    },
                    "radius": radius,
                }
            },
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=data)
            if response.status_code == 200:
                result = response.json()
                places = result.get("places", [])
                selected_places = []
                for place in places:
                    display_name = place.get("displayName", {}).get("text", "")
                    location = place.get("location", {})
                    lat = location.get("latitude")
                    lng = location.get("longitude")
                    coordinates = f"{lat},{lng}" if lat is not None and lng is not None else ""
                    address = place.get("formattedAddress", "")
                    selected_places.append({
                        "name": display_name,
                        "coordinates": coordinates,
                        "address": address,
                    })

                if selected_places:
                    response_data = {
                        "type": place_type_str,
                        "status": "success",
                        "msg": "Places found successfully.",
                        "places": selected_places,
                    }
                else:
                    response_data = {
                        "type": place_type_str,
                        "status": "error",
                        "msg": (
                            "No places found matching the criteria, or no locations were "
                            "found within the current search radius. You can expand the "
                            "search radius."
                        ),
                    }
            else:
                response_data = {
                    "type": place_type_str,
                    "status": "error",
                    "msg": f"API error: {response.status_code} {response.text}",
                }

        logger.info(f"[gg_places] Search results: {response_data}")
        return BuiltInToolResult(structured_content=response_data)


class MapDirectionLinkTool(BuiltInTool):
    name: str = "map_direction_link"
    description: str = (
        "This tool generates a Google Maps directions link from an origin to a destination. "
        "Each endpoint (origin and destination) can be specified as either a place name/address "
        "(string) or as latitude/longitude coordinates. You can mix both styles freely."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "origin": {
                "type": "string",
                "description": "The origin address or place name.",
            },
            "origin_latitude": {
                "type": "number",
                "description": "Latitude of the origin.",
            },
            "origin_longitude": {
                "type": "number",
                "description": "Longitude of the origin.",
            },
            "destination": {
                "type": "string",
                "description": "The destination address or place name.",
            },
            "destination_latitude": {
                "type": "number",
                "description": "Latitude of the destination.",
            },
            "destination_longitude": {
                "type": "number",
                "description": "Longitude of the destination.",
            },
            "travel_mode": {
                "type": "string",
                "description": "Mode of travel.",
                "enum": ["driving", "walking", "bicycling", "transit"],
                "default": "driving",
            },
        },
    }

    @staticmethod
    def _resolve_endpoint(
        name: str | None,
        lat: float | None,
        lng: float | None,
    ) -> str | None:
        """Return a URL-safe string for an endpoint.

        Coordinates take precedence when both a name and coordinates are provided.
        """
        if lat is not None and lng is not None:
            return f"{lat},+{lng}"
        if name:
            return urllib.parse.quote(name, safe=",+")
        return None

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        logger.info(f"[gg_places] Generating route link: {arguments}")

        origin_part = self._resolve_endpoint(
            arguments.get("origin"),
            arguments.get("origin_latitude"),
            arguments.get("origin_longitude"),
        )
        destination_part = self._resolve_endpoint(
            arguments.get("destination"),
            arguments.get("destination_latitude"),
            arguments.get("destination_longitude"),
        )

        if not origin_part or not destination_part:
            return BuiltInToolResult(structured_content={
                "status": "error",
                "msg": "Both origin and destination must be provided (either as a place name or as coordinates).",
            })

        final_url = f"https://www.google.com/maps/dir/{origin_part}/{destination_part}"

        return BuiltInToolResult(structured_content={
            "status": "success",
            "msg": "Route link generated successfully.",
            "link": final_url,
        })


# ---------------------------------------------------------------------------
# Module exports (required by built-in tool convention)
# ---------------------------------------------------------------------------

def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    api_key = config.get(Var.API_KEY, "")
    return [SearchPlacesTool(api_key=api_key), MapDirectionLinkTool()]


def get_prepare_tools(vector_store=None) -> Optional[Callable]:
    """Build embedding table and return the prepare_tools callback.

    Called once at startup by register_builtin_tools(). When `vector_store`
    is provided, cached place-type embeddings are loaded from Qdrant instead
    of being re-generated via gRPC. Returns None if the embedding service
    is unavailable (graceful degradation -- the tool still works, just
    without type filtering).
    """
    embedding_vendor, embedding_table = _build_embedding_table(vector_store=vector_store)
    return create_prepare_tools(embedding_vendor, embedding_table)
