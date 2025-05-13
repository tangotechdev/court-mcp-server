from mcp.server.fastmcp import FastMCP
import requests
import re
import difflib
import httpx
from dateutil import parser
from datetime import datetime


mcp = FastMCP("Court Tools")


def normalize_location(county_name: str) -> str:
    county_name = county_name.strip()
    if ", county" in county_name.lower() or "county" in county_name.lower():
        return county_name  # already valid
    return f"{county_name}"


def format_date(date_str: str) -> str:
    """Parse an ISO date string and format it as 'Weekday, MM/DD/YYYY'."""
    dt = parser.isoparse(date_str)
    return dt.strftime("%A, %m/%d/%Y")




@mcp.tool()
async def fetch_closings(county_name: str) -> dict:
    """
    Find if a courthouse or clerks office or county is closed based on a county name.
    The user may ask if the courthosue is closed, there is a weather outage, closed due to weather, etc.

    Requires: 'county_name'

    Returns:
        A dict with a single key 'description' containing the formatted alert text.
    """
    url = "https://nccourts-01-prod-json.s3.amazonaws.com/juno_alerts.json"
    county_lower = county_name.strip().lower()

    normalized_location = normalize_location(county_lower)

    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    description_parts = []

    for alert in data.get("countyalerts", []):
        if alert.get("county", "").lower() == normalized_location:
            for entry in alert.get("dates", []):
                lines = ["**Closing Alert**"]
                start = format_date(entry["startdate"])
                end = format_date(entry["enddate"])
                if entry["startdate"] == entry["enddate"]:
                    lines.append(f"Date: {start}")
                else:
                    lines.append(f"Dates: {start} to {end}")

                # Optional description
                desc = entry.get("alerts", [{}])[0].get("description")
                if desc:
                    lines.append(desc)

                # Office-specific closings
                office_alerts = entry.get("alerts", [{}])[0].get("officealerts", [])
                for oa in office_alerts:
                    title = oa.get("title", "").strip()
                    closing = oa.get("closing", "").strip()
                    lines.append(f"{title} – **{closing}**")

                # Alternate filing location info
                alt = entry["alerts"][0]
                alt_fields = [
                    alt.get("filinginstructions"),
                    alt.get("alternatename"),
                    alt.get("alternateaddress"),
                    None if not (alt.get("alternatecity") or alt.get("alternatezip")) 
                         else f"{alt.get('alternatecity', '')}, {alt.get('alternatezip', '')}",
                    alt.get("alternatephone"),
                ]
                alt_fields = [f for f in alt_fields if f]
                if alt_fields:
                    lines.append("")
                    lines.append("**Alternate filing location:**")
                    lines.extend(alt_fields)

                description_parts.append("\n".join(lines))

    return {"description": "\n\n".join(description_parts).strip()}


if __name__ == "__main__":
    mcp.run(transport="sse")