import os
from mcp.server.fastmcp import FastMCP
import requests
import re
import difflib
import httpx
from dateutil import parser
from datetime import datetime
from bs4 import BeautifulSoup
from typing import Optional
import json
import logging
import asyncio
from datetime import datetime, timedelta
from python_anticaptcha import AnticaptchaClient, NoCaptchaTaskProxylessTask
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,              
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
DASHBOARD_URL = "https://portal-nc.tylertech.cloud/Portal/Home/Dashboard/26"
SITE_KEY = "6LfqmHkUAAAAAAKhHRHuxUy6LOMRZSG2LvSwWPO9"
ANTICAPTCHA_KEY = os.getenv("ANTICAPTCHA_KEY", "f438aa48dc4f094f0add4d5fce564c27")
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG")


today = datetime.today()
future = today + timedelta(days=5 * 365)

port = int(os.environ.get("PORT", 10000))

logger = logging.getLogger(__name__)

mcp = FastMCP("Court Tools", host="0.0.0.0", port=port)


def normalize_location(county_name: str) -> str:
    county_name = county_name.strip()
    if ", county" in county_name.lower() or "county" in county_name.lower():
        return county_name  # already valid
    return f"{county_name}"


def format_date(date_str: str) -> str:
    """Parse an ISO date string and format it as 'Weekday, MM/DD/YYYY'."""
    dt = parser.isoparse(date_str)
    return dt.strftime("%A, %m/%d/%Y")


async def solve_captcha_async():
    def _solve():
        client = AnticaptchaClient(ANTICAPTCHA_KEY)
        task = NoCaptchaTaskProxylessTask(website_url=DASHBOARD_URL, website_key=SITE_KEY)
        job = client.createTask(task)
        job.join()  # Blocking call
        return job.get_solution_response()

    token = await asyncio.to_thread(_solve)
    if not token:
        raise RuntimeError("CAPTCHA solving failed – empty token.")
    return token


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
            f"No specific adviosry or closing is reported for {county_name}. "
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
            results.append(f"{form_number} - {form_name}\n\n")

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

