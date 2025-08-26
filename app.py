from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import re

DIY_URL = "https://holidayz.makemytrip.com/holidays/diyPlanner"
DETAIL_PREFIX = "https://holidayz.makemytrip.com/holidays/india/package?itineraryId="
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)

def extract_uuid_from_url(url: str):
    try:
        qs = parse_qs(urlparse(url).query)
        if "itineraryId" in qs and qs["itineraryId"]:
            return qs["itineraryId"][0]
    except Exception:
        pass
    m = UUID_RE.search(url or "")
    return m.group(0) if m else None


app = FastAPI()

class BuildRequest(BaseModel):
    from_city: str
    to_city: str
    nights: int
    departure: str   # format: "30 October 2025"
    adults: int = 2
    roundtrip: bool = True


@app.post("/build-itinerary")
async def build_itinerary(req: BuildRequest):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
                "--disable-http2"
            ]
        )
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        # 1. Open DIY planner
        await page.goto(DIY_URL, wait_until="domcontentloaded")

        # 2. FROM CITY
        await page.locator("input[name='fromCity']").click()
        await page.locator("input.holidays-citypicker-input[placeholder='From']").fill(req.from_city)
        await page.wait_for_selector(".react-autosuggest__suggestion")
        await page.locator(".react-autosuggest__suggestion").first.click()

        # 3. TO CITY
        await page.locator("input[name='destCity0']").click()
        await page.locator("input.holidays-citypicker-input[placeholder='To']").fill(req.to_city)
        await page.wait_for_selector(".react-autosuggest__suggestion")
        await page.locator(".react-autosuggest__suggestion").first.click()

        # 4. Nights
        current_text = await page.locator(".diy-nights-text").inner_text()
        try:
            current_nights = int(current_text.split()[0])
        except Exception:
            current_nights = 1
        while current_nights < req.nights:
            await page.locator("button[data-testid='diy-nights-add-btn-landing']").click()
            current_nights += 1
        while current_nights > req.nights:
            await page.locator("button[data-testid='diy-nights-remove-btn-landing']").click()
            current_nights -= 1

        # 5. Departure Date
        await page.locator("input[name='deptDate']").click()
        target_date = datetime.strptime(req.departure, "%d %B %Y")
        target_month_year = target_date.strftime("%B %Y")
        target_day = target_date.strftime("%-d")
        aria_match = target_date.strftime(f"%A, %B {int(target_day)}, %Y")

        while True:
            visible_caption = await page.locator(".DayPicker-Caption").first.inner_text()
            if target_month_year in visible_caption:
                break
            await page.locator(".DayPicker-NavButton--next").click()
            await page.wait_for_timeout(500)

        await page.locator(f".DayPicker-Day[aria-label='{aria_match}']").click()

        # 6. Rooms & Guests (always 2 Adults → just click APPLY)
        await page.locator("input[name='roomsCount']").click()
        await page.locator("button.applyBtn", has_text="APPLY").click()

        # 7. CREATE ITINERARY
        await page.locator("button[data-testid='diy-form-create-btn']").click()

        # 8. Select Route (Step 2/3)
        await page.wait_for_selector("div.options-cards-parent")
        if req.roundtrip:
            await page.locator("div.option-heading-text", has_text="Round Trip Flights").click()
        else:
            await page.locator("div.option-heading-text").first.click()
        await page.locator("button.diy-button", has_text="PROCEED").click()

        # 9. Step 3: click DONE and wait for new tab with final itinerary link
        async with context.expect_page() as final_page_info:
            await page.locator("button.diy-button", has_text="DONE").click()
        final_page = await final_page_info.value
        await final_page.wait_for_url("**/holidays/india/package?itineraryId=*", timeout=20000)

        final_url = final_page.url
        uuid = extract_uuid_from_url(final_url)

        await browser.close()

        if not uuid:
            raise HTTPException(status_code=500, detail="Could not extract itineraryId")

        return JSONResponse({
            "itineraryId": uuid,
            "url": final_url
        })
