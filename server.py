import os
from mcp.server.fastmcp import FastMCP
import requests
import re
import difflib
import httpx
from dateutil import parser
from datetime import datetime
from bs4 import BeautifulSoup
import json
import logging

logging.basicConfig(
    level=logging.INFO,              
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

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
    The user may ask if there is a closing, the courthosue is closed, there is a weather outage, closed due to weather, etc.

    Requires: 'county_name'

    Returns:
         dict: JSON object with two keys—
        answer (str): 'description' containing the formatted alert text,
        source (str): the URL of https://www.nccourts.gov/closings
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

    description = "\n\n".join(description_parts).strip()

    if not description:
        description = (
            f"No specific closing alert is currently posted for '{county_name}'. "
        )

    result = {
        "answer": description,
        "source": "https://www.nccourts.gov/closings"
    }

    fetch_closings_result = json.dumps(result, separators=(",", ":"))
    logger.info(fetch_closings_result)

    return result

@mcp.tool()
async def query_court_form(query: str) -> dict:
    """
    Search North Carolina court forms by keyword.
    The user may ask for a specific form name like Civil Summons or form number like AOC-CV-100

    Requires: 'query'

   Returns:
    dict: JSON object with two keys—
    answer (str): newline-separated list of up to 3 form numbers & names,
    source (str): the URL used for the search.
    """
    suggestion_keyword = query.strip().rstrip('?')
    url = (
        "https://www.nccourts.gov/documents/forms?contains="
        + suggestion_keyword
        + "&field_form_type_target_id=All&field_language_target_id=All"
    )

    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, 'html.parser')
    items = soup.find_all('article', class_='list__item')

    results = []
    for el in items:
        num = el.select_one('div:nth-child(1) > .badge--pill')
        name = el.select_one('h5')
        link = el.select_one('h5 > a')
        if num and name and link:
            form_number = re.sub(r"(\r\n|\n|\r)", "", num.text).strip()
            form_name = re.sub(r"(\r\n|\n|\r)", "", name.text).strip()
            results.append(f"{form_number} - {form_name}")

    if not results:
        return {"answer": "No forms found.", "source": url}

    print(results)
    answer = "\n".join(results[:3])

    result = {
        "answer": answer,
        "source": url
    }

    query_forms_result = json.dumps(result, separators=(",", ":"))
    logger.info(query_forms_result)

    return result



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    mcp.run(transport="sse", host="0.0.0.0", port=port)