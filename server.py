import os
from mcp.server.fastmcp import FastMCP
import re
import httpx
from dateutil import parser
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin
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


def normalize_location(name: str) -> str:
    return name.replace("county", "").strip().lower()


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


@mcp.tool(
    annotations={
        "title": "Fetch NC Court Closings",
        "openWorldHint": False,
        "readOnlyHint": True
    }
)
async def fetch_closings(countyname: str) -> str:
    """
    Lookup if a county courthouse or clerk's office is closed/advisory.
    SOURCE: https://www.nccourts.gov/closings
    """
    logger.info("fetch_closings called with countyname=%r", countyname)

    if not countyname:
        return "What county would you like to check on?\n\nSOURCE: https://www.nccourts.gov/closings"

    url = "https://nccourts-01-prod-json.s3.amazonaws.com/juno_alerts.json"
    normalized = normalize_location(countyname.strip().lower())

    description_parts = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.error("HTTP error fetching closings: %s", e)
        return "Sorry, I ran into a error. Please try again later."

    last_county = None

    for alert in data.get("countyalerts", []):
        county = alert.get("county", "").strip()
        if county.lower() != normalized:
            continue

        section = [f"## {county} County"]
        for entry in alert.get("dates", []):
            start_raw = entry.get("startdate")
            end_raw   = entry.get("enddate")

            start = format_date(start_raw) if start_raw else "Unknown"
            end   = format_date(end_raw)   if end_raw   else None

            # Date line
            if start_raw == end_raw or not end:
                section.append(f"**{start}**")
            else:
                section.append(f"**{start} – {end}**")

            info = entry.get("alerts", [{}])[0]
            # Facility
            name = info.get("facility", {}).get("name", "").strip()
            addr = info.get("facility", {}).get("address", "").strip()
            city = info.get("facility", {}).get("city", "").strip()
            zc   = info.get("facility", {}).get("zip", "").strip()
            if name:
                section.append(f"**{name}**")
            if any((addr, city, zc)):
                section.append(f"{addr}, {city} NC {zc}".replace("  ", " ").strip(", "))

            # Description
            desc = info.get("description", "").strip()
            if desc:
                section.append("")  # blank line
                section.append(desc)

            # Office hours
            oa_list = info.get("officealerts", [])
            if oa_list:
                section.append("")
                section.append("### Hours of operation:")
                for oa in oa_list:
                    title   = oa.get("title", "").strip()
                    closing = oa.get("closing", "").strip()
                    if title:
                        section.append(title)
                    if closing:
                        section.append(closing)

            section.append("---")

        if section and section[-1] == "---":
            section.pop()
        description_parts.append("\n\n".join(section))

    full_desc = "\n\n".join(description_parts).strip()
    if not full_desc:
        full_desc = f"No specific advisory or closing is reported for {countyname}."

    return f"{full_desc}\n\nSOURCE: https://www.nccourts.gov/closings"