@mcp.tool()
async def court_dates_by_case_number(case_number: str) -> dict:
    """
    Search for upcoming North Carolina court dates based on case number.
    The user may ask for court date or when is my court date.
    THe case_number needs to be formatted like: 25CR000000-123

    Requires: 'case_number'

    Returns:
        dict: JSON object with the court date info and a link to the case.
    """
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(DASHBOARD_URL, timeout=30000)

            # If CAPTCHA is visible, solve and inject token
            if await page.locator("iframe[src*='recaptcha']").first.is_visible(timeout=5000):
                logger.info("CAPTCHA detected. Solving...")
                token = await solve_captcha_async()
                await page.evaluate("""
                    (token) => {
                        let el = document.getElementById('g-recaptcha-response');
                        if (!el) {
                            el = document.createElement('textarea');
                            el.id = 'g-recaptcha-response';
                            el.name = 'g-recaptcha-response';
                            el.style.display = 'block';
                            document.body.appendChild(el);
                        }
                        el.value = token;
                        el.style.display = 'block';
                    }
                """, token)
            await page.wait_for_function("document.getElementById('g-recaptcha-response').value.length > 0")
            logger.info("CAPTCHA solved and token injected.")

            await page.select_option("#cboHSLocationGroup", label="All Locations")
            await page.select_option("#cboHSHearingTypeGroup", label="All Hearing Types")
            await page.select_option("#cboHSSearchBy", label="Case Number")

            await page.fill("#SearchCriteria_SearchValue", case_number)
            await page.fill("#SearchCriteria_DateFrom", today.strftime("%m/%d/%Y"))
            await page.fill("#SearchCriteria_DateTo", future.strftime("%m/%d/%Y"))
            await page.click("#btnHSSubmit")
            logger.info("Submit Data")
            try:
                await page.wait_for_function(
                    "() => document.querySelectorAll('td.data-heading a.caseLink').length > 0",
                    timeout=5000
                )
            except:
                return {"answer": f"No court dates found for case {case_number}.", "source": DASHBOARD_URL}

            tbody = page.locator("#hearingResultsGrid table tbody")
            rows = await tbody.locator("tr").all()

            results = []
            for row in rows:
                try:
                    logger.info("Found Court Date")
                    case_number_elem = row.locator("td.data-heading a.caseLink")
                    if await case_number_elem.count() == 0:
                        continue

                    full_case_number = (await case_number_elem.inner_text()).strip()
                    relative_url = await case_number_elem.get_attribute("data-url")
                    full_url = f"https://portal-nc.tylertech.cloud{relative_url.strip()}" if relative_url else ""

                    async def safe_text(selector):
                        el = row.locator(selector)
                        if await el.count() > 0:
                            text = await el.inner_text()
                            return text.strip()
                        return ""



                    result = {
                        "Case Number": full_case_number,
                        "Style/Defendant": await safe_text("td:nth-child(2)"),
                        "Case Type": await safe_text("td:nth-child(3)"),
                        "Date/Time": await safe_text("td:nth-child(4)"),
                        "Hearing Type": await safe_text("td:nth-child(5)"),
                        "Judge": await safe_text("td:nth-child(6)"),
                        "Courtroom": await safe_text("td:nth-child(7)"),
                        "Case Category": await safe_text("td:nth-child(8)"),
                        "Detail URL": full_url,
                    }
                    results.append(result)
                except Exception as e:
                    logger.error(f"Error processing row: {e}")

            if not results:
                return {"answer": f"No court dates found for case {case_number}.", "source": DASHBOARD_URL}

            def format_case_number_hearing_message(result: dict) -> str:
                lines = [
                    f"Here’s what I found for {result['Style/Defendant'].title()}:",
                    f"- **Hearing Type:** {result['Hearing Type']}",
                    f"- **Case Category:** {result['Case Category'].lower()}",
                    f"- **Case Number:** {result['Case Number']}",
                    f"- **When:** {result['Date/Time']}",
                    f"- **Where:** {result['Courtroom']}",
                ]

                # Only show judge if we have a real name
                judge = result.get('Judge')
                if judge and judge.lower() != 'unknown':
                    lines.append(f"- **Judge:** {judge}")

                return "\n".join(lines)

            first = results[0]  # Use the first result only
            formatted = format_case_number_hearing_message(first)

            return {
                "answer": formatted,
                "source": full_url
            }

        finally:
            await browser.close()
            logger.info("Court Date Function Complete")


