import asyncio
import os
import re
from typing import Annotated
import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp import ErrorData, McpError
from mcp.server.auth.provider import AccessToken
from mcp.types import TextContent, INVALID_PARAMS, INTERNAL_ERROR
from pydantic import BaseModel, Field

# --- Load environment variables ---
load_dotenv()

TOKEN = os.environ.get("AUTH_TOKEN")
MY_NUMBER = os.environ.get("MY_NUMBER")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")

assert TOKEN is not None, "Please set AUTH_TOKEN in your .env file"
assert MY_NUMBER is not None, "Please set MY_NUMBER in your .env file"
assert GOOGLE_API_KEY is not None, "Please set GOOGLE_API_KEY in your .env file"

# --- Auth Provider ---
class SimpleBearerAuthProvider(BearerAuthProvider):
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token == self.token:
            return AccessToken(
                token=token,
                client_id="metro-finder-client",
                scopes=["*"],
                expires_at=None,
            )
        return None

# --- Rich Tool Description model ---
class RichToolDescription(BaseModel):
    description: str
    use_when: str
    side_effects: str | None = None

# --- MCP Server Setup ---
mcp = FastMCP(
    "Transit Finder - Find routes between stations using public transportation",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- Tool: validate (required by Puch) ---
@mcp.tool
async def validate() -> str:
    return MY_NUMBER

# --- Metro Station Finder ---
METRO_FINDER_DESCRIPTION = RichToolDescription(
    description="Find public transit routes including metro, bus, train and tram. Can find the nearest station to your location or provide routes between specific stations.",
    use_when="Use when users want to find public transportation options, get to a station, or find routes between locations.",
    side_effects="Uses Google Places API to find nearby transit stations with directions"
)

@mcp.tool(description=METRO_FINDER_DESCRIPTION.model_dump_json())
async def find_transit_route(
    destination_station: Annotated[str, Field(description="Destination station or location name")] = None,
    starting_station: Annotated[str | None, Field(description="Starting station or location name (optional)")] = None,
    city: Annotated[str | None, Field(description="City name where the locations are situated")] = None,
    current_location: Annotated[str | None, Field(description="Your current location if not at a station (address, landmark, or coordinates)")] = None
) -> str:
    """
    Find public transit routes (metro, bus, train, tram) between locations or from your current location.
    If starting point is not provided, will help find the nearest transit station.
    Specify the city to get more accurate results.
    """
    try:
        # Check if we have the minimum required information
        if not destination_station:
            return ("� **Transit Finder**\n\n"
                   "I need to know where you want to go. Please provide:\n\n"
                   "**Destination**: Name of the location or station you want to reach\n"
                   "**City**: The city where the location is situated\n\n"
                   "**Optional**:\n"
                   "• Starting station or location (if you're already at a known location)\n"
                   "• Your current location (if you need to get to the nearest station first)")
                   
        # Check if city is provided
        if not city:
            return ("� **Transit Finder**\n\n"
                   f"To find accurate transit information about {'both locations' if starting_station else 'the destination'}, "
                   f"I need to know which city {'they are' if starting_station else 'it is'} in.\n\n"
                   "Please provide the city name where the locations are situated.")
        
        # Case 1: User provided current location but no starting station
        if current_location and not starting_station:
            # Find the nearest transit station to the current location first
            nearest_station = await find_nearest_transit_station(current_location)
            
            if not nearest_station:
                return ("❌ Sorry, I couldn't find any transit stations near your location. "
                       "Please try a different location or provide a specific starting station.")
            
            # Get route from nearest station to destination
            destination_with_city = f"{destination_station}, {city}" if city else destination_station
            route = await get_transit_route(nearest_station["name"], destination_with_city)
            
            # Format response with walking directions to the nearest station
            result = f"🚶 **First: Walk to the nearest transit station**\n\n"
            result += f"📍 **From**: {current_location}\n"
            result += f"� **To**: {nearest_station['name']} Station\n"
            result += f"⏱️ **Walking time**: {nearest_station['walking_time']}\n"
            result += f"📏 **Distance**: {nearest_station['distance']}\n"
            result += f"🗺️ **Walking directions**: {nearest_station['walking_directions']}\n\n"
            
            result += f"� **Then: Take public transit**\n\n"
            result += route
            
            return result
            
        # Case 2: User provided starting and destination stations
        elif starting_station:
            # Format station names with city for better search results
            starting_with_city = f"{starting_station}, {city}" if city else starting_station
            destination_with_city = f"{destination_station}, {city}" if city else destination_station
            
            # Get direct route between the two stations
            route = await get_transit_route(starting_with_city, destination_with_city)
            return route
            
        # Case 3: Only destination provided, need to ask for starting point
        else:
            return ("� **Transit Route Planner**\n\n"
                   f"I need to know where you're starting from to get to **{destination_station}** in **{city}**.\n\n"
                   "Please provide either:\n"
                   "• Your current location (address or landmark)\n"
                   "• The station or location you're starting from\n\n"
                   "I'll find the best transit route for you!")
    
    except Exception as e:
        return f"❌ Error finding metro route: {str(e)}"

async def find_nearest_transit_station(location: str) -> dict | None:
    """Find the nearest transit station to a given location"""
    try:
        # First, get coordinates of the input location using Geocoding API
        geocode_params = {
            'address': location,
            'key': GOOGLE_API_KEY
        }
        
        async with httpx.AsyncClient() as client:
            # Get coordinates of the location
            geocode_response = await client.get(
                'https://maps.googleapis.com/maps/api/geocode/json',
                params=geocode_params,
                timeout=30
            )
            
            if geocode_response.status_code != 200:
                return None
            
            geocode_data = geocode_response.json()
            if geocode_data['status'] != 'OK' or not geocode_data['results']:
                return None
            
            # Get coordinates
            location_coords = geocode_data['results'][0]['geometry']['location']
            lat = location_coords['lat']
            lng = location_coords['lng']
            formatted_address = geocode_data['results'][0]['formatted_address']
            
            # Search for nearby transit stations using Places API
            # First, try looking for all public transit stations
            places_params = {
                'location': f"{lat},{lng}",
                'radius': 2000,  # 2km radius
                'type': 'transit_station',  # General transit stations (includes bus, train, metro)
                'key': GOOGLE_API_KEY
            }
            
            places_response = await client.get(
                'https://maps.googleapis.com/maps/api/place/nearbysearch/json',
                params=places_params,
                timeout=30
            )
            
            if places_response.status_code != 200:
                return None
            
            places_data = places_response.json()
            if places_data['status'] != 'OK' or not places_data['results']:
                # Try with bus_station type if transit_station doesn't work
                places_params['type'] = 'bus_station'
                
                places_response = await client.get(
                    'https://maps.googleapis.com/maps/api/place/nearbysearch/json',
                    params=places_params,
                    timeout=30
                )
                
                if places_response.status_code != 200:
                    return None
                
                places_data = places_response.json()
                if places_data['status'] != 'OK' or not places_data['results']:
                    return None
            
            # Get the nearest station (first result)
            station = places_data['results'][0]
            station_name = station['name']
            station_lat = station['geometry']['location']['lat']
            station_lng = station['geometry']['location']['lng']
            
            # Get walking directions to this station
            directions_params = {
                'origin': f"{lat},{lng}",
                'destination': f"{station_lat},{station_lng}",
                'mode': 'walking',
                'key': GOOGLE_API_KEY
            }
            
            directions_response = await client.get(
                'https://maps.googleapis.com/maps/api/directions/json',
                params=directions_params,
                timeout=30
            )
            
            if directions_response.status_code != 200:
                return None
            
            directions_data = directions_response.json()
            if directions_data['status'] != 'OK' or not directions_data['routes']:
                return None
            
            # Get walking time and distance
            leg = directions_data['routes'][0]['legs'][0]
            walking_time = leg['duration']['text']
            distance = leg['distance']['text']
            
            # Create Google Maps walking directions link
            walking_directions = f"https://maps.google.com/maps/dir/{lat},{lng}/{station_lat},{station_lng}/data=!4m2!4m1!3e2"
            
            return {
                "name": station_name,
                "walking_time": walking_time,
                "distance": distance,
                "walking_directions": walking_directions
            }
    
    except Exception as e:
        print(f"Error finding nearest metro station: {str(e)}")
        return None

async def get_transit_route(starting_station: str, destination_station: str) -> str:
    """Get transit route between two locations"""
    try:
        # Log the search query to help debugging
        print(f"Searching for transit route: '{starting_station}' to '{destination_station}'")
        
        # Get transit route using Google Directions API
        params = {
            'origin': starting_station,
            'destination': destination_station,
            'mode': 'transit',
            # Remove transit_mode filter to include all transit types (bus, rail, subway, train, tram)
            'key': GOOGLE_API_KEY
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                'https://maps.googleapis.com/maps/api/directions/json',
                params=params,
                timeout=30
            )
            
            if response.status_code != 200:
                return f"❌ Sorry, I couldn't find a transit route between {starting_station} and {destination_station}."
            
            data = response.json()
            if data['status'] != 'OK' or not data['routes']:
                return f"❌ No public transit routes found between {starting_station} and {destination_station}. The locations may be too far apart or not connected by public transportation."
            
            # Format the response
            route = data['routes'][0]
            leg = route['legs'][0]
            duration = leg['duration']['text']
            distance = leg['distance']['text']
            
            result = f"� **Transit Route**: {starting_station} → {destination_station}\n\n"
            result += f"⏱️ **Total journey time**: {duration}\n"
            result += f"📏 **Distance**: {distance}\n\n"
            result += "**Step-by-step directions**:\n"
            
            # Get detailed steps
            steps = leg['steps']
            step_num = 1
            
            for step in steps:
                travel_mode = step['travel_mode']
                
                if travel_mode == 'WALKING':
                    walk_duration = step['duration']['text']
                    walk_instruction = step['html_instructions'].replace('<div>', '\n').replace('</div>', '')
                    walk_instruction = re.sub('<[^<]+?>', '', walk_instruction)  # Remove HTML tags
                    
                    result += f"{step_num}. 🚶 **Walk**: {walk_instruction} ({walk_duration})\n"
                    step_num += 1
                
                elif travel_mode == 'TRANSIT':
                    transit = step['transit_details']
                    departure_stop = transit['departure_stop']['name']
                    arrival_stop = transit['arrival_stop']['name']
                    line = transit['line']
                    
                    # Get line name/number
                    line_name = line.get('short_name') or line.get('name', 'Transit Line')
                    
                    # Get vehicle type and set appropriate emoji
                    vehicle_type = line.get('vehicle', {}).get('type', '').lower()
                    
                    if vehicle_type == 'subway' or vehicle_type == 'metro_rail':
                        emoji = '🚇'
                        transit_type = 'metro'
                    elif vehicle_type == 'bus':
                        emoji = '🚌'
                        transit_type = 'bus'
                    elif vehicle_type == 'tram':
                        emoji = '🚊'
                        transit_type = 'tram'
                    elif vehicle_type == 'train' or vehicle_type == 'rail':
                        emoji = '🚆'
                        transit_type = 'train'
                    else:
                        emoji = '🚏'
                        transit_type = 'transit'
                    
                    # Get number of stops
                    num_stops = transit.get('num_stops', 0)
                    
                    result += f"{step_num}. {emoji} **{line_name}**: Board the {transit_type} at {departure_stop}\n"
                    result += f"   • Travel {num_stops} stops\n"
                    result += f"   • Get off at {arrival_stop}\n"
                    step_num += 1
            
            # Add Google Maps link
            # Use the full query strings that include city information
            origin_query = starting_station.replace(' ', '+')
            dest_query = destination_station.replace(' ', '+')
            maps_link = f"https://maps.google.com/maps/dir/{origin_query}/{dest_query}/data=!4m2!4m1!3e3"
            result += f"\n🗺️ **View on Google Maps**: {maps_link}\n"
            
            return result
    
    except Exception as e:
        return f"❌ Error getting metro route: {str(e)}"

# --- Run MCP Server ---
async def main():
    print("� Starting Transit Finder MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())
