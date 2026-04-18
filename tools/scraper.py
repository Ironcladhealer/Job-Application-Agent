#Imports
import asyncio
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page, TimeoutError as PWTimeout
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.models import Job, JobStatus, AgentRun, RunStatus

#Cookie Persistence — The Key to Avoiding Re-Login
COOKIES_FILE = Path("linkedin_cookies.json")

async def save_cookies(context: BrowserContext):
    """
    Save the browser session cookies to disk after login.
    
    A "cookie" is a small token the server sends your browser to prove
    "this browser is logged in as user X." Every subsequent request
    includes this token, so the server doesn't ask you to log in again.
    
    By saving cookies to a JSON file, we can reload them in future
    Playwright runs — skipping the login form entirely. LinkedIn sessions
    typically last 1-2 weeks before expiring.
    """
    cookies = await context.cookies()
    COOKIES_FILE.write_text(json.dumps(cookies))

async def load_cookies(context: BrowserContext) -> bool:
    """
    Load saved cookies into the browser context.
    Returns True if cookies were found and loaded, False if we need to log in.
    """
    if not COOKIES_FILE.exists():
        return False
    
    cookies = json.loads(COOKIES_FILE.read_text())
    await context.add_cookies(cookies)
    return True

#The Browser Setup
async def create_browser_context(playwright):
    """
    Creates a Chromium browser with a realistic fingerprint.
    
    WHY these specific options?
    
    headless=True for production runs (no visible window).
    Set headless=False during development — watching Playwright
    navigate in real time is the fastest way to debug selector issues.
    
    The user_agent, viewport, and locale together form a "fingerprint."
    LinkedIn's bot detection compares your fingerprint against known
    headless browser signatures. We mimic a real Windows Chrome user.
    """
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            # This flag removes the "Chrome is being controlled by
            # automated software" banner AND the navigator.webdriver
            # property that bot detectors check for.
        ]
    )

    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        timezone_id="Asia/Karachi",
    )

    # Override the webdriver property in JavaScript.
    # navigator.webdriver = true is the most common bot detection signal.
    # This script runs on every page before any other JS executes.
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
    """)

    return browser, context

#Login Flow
async def login_to_linkedin(page: Page):
    """
    Fills the LinkedIn login form and waits for the feed to load.
    
    WHY type character by character instead of page.fill()?
    page.fill() dumps all text instantly — no human types that fast.
    page.type() with a delay sends one keypress at a time with a
    randomized interval, mimicking real typing speed. Small detail,
    but it affects bot scoring on sites that measure input timing.
    """
    await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    
    await page.wait_for_selector("#username", timeout=10_000)

    # Type email slowly
    await page.click("#username")
    await page.type("#username", settings.LINKEDIN_EMAIL, delay=random.randint(50, 120))
    
    await asyncio.sleep(random.uniform(0.5, 1.2))  # human pause between fields
    
    # Type password slowly
    await page.click("#password")
    await page.type("#password", settings.LINKEDIN_PASSWORD, delay=random.randint(50, 120))
    
    await asyncio.sleep(random.uniform(0.3, 0.8))
    
    await page.click('button[type="submit"]')
    
    # Wait for the feed — this confirms login succeeded.
    # If LinkedIn shows a CAPTCHA or verification, this will time out.
    # We raise a clear error instead of silently continuing with a broken session.
    try:
        await page.wait_for_url("**/feed/**", timeout=15_000)
    except PWTimeout:
        raise RuntimeError(
            "LinkedIn login failed or hit a CAPTCHA. "
            "Run with headless=False to see what happened, "
            "then complete the verification manually and re-save cookies."
        )
    
    print("LinkedIn login successful.")

#The core scrape function
async def scrape_linkedin_jobs(
    keyword: str,
    location: str = "Pakistan",
    max_jobs: int = 20,
) -> list[dict]:
    """
    Scrapes LinkedIn job search results for a given keyword.
    Returns a list of job dicts ready to be inserted into the DB.
    
    The flow:
      1. Launch browser
      2. Try to load saved cookies (skip login if valid)
      3. Navigate to search results
      4. Scroll to load all cards
      5. Click each card to get full description
      6. Return structured list
    """
    jobs = []

    async with async_playwright() as p:
        browser, context = await create_browser_context(p)
        page = await context.new_page()

        # --- Session management ---
        cookies_loaded = await load_cookies(context)

        if cookies_loaded:
            # Verify the session is still valid by hitting a protected page
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            await asyncio.sleep(2)

            # If we got redirected to login, cookies expired
            if "login" in page.url or "authwall" in page.url:
                print("Saved cookies expired. Logging in again...")
                await login_to_linkedin(page)
                await save_cookies(context)
        else:
            print("No saved cookies found. Logging in...")
            await login_to_linkedin(page)
            await save_cookies(context)

        # --- Navigate to job search ---
        search_url = (
            f"https://www.linkedin.com/jobs/search/"
            f"?keywords={keyword.replace(' ', '%20')}"
            f"&location={location.replace(' ', '%20')}"
            f"&f_TPR=r86400"   # posted in last 24 hours — freshness filter
            f"&sortBy=DD"      # sort by date, not LinkedIn's "relevance" algo
        )

        await page.goto(search_url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(2, 4))  # let the page settle

        # --- Scroll to load cards ---
        # LinkedIn loads ~25 jobs initially, then more as you scroll.
        # We scroll until we have enough or hit the bottom.
        await scroll_to_load_jobs(page, target_count=max_jobs)

        # --- Extract job list ---
        job_cards = await page.query_selector_all(".job-card-container")
        print(f"Found {len(job_cards)} job cards.")

        for i, card in enumerate(job_cards[:max_jobs]):
            job = await extract_job_from_card(page, card, i)
            if job:
                jobs.append(job)
            
            # Random delay between card clicks — looks human, avoids rate limiting
            await asyncio.sleep(random.uniform(1.5, 3.5))

        await browser.close()

    return jobs

#Scroll Logic
async def scroll_to_load_jobs(page: Page, target_count: int):
    """
    Scrolls the job list panel to trigger lazy loading.
    
    LinkedIn's layout has TWO scrollable areas: the whole page, and the
    left panel containing job cards. We need to scroll the LEFT PANEL,
    not the window. This is a common Playwright mistake — window.scrollBy
    does nothing when the scrollable element is a div, not the body.
    
    We target the specific scrollable container with its CSS selector
    and call scrollBy on that element directly via page.evaluate().
    """
    for attempt in range(8):  # max 8 scroll attempts
        # Count current cards
        cards = await page.query_selector_all(".job-card-container")
        if len(cards) >= target_count:
            break

        # Scroll the jobs panel, not the window
        await page.evaluate("""
            const panel = document.querySelector('.jobs-search-results-list');
            if (panel) {
                panel.scrollBy(0, panel.clientHeight);
            }
        """)

        await asyncio.sleep(random.uniform(1.5, 2.5))
        print(f"  Scroll {attempt + 1}: {len(cards)} cards loaded so far...")

#Extracting data from each card
async def extract_job_from_card(page: Page, card, index: int) -> Optional[dict]:
    """
    Clicks a job card to open its detail panel, then extracts all fields.
    
    WHY CLICK INSTEAD OF JUST READING THE CARD?
    Job cards show: title, company, location. That's it.
    The description — which is what Gemini needs to score and gap-analyse —
    is only visible in the RIGHT PANEL after you click the card.
    
    So the flow is: click card → wait for right panel → extract description.
    This is slow (1-3 seconds per job) but unavoidable without an API.
    """
    try:
        # Scroll card into view before clicking — Playwright can't click
        # elements that are outside the visible viewport
        await card.scroll_into_view_if_needed()
        await card.click()

        # Wait for the job detail panel to load
        await page.wait_for_selector(".job-view-layout", timeout=8_000)
        await asyncio.sleep(random.uniform(0.8, 1.5))

        # Extract everything from the detail panel using one evaluate() call.
        # WHY ONE evaluate() CALL?
        # Each query_selector call from Python is a round-trip to the browser
        # process. For 5 fields × 20 jobs = 100 round trips.
        # One evaluate() that runs JS inside the browser returns everything
        # in a single call. Much faster.
        data = await page.evaluate("""
            () => {
                const get = (selector, attr = 'innerText') => {
                    const el = document.querySelector(selector);
                    return el ? (attr === 'innerText' ? el.innerText.trim() : el.getAttribute(attr)) : null;
                };

                return {
                    title:       get('.job-details-jobs-unified-top-card__job-title'),
                    company:     get('.job-details-jobs-unified-top-card__company-name'),
                    location:    get('.job-details-jobs-unified-top-card__bullet'),
                    description: get('.jobs-description__content'),
                    url:         window.location.href,
                };
            }
        """)

        # Guard: skip cards with missing critical fields
        if not data.get("title") or not data.get("url"):
            print(f"  Card {index}: missing title or URL, skipping.")
            return None

        # Clean the URL — LinkedIn appends tracking params we don't need
        # and the URL changes slightly between sessions.
        # We normalize to just the job ID portion for deduplication.
        url = data["url"].split("?")[0].rstrip("/")

        return {
            "url": url,
            "title": data["title"],
            "company": data["company"] or "Unknown",
            "location": data["location"],
            "description": data["description"],
            "scraped_at": datetime.now(timezone.utc),
            "status": JobStatus.NEW.value,
        }

    except PWTimeout:
        print(f"  Card {index}: timed out waiting for detail panel, skipping.")
        return None
    except Exception as e:
        print(f"  Card {index}: error — {e}")
        return None

#Saving to Database
async def save_jobs_to_db(jobs: list[dict], session: AsyncSession) -> tuple[int, int]:
    """
    Inserts scraped jobs into the database.
    Returns (new_count, duplicate_count).
    
    THE DEDUP PATTERN:
    We don't query "does this URL exist?" before inserting.
    That's two operations: SELECT then INSERT. In concurrent code,
    another process could insert between your SELECT and INSERT —
    a race condition. Instead, we just INSERT and catch the IntegrityError
    that fires when unique=True is violated. One operation, no race condition.
    This pattern is called "insert or ignore" or "upsert-lite."
    """
    new_count = 0
    duplicate_count = 0

    for job_data in jobs:
        try:
            job = Job(**job_data)
            session.add(job)
            await session.flush()   # flush sends the INSERT to SQLite but
                                    # doesn't commit yet — lets us catch errors
                                    # per-row without rolling back everything
            new_count += 1
            print(f"  ✓ Saved: {job_data['title']} @ {job_data['company']}")

        except IntegrityError:
            await session.rollback()  # must rollback after any error
            duplicate_count += 1
            print(f"  ↩ Duplicate skipped: {job_data['url']}")

    return new_count, duplicate_count

#The Entry Point
async def run_scrape_phase(session: AsyncSession, keyword: str = "backend engineer"):
    """
    Full scrape phase: scrape LinkedIn → save to DB → log the run.
    
    This is what the orchestrator calls. It handles:
      - Creating an AgentRun record (audit log)
      - Running the scraper
      - Saving results to DB
      - Updating the run record with final counts and status
    
    WHY WRAP EVERYTHING IN A TRY/EXCEPT AT THIS LEVEL?
    If the scraper crashes halfway through, we still want to:
      1. Save whatever jobs we already scraped
      2. Mark the run as FAILED (not leave it stuck as RUNNING)
      3. Store the error so you can see it in the dashboard
    Letting the exception propagate would skip all three.
    """
    run = AgentRun(phase="scrape", status=RunStatus.RUNNING.value)
    session.add(run)
    await session.flush()

    try:
        print(f"Starting scrape for: '{keyword}'")
        raw_jobs = await scrape_linkedin_jobs(keyword=keyword, max_jobs=20)

        new_count, dup_count = await save_jobs_to_db(raw_jobs, session)

        run.status = RunStatus.DONE.value
        run.jobs_found = new_count
        run.completed_at = datetime.now(timezone.utc)
        run.log = f"Scraped {len(raw_jobs)} listings. {new_count} new, {dup_count} duplicates skipped."

        print(f"\nScrape complete: {new_count} new jobs, {dup_count} skipped.")

    except Exception as e:
        run.status = RunStatus.FAILED.value
        run.error = str(e)
        run.completed_at = datetime.now(timezone.utc)
        print(f"Scrape failed: {e}")
        raise