@mcp.tool()
async def court_dates_by_name(first_name: str, last_name: str, county_name: str) -> dict:
    """
    Search for upcoming North Carolina court dates by party name and county.

    Q: I need court date for Jane Smith in Durham County
    A: { "first_name": "Jane",   "last_name": "Smith",  "county_name": "Durham"   }

    Q: I need my court date for Melody Barto in Randolph County
    A: { "first_name": "Melody", "last_name": "Barto",  "county_name": "Randolph" }

    Arguments:
    - first_name (str): Required.
    - last_name (str): Required.
    - county_name (str): Required. County to filter by (e.g., "Pitt").

    Returns:
        dict: One court date results. Prompts user if more detail is needed.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(DASHBOARD_URL, timeout=30000)

            if await page.locator("iframe[src*='recaptcha']").first.is_visible(timeout=5000):
                logger.info("CAPTCHA detected. Solving...")
                token = await solve_captcha_async()
                await page.evaluate("""
                    (token) => {
                        let el = document.getElementById('g-recaptcha-response');
                        if (!el) {
                            el = document.createElement('textarea');
                            el.id = 'g-recaptcha-response';
                            el.name = 'g-recaptcha-response';
                            el.style.display = 'block';
                            document.body.appendChild(el);
                        }
                        el.value = token;
                        el.style.display = 'block';
                    }
                """, token)
            await page.wait_for_function("document.getElementById('g-recaptcha-response').value.length > 0")
            logger.info("CAPTCHA solved and token injected.")

            def extract_county_name(text: str) -> str:
                match = re.search(r"in ([A-Z][a-z]+)\s+(County|Co\.?)", text)
                if match:
                    return match.group(1)
                return ""

            county_clean = re.sub(r"\s+(County|Co\.?)$", "", county_name.strip(), flags=re.IGNORECASE)
            location_label = f"{county_clean} County"
            await page.select_option("#cboHSLocationGroup", label=location_label)
            await page.select_option("#cboHSHearingTypeGroup", label="All Hearing Types")
            await page.select_option("#cboHSSearchBy", label="Party Name")

            await page.fill("#txtHSLastName", last_name)
            await page.fill("#txtHSFirstName", first_name)
            
            await page.fill("#SearchCriteria_DateFrom", today.strftime("%m/%d/%Y"))
            await page.fill("#SearchCriteria_DateTo", future.strftime("%m/%d/%Y"))
            logger.info(f"Search Inputs -> First Name: {first_name}, Last Name: {last_name}, County: {location_label}")

            await page.click("#btnHSSubmit")
            logger.info("Submit Data")

            try:
                await page.wait_for_function(
                    "() => document.querySelectorAll('td.data-heading a.caseLink').length > 0",
                    timeout=5000
                )
            except:
                return {"answer": f"No court dates found for {first_name} {last_name}.", "source": DASHBOARD_URL}

            tbody = page.locator("#hearingResultsGrid table tbody")
            rows = await tbody.locator("tr").all()

            results = []
            for row in rows:
                try:
                    case_number_elem = row.locator("td.data-heading a.caseLink")
                    if await case_number_elem.count() == 0:
                        continue

                    full_case_number = (await case_number_elem.inner_text()).strip()
                    relative_url = await case_number_elem.get_attribute("data-url")
                    full_url = f"https://portal-nc.tylertech.cloud{relative_url.strip()}" if relative_url else ""

                    async def safe_text(selector):
                        el = row.locator(selector)
                        if await el.count() > 0:
                            text = await el.inner_text()
                            return text.strip()
                        return ""

                    result = {
                        "Case Number": full_case_number,
                        "Style/Defendant": await safe_text("td:nth-child(2)"),
                        "Case Type": await safe_text("td:nth-child(3)"),
                        "Date/Time": await safe_text("td:nth-child(4)"),
                        "Hearing Type": await safe_text("td:nth-child(5)"),
                        "Judge": await safe_text("td:nth-child(6)"),
                        "Courtroom": await safe_text("td:nth-child(7)"),
                        "Case Category": await safe_text("td:nth-child(8)"),
                        "Detail URL": full_url,
                    }
                    results.append(result)
                except Exception as e:
                    logger.error(f"Error processing row: {e}")

            if not results:
                return {"answer": f"No court dates found for {first_name} {last_name}.", "source": DASHBOARD_URL}

            limited_results = results[:3]

            def format_hearing_message(result: dict) -> str:
                lines = [
                    f"Here’s what I found for {result['Style/Defendant'].title()}:",
                    f"- **Hearing Type:** {result['Hearing Type']}",
                    f"- **Case Category:** {result['Case Category'].lower()}",
                    f"- **Case Number:** {result['Case Number']}",
                    f"- **When:** {result['Date/Time']}",
                    f"- **Where:** {result['Courtroom']}",
                ]

                # Only show judge if we have a real name
                judge = result.get('Judge')
                if judge and judge.lower() != 'unknown':
                    lines.append(f"- **Judge:** {judge}")

                return "\n".join(lines)


            if len(results) > 1:
            # replace the multi-result summary with a single apology + Learn More link
                formatted = (
                    "Sorry, I found more than one court date. "
                    f"Please click [Learn More]({DASHBOARD_URL}) below."
                )
            else:
                formatted = format_hearing_message(results[0])

            return {
                "answer": formatted,
                "source": full_url
            }


        finally:
            await browser.close()
            logger.info("Court Date Function Complete")


if __name__ == "__main__":
    mcp.run(transport="sse")