@mcp.tool(
    annotations={
        "title": "Search NC Court Forms",
        "readOnlyHint": True,
        "openWorldHint": False
    }
)
async def query_court_form(query: str) -> str:
    """
    Search North Carolina court forms by keyword or form number.

    Requires:
      - query (str): keyword or form number to search.

    Returns:
      str: newline-separated list of up to 3 form numbers & names, plus SOURCE URL.
    """
    if not query:
        return f"Sure, what form are you looking for?\n\nSOURCE: https://www.nccourts.gov/forms"

    raw = query.strip().rstrip("?")
    base_url = "https://www.nccourts.gov"
    # 1) Detect if the user actually gave a form number
    form_match = re.search(r"\b[A-Za-z]+-[A-Za-z]-\d+\b", raw)
    if form_match:
        keyword = form_match.group(0)
        is_number_query = True
    else:
        # remove the filler word “form”
        keyword = re.sub(r"(?i)\bform\b", "", raw).strip()
        is_number_query = False

    # ←– INSERT THIS CHECK
    if not is_number_query and not keyword:
        return "Sure, what form are you looking for?\n\nSOURCE: https://www.nccourts.gov/forms"

    url = (
        f"{base_url}/documents/forms"
        f"?contains={keyword}"
        "&field_form_type_target_id=All"
        "&field_language_target_id=All"
    )

    # 2) Fetch and parse
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
    except httpx.HTTPError as e:
        logger.error("Error fetching forms: %s", e)
        return f"Sorry, I couldn’t fetch forms right now.\n\nSOURCE:{url}"

    soup = BeautifulSoup(html, "html.parser")
    items = soup.find_all("article", class_="list__item")

    # 3) Build entries, but if it was a number‐query, only keep exact matches
    entries = []
    for el in items:
        num_tag  = el.select_one("div:nth-child(1) > .badge--pill")
        name_tag = el.select_one("h5 > a")
        if not (num_tag and name_tag):
            continue

        form_number = re.sub(r"\s+", " ", num_tag.get_text()).strip()
        form_name   = re.sub(r"\s+", " ", name_tag.get_text()).strip()
        href        = name_tag.get("href")
        link        = urljoin(base_url, href) if href else url

        # If the user asked for a form number, only include exact matches
        if is_number_query and form_number.lower() != keyword.lower():
            continue

        entries.append(f"- **{form_number}**: {form_name}")

        # If exact-number query, stop after first match
        if is_number_query:
            break

        # Otherwise, limit to 3 total
        if len(entries) >= 3:
            break

    # 4) Format answer
    if entries:
        if is_number_query and len(entries) == 0:
            answer = f"No form *{keyword}* found."
        elif is_number_query:
            answer = f"Found form **{keyword}**:\n\n" + entries[0]
        else:
            answer = f"Here are the top results for **{keyword}**:\n\n" + "\n".join(entries)
    else:
        answer = "No forms found."

    return f"{answer}\n\nSOURCE:{url}"

@mcp.tool(
    annotations={
        "title": "Lookup NC Court Date by Case #",
        "readOnlyHint": True,
        "openWorldHint": False
    }
)
async def court_dates_by_case_number(case_number: str) -> str:
    """
    Search for upcoming North Carolina court dates based on case number.
    
    Requires:
      - case_number (str)

    Returns:
      str: formatted court-date info and SOURCE URL.
    """
    # 1) Prompt if missing
   
    pattern = re.compile(r"^[0-9]{2}[A-Za-z]{2,4}[0-9]{6}-?[0-9]+$")
    if not pattern.match(case_number.strip()):
        return (
            "Sure, what case number would you like to look up?\n\n"
            "It should look like “25CR000000-123” or “25CR000000123”.\n\n"
            f"SOURCE: {DASHBOARD_URL}"
        )
    TOKEN = "2SaxS3OpzTZ4SVh6731f59b991f30294079106ed7d3fefbd2"
    ws_endpoint = f"wss://production-sfo.browserless.io?token={TOKEN}"
    
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(ws_endpoint)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(DASHBOARD_URL, timeout=30000)

            # If CAPTCHA is visible, solve and inject token
            if await page.locator("iframe[src*='recaptcha']").first.is_visible():
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
                return f"No court dates found for {case_number}\n\nSOURCE:{DASHBOARD_URL}"

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
                return f"No court dates found for {case_number}\n\nSOURCE:{DASHBOARD_URL}\n\nNeed Help? Contact the Clerk of Court in the County where the case is assigned for specific case information."

            def format_case_number_hearing_message(result: dict) -> str:
                lines = [
                    f"Here’s what I found for **{result['Style/Defendant'].title()}**:",
                    f"- **Hearing Type:** {result['Hearing Type']}",
                    f"- **Case Category:** {result['Case Category']}",
                    f"- **Case Number:** {result['Case Number']}",
                    f"- **When:** {result['Date/Time']}",
                    f"- **Where:** {result['Courtroom']}",
                    f"\n\nNeed Help? Contact the Clerk of Court in the County where the case is assigned for specific case information"
                ]

                # Only show judge if we have a real name
                judge = result.get('Judge')
                if judge and judge.lower() != 'unknown':
                    lines.append(f"- **Judge:** {judge}")

                return "\n".join(lines)

            first = results[0]  # Use the first result only
            formatted = format_case_number_hearing_message(first)

            return f"{formatted}\n\nSOURCE:{full_url}"

        finally:
            await browser.close()
            logger.info("Court Date Function Complete")


if __name__ == "__main__":
    mcp.run(transport="sse")