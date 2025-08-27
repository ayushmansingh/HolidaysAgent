from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List
from playwright.async_api import async_playwright
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import re

DIY_URL = "https://holidayz.makemytrip.com/holidays/diyPlanner"
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}")

def extract_uuid_from_url(url: str):
    try:
        qs = parse_qs(urlparse(url).query)
        if 'itineraryId' in qs and qs['itineraryId']:
            return qs['itineraryId'][0]
    except Exception:
        pass
    m = UUID_RE.search(url or "")
    return m.group(0) if m else None

app = FastAPI()

class Destination(BaseModel):
    city: str
    nights: int

class BuildRequest(BaseModel):
    from_city: str
    departure: str   # e.g. "30 October 2025"
    destinations: List[Destination]
    adults: int = 2
    roundtrip: bool = True

@app.post("/build-itinerary")
async def build_itinerary(req: BuildRequest):
    if not req.destinations:
        raise HTTPException(status_code=400, detail="At least one destination required")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()

        # 1. Open DIY planner
        await page.goto(DIY_URL, wait_until="domcontentloaded")

        # 2. FROM CITY
        await page.locator("input[name='fromCity']").click()
        await page.locator("input.holidays-citypicker-input[placeholder='From']").fill(req.from_city)
        await page.wait_for_selector(".holidays-citypicker-list li")
        await page.locator(".holidays-citypicker-list li", has_text=req.from_city).first.click()

        # 3. DESTINATIONS
        for i, dest in enumerate(req.destinations):
            if i > 0:
                # Add new destination
                await page.locator("button", has_text="ADD").click()

            dest_input = f"input[name='destCity{i}']"
            await page.locator(dest_input).click()
            await page.locator("input.holidays-citypicker-input[placeholder='To']").fill(dest.city)
            await page.wait_for_selector(".holidays-citypicker-list li")
            await page.locator(".holidays-citypicker-list li", has_text=dest.city).first.click()

            # Adjust nights for this destination
            current_text = await page.locator(".diy-nights-text").nth(i).inner_text()
            current_nights = int(current_text.split()[0])
            while current_nights < dest.nights:
                await page.locator("button[data-testid='diy-nights-add-btn-landing']").nth(i).click()
                current_nights += 1
            while current_nights > dest.nights:
                await page.locator("button[data-testid='diy-nights-remove-btn-landing']").nth(i).click()
                current_nights -= 1

        # 4. Departure Date
        await page.locator("input[name='deptDate']").click()
        target_date = datetime.strptime(req.departure, "%d %B %Y")  # e.g. "30 October 2025"
        target_month_year = target_date.strftime("%B %Y")
        aria_match = target_date.strftime("%a %b %d %Y")  # e.g. "Thu Oct 30 2025"

        while True:
            visible_caption = await page.locator(".DayPicker-Caption div").first.inner_text()
            if visible_caption.strip() == target_month_year:
                break
            await page.locator(".DayPicker-NavButton--next").click()
            await page.wait_for_timeout(500)

        await page.locator(f".DayPicker-Day[aria-label='{aria_match}']").click()

        # 5. Rooms & Guests
        await page.locator("input[name='roomsCount']").click()
        await page.locator("button.applyBtn").click()

        # 6. CREATE ITINERARY
        await page.locator("button[data-testid='diy-form-create-btn']").click()

        # 7. Select Route
        await page.wait_for_selector("div.options-cards-parent")
        await page.locator("div.option-heading-text", has_text="Round Trip Flights").click()
        await page.locator("button.diy-button[data-testid='diydetails-done-button']", has_text="PROCEED").click()

        # 8. Final DONE (opens new tab)
        await page.wait_for_selector("button.diy-button[data-testid='diydetails-done-button']", timeout=10000)
        async with context.expect_page() as final_page_info:
            await page.locator(
                "button.diy-button[data-testid='diydetails-done-button']", has_text="DONE"
            ).click()

        final_page = await final_page_info.value
        await final_page.wait_for_url("**/holidays/india/package?itineraryId=*", timeout=20000)

        final_url = final_page.url
        uuid = extract_uuid_from_url(final_url)

        await browser.close()

        if not uuid:
            raise HTTPException(status_code=504, detail="No itineraryId found in final URL")

        return JSONResponse({
            "itineraryId": uuid,
            "url": final_url
        })
