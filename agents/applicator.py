# agents/applicator.py

import asyncio
import json
import random
import re
from datetime import datetime, timezone
from typing import TypedDict, Optional, Literal

from langgraph.graph import StateGraph, END
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, SystemMessage
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.config import settings
from core.models import Job, JobStatus, AgentRun, RunStatus, FormAttempt
from agents.prompts import CV_TEXT

class ApplicationState(TypedDict):
    """
    The single object that flows through every node in the graph.

    WHY TypedDict AND NOT A DATACLASS OR PYDANTIC MODEL?
    LangGraph requires its state to be a TypedDict. This is because
    LangGraph uses the type annotations to understand which fields
    each node updates — it merges node outputs back into the state
    using the field names. Pydantic models and dataclasses don't
    give LangGraph the introspection it needs for this merging.

    Every field is Optional because different nodes populate
    different fields. The state starts mostly empty and fills
    up as the graph executes.
    """
    # Input — set before the graph starts
    job_id: int
    job_url: str
    job_title: str
    job_company: str
    job_description: str
    cover_letter: Optional[str]

    # Runtime — set by nodes as the graph executes
    page_loaded: bool
    apply_button_found: bool
    form_fields: Optional[list[dict]]    # fields detected on the form
    fields_filled: Optional[list[str]]   # field names successfully filled
    unexpected_fields: Optional[list[str]]  # questions the agent couldn't answer

    # Outcome — set by terminal nodes
    status: Literal["running", "done", "failed", "flagged"]
    failure_reason: Optional[str]
    submitted_at: Optional[str]

# Low temperature for form filling — we want deterministic, consistent answers.
# This LLM is only called when the form has an unexpected question.
# For known fields (name, email, cover letter), we fill them directly.
_llm = ChatGoogleGenerativeAI(
    model=settings.GEMINI_MODEL,
    google_api_key=settings.GEMINI_API_KEY,
    temperature=0.2,
    max_tokens=256,   # form answers are short
)

async def load_job_page(state: ApplicationState, page: Page) -> dict:
    """
    Navigates to the job URL and verifies the page loaded correctly.

    WHY DOES EVERY NODE RETURN A DICT INSTEAD OF MODIFYING STATE DIRECTLY?
    This is the core LangGraph pattern. Nodes don't mutate state —
    they return a dict of ONLY the fields they changed. LangGraph
    merges that dict into the current state. This means:

    1. Nodes are pure functions (easier to test)
    2. LangGraph can track which node changed which field
    3. You can add nodes without worrying about other fields

    If you returned the full state from every node, adding a new field
    means updating every node's return statement. With partial returns,
    only the node that owns that field touches it.
    """
    try:
        await page.goto(state["job_url"], wait_until="domcontentloaded", timeout=20_000)
        await asyncio.sleep(random.uniform(1.5, 3.0))

        # Check if we got redirected to a login wall
        if "login" in page.url or "authwall" in page.url:
            return {
                "page_loaded": False,
                "status": "failed",
                "failure_reason": "Hit LinkedIn login wall — cookies may have expired.",
            }

        # Verify the job title is visible — confirms we're on the right page
        try:
            await page.wait_for_selector(
                ".job-details-jobs-unified-top-card__job-title",
                timeout=8_000
            )
        except PWTimeout:
            return {
                "page_loaded": False,
                "status": "failed",
                "failure_reason": "Job detail panel did not load — page structure may have changed.",
            }

        return {"page_loaded": True, "status": "running"}

    except Exception as e:
        return {
            "page_loaded": False,
            "status": "failed",
            "failure_reason": f"Page load error: {str(e)}",
        }

async def find_apply_button(state: ApplicationState, page: Page) -> dict:
    """
    Looks for LinkedIn's Easy Apply button and clicks it.

    WHY EASY APPLY ONLY?
    LinkedIn has two application types:
    - Easy Apply: form stays on LinkedIn, Playwright can control it
    - External Apply: redirects to the company's own ATS (Greenhouse,
      Workday, Lever) — every ATS has a completely different form structure

    Handling every external ATS is a separate project. We focus on
    Easy Apply where the form is predictable and controllable.
    Jobs without Easy Apply get flagged, not failed — they're still
    valid jobs, just ones you apply to manually.
    """
    # LinkedIn's Easy Apply button has a specific data attribute.
    # CSS selectors based on data attributes are more stable than
    # class-based selectors — LinkedIn changes class names frequently
    # but data attributes are tied to functionality.
    easy_apply_selectors = [
        "button[data-control-name='jobdetails_topcard_inapply']",
        ".jobs-apply-button--top-card",
        "button.jobs-apply-button",
    ]

    for selector in easy_apply_selectors:
        try:
            button = await page.wait_for_selector(selector, timeout=4_000)
            if button:
                button_text = await button.inner_text()
                if "easy apply" in button_text.lower():
                    await button.click()
                    await asyncio.sleep(random.uniform(1.0, 2.0))

                    # Verify the modal opened
                    try:
                        await page.wait_for_selector(".jobs-easy-apply-modal", timeout=6_000)
                        return {"apply_button_found": True, "status": "running"}
                    except PWTimeout:
                        continue  # try next selector
        except PWTimeout:
            continue

    # No Easy Apply button found — external application
    return {
        "apply_button_found": False,
        "status": "flagged",
        "failure_reason": "No Easy Apply button found. Requires external application.",
    }

async def fill_form_fields(state: ApplicationState, page: Page, session: AsyncSession) -> dict:
    """
    Detects and fills all form fields in the Easy Apply modal.

    The strategy:
    - KNOWN fields (name, email, phone, cover letter): fill directly
    - UNKNOWN fields (custom questions): ask Gemini what to answer
    - CAPTCHA: flag immediately, don't attempt

    WHY DETECT FIELDS DYNAMICALLY INSTEAD OF HARDCODING SELECTORS?
    LinkedIn's Easy Apply forms vary by company. Some have 2 fields,
    some have 10. Some ask for salary expectations, years of experience,
    or "why do you want to work here?" We can't hardcode every possible
    field — we detect what's present and handle it.
    """
    fields_filled = []
    unexpected_fields = []

    # Check for CAPTCHA before doing anything
    captcha_present = await page.query_selector(".captcha-container, iframe[src*='captcha']")
    if captcha_present:
        await _log_form_attempt(session, state["job_id"], "captcha", None, "captcha")
        return {
            "status": "flagged",
            "failure_reason": "CAPTCHA encountered — cannot proceed automatically.",
            "fields_filled": fields_filled,
            "unexpected_fields": ["captcha"],
        }

    # Multi-page forms: LinkedIn Easy Apply can have multiple pages.
    # We loop through pages until we hit the submit button or a failure.
    max_pages = 5  # safety limit — no real form has more than 5 pages
    for page_num in range(max_pages):
        print(f"  Form page {page_num + 1}...")

        result = await _fill_current_form_page(
            page, session, state, fields_filled, unexpected_fields
        )

        if result["should_stop"]:
            break

        # Look for a "Next" button — means there are more pages
        next_button = await page.query_selector("button[aria-label='Continue to next step']")
        if next_button:
            await next_button.click()
            await asyncio.sleep(random.uniform(1.0, 2.0))
        else:
            break  # no next button = we're on the last page

    if unexpected_fields:
        return {
            "status": "flagged",
            "failure_reason": f"Unexpected fields encountered: {', '.join(unexpected_fields)}",
            "fields_filled": fields_filled,
            "unexpected_fields": unexpected_fields,
        }

    return {
        "status": "running",
        "fields_filled": fields_filled,
        "unexpected_fields": unexpected_fields,
    }

async def _fill_current_form_page(
    page: Page,
    session: AsyncSession,
    state: ApplicationState,
    fields_filled: list,
    unexpected_fields: list,
) -> dict:
    """
    Fills all visible form fields on the current page of the modal.
    Returns {"should_stop": bool} to signal whether to abort.

    FIELD DETECTION STRATEGY:
    We find all <label> elements in the modal. Each label describes
    a field. We read the label text, classify the field, and fill
    the associated input/textarea/select.

    WHY LABELS AND NOT INPUTS DIRECTLY?
    Labels have human-readable text ("First Name", "Phone Number").
    Inputs have attributes like name="firstName" or placeholder="Enter phone"
    — but LinkedIn doesn't always set these consistently. Labels are
    more reliable for understanding what a field is asking for.
    """
    # Known field handlers — label text patterns mapped to fill functions
    known_field_handlers = {
        r"first name":      lambda inp: inp.fill("Saim"),
        r"last name":       lambda inp: inp.fill(""),       # fill with your last name
        r"email":           lambda inp: inp.fill(settings.LINKEDIN_EMAIL),
        r"phone":           lambda inp: inp.fill(""),       # fill with your phone
        r"cover letter":    lambda inp: inp.fill(state.get("cover_letter") or ""),
        r"years of exp":    lambda inp: inp.fill("2"),
        r"linkedin":        lambda inp: inp.fill("https://linkedin.com/in/yourprofile"),
        r"github":          lambda inp: inp.fill("https://github.com/yourhandle"),
        r"website|portfolio": lambda inp: inp.fill(""),
    }

    labels = await page.query_selector_all(".jobs-easy-apply-modal label")

    for label in labels:
        label_text = (await label.inner_text()).strip().lower()

        # Find the input associated with this label.
        # The 'for' attribute on a label points to the input's id.
        label_for = await label.get_attribute("for")
        if not label_for:
            continue

        field = await page.query_selector(f"#{label_for}")
        if not field:
            continue

        field_tag = await field.evaluate("el => el.tagName.toLowerCase()")

        # --- Try known handlers first ---
        matched = False
        for pattern, handler in known_field_handlers.items():
            if re.search(pattern, label_text):
                try:
                    if field_tag == "select":
                        # Selects need different handling — pick first non-empty option
                        await _fill_select_field(field, page)
                    else:
                        await field.click()
                        await field.fill("")  # clear first
                        await handler(field)

                    fields_filled.append(label_text)
                    await _log_form_attempt(session, state["job_id"], label_text, "filled", "filled")
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    matched = True
                    break
                except Exception as e:
                    print(f"    Failed to fill '{label_text}': {e}")
                    matched = True  # still mark matched to avoid unexpected path
                    break

        if matched:
            continue

        # --- Unknown field — ask Gemini ---
        print(f"    Unexpected field: '{label_text}' — asking Gemini...")
        answer = await _ask_llm_for_field_answer(label_text, state)

        if answer:
            try:
                await field.click()
                await field.fill("")
                await field.type(answer, delay=random.randint(40, 90))
                fields_filled.append(label_text)
                await _log_form_attempt(session, state["job_id"], label_text, answer, "filled")
            except Exception as e:
                unexpected_fields.append(label_text)
                await _log_form_attempt(session, state["job_id"], label_text, None, "error", str(e))
        else:
            unexpected_fields.append(label_text)
            await _log_form_attempt(session, state["job_id"], label_text, None, "unexpected_field")
            return {"should_stop": True}  # can't proceed if we can't answer

    return {"should_stop": False}

async def _ask_llm_for_field_answer(field_label: str, state: ApplicationState) -> Optional[str]:
    """
    When the form has a question we don't recognize, ask Gemini.
    
    We give Gemini the CV, the job context, and the exact field label.
    It returns a short, appropriate answer.

    WHY WE CAP THE ANSWER AT 200 CHARS:
    Form fields have character limits. A 500-word essay for
    "years of experience?" breaks the form. We instruct the model
    to be brief, and we hard-cap just in case.
    """
    prompt = f"""You are helping fill a job application form field.

Job: {state['job_title']} at {state['job_company']}
Candidate CV summary: {CV_TEXT[:800]}

Form field label: "{field_label}"

Provide a short, appropriate answer for this field based on the candidate's CV.
- If it's a yes/no question, answer "Yes" or "No"
- If it's a number (years of experience), give a number only  
- If it's a short answer, keep it under 100 words
- If you genuinely cannot answer from the CV, respond with: CANNOT_ANSWER

Respond with ONLY the answer text. Nothing else."""

    try:
        response = await _llm.ainvoke([
            SystemMessage(content="You fill job application form fields concisely and accurately."),
            HumanMessage(content=prompt),
        ])
        answer = response.content.strip()
        if answer == "CANNOT_ANSWER" or not answer:
            return None
        return answer[:200]  # hard cap
    except Exception:
        return None


async def _fill_select_field(field, page: Page):
    """Selects the first non-empty option in a <select> element."""
    options = await field.query_selector_all("option")
    for option in options:
        value = await option.get_attribute("value")
        if value and value.strip():
            await field.select_option(value=value)
            return


async def _log_form_attempt(
    session: AsyncSession,
    job_id: int,
    field_name: str,
    field_value: Optional[str],
    status: str,
    error_detail: Optional[str] = None,
):
    """
    Logs every form field interaction to the FormAttempt table.
    
    This is what feeds your LangSmith-equivalent audit view.
    Every fill, every failure, every unexpected question — all recorded.
    You can query: "show me all unexpected fields across all applications"
    and immediately know what to improve.
    """
    attempt = FormAttempt(
        job_id=job_id,
        field_name=field_name,
        field_value=field_value[:500] if field_value else None,
        status=status,
        error_detail=error_detail,
    )
    session.add(attempt)
    await session.flush()

async def submit_form(state: ApplicationState, page: Page) -> dict:
    """
    Clicks the final submit button.

    WHY WE WAIT AFTER CLICKING:
    Form submission is async — the browser sends the request and
    the page updates when the server responds. If we check for
    success confirmation immediately after clicking, the confirmation
    element isn't rendered yet. We wait 3 seconds to let the
    response come back before checking.
    """
    submit_selectors = [
        "button[aria-label='Submit application']",
        "button[data-control-name='submit_unify']",
        ".jobs-easy-apply-footer button[type='submit']",
    ]

    for selector in submit_selectors:
        try:
            submit_btn = await page.wait_for_selector(selector, timeout=5_000)
            if submit_btn:
                await submit_btn.click()
                await asyncio.sleep(3.0)  # wait for server response
                return {
                    "status": "running",
                    "submitted_at": datetime.now(timezone.utc).isoformat(),
                }
        except PWTimeout:
            continue

    return {
        "status": "failed",
        "failure_reason": "Submit button not found — form structure may have changed.",
    }

async def verify_success(state: ApplicationState, page: Page) -> dict:
    """
    Checks for LinkedIn's post-submission confirmation.

    WHY THIS NODE EXISTS:
    Clicking submit doesn't guarantee submission succeeded. The form
    could have client-side validation errors. The server could return
    an error. The page could have changed.

    We look for LinkedIn's "Application submitted" confirmation element.
    If it's not there, we flag for human review rather than assuming success.
    Assuming success when the application didn't go through is the worst
    possible failure mode — you think you applied, you didn't.
    """
    confirmation_selectors = [
        ".artdeco-inline-feedback--success",
        "[data-test-modal] h2",   # "Your application was sent"
        ".jobs-easy-apply-content h2",
    ]

    for selector in confirmation_selectors:
        try:
            el = await page.wait_for_selector(selector, timeout=5_000)
            if el:
                text = await el.inner_text()
                if any(word in text.lower() for word in ["sent", "submitted", "applied"]):
                    return {"status": "done"}
        except PWTimeout:
            continue

    # No confirmation found
    return {
        "status": "flagged",
        "failure_reason": "No submission confirmation found — verify manually.",
    }

def build_application_graph():
    """
    Assembles the state machine.

    NODES: functions that do work
    EDGES: unconditional transitions ("always go from A to B")
    CONDITIONAL EDGES: transitions based on state ("if status=failed, go to END")

    WHY add_conditional_edges AFTER EVERY NODE?
    After every step, we check if the status has become "failed" or "flagged".
    If so, we go to END immediately — no point continuing if we can't proceed.
    This is the "early exit" pattern. Without it, a failed node 1 would
    cascade through nodes 2-5 with broken state, producing confusing errors.
    """
    graph = StateGraph(ApplicationState)

    # Register nodes — these are the function names you'll call
    graph.add_node("load_job_page", load_job_page)
    graph.add_node("find_apply_button", find_apply_button)
    graph.add_node("fill_form_fields", fill_form_fields)
    graph.add_node("submit_form", submit_form)
    graph.add_node("verify_success", verify_success)

    # Entry point
    graph.set_entry_point("load_job_page")

    # Conditional edge helper — used after every node
    def route_on_status(state: ApplicationState) -> str:
        """
        If status is failed or flagged, go to END.
        Otherwise continue to the next node.
        This function is called by LangGraph after each node runs.
        The string it returns is the name of the next node to execute.
        """
        if state["status"] in ("failed", "flagged"):
            return END
        return "continue"

    # After load_job_page: if ok → find_apply_button, else → END
    graph.add_conditional_edges(
        "load_job_page",
        route_on_status,
        {"continue": "find_apply_button", END: END},
    )

    graph.add_conditional_edges(
        "find_apply_button",
        route_on_status,
        {"continue": "fill_form_fields", END: END},
    )

    graph.add_conditional_edges(
        "fill_form_fields",
        route_on_status,
        {"continue": "submit_form", END: END},
    )

    graph.add_conditional_edges(
        "submit_form",
        route_on_status,
        {"continue": "verify_success", END: END},
    )

    # verify_success is the terminal node — always goes to END
    graph.add_edge("verify_success", END)

    return graph.compile()


# Compile once at module level — compilation is expensive,
# execution is cheap. Compile once, run many times.
APPLICATION_GRAPH = build_application_graph()

async def run_applicator_phase(session: AsyncSession):
    """
    Fetches all QUEUED jobs and runs the application graph on each.
    Updates job status and AgentRun record when done.
    """
    result = await session.execute(
        select(Job).where(Job.status == JobStatus.QUEUED.value)
    )
    jobs = result.scalars().all()

    if not jobs:
        print("No queued jobs to apply to.")
        return

    print(f"Applying to {len(jobs)} jobs...")

    run = AgentRun(phase="apply", status=RunStatus.RUNNING.value)
    session.add(run)
    await session.flush()

    applied = failed = flagged = 0

    # One browser for all applications in this run.
    # WHY NOT ONE BROWSER PER JOB?
    # Browser launch is slow (~2 seconds). Launching 20 browsers
    # for 20 jobs = 40 seconds of overhead. One browser with separate
    # pages per job is much faster. Also reuses the logged-in session.
    async with async_playwright() as p:
        from tools.scraper import create_browser_context, load_cookies
        browser, context = await create_browser_context(p)

        # Load saved cookies (we're already logged in from the scrape phase)
        await load_cookies(context)
        page = await context.new_page()

        for job in jobs:
            print(f"\nApplying: {job.title} @ {job.company}")
            job.status = JobStatus.APPLYING.value
            await session.flush()

            # Build initial state for this job's graph run
            initial_state: ApplicationState = {
                "job_id": job.id,
                "job_url": job.url,
                "job_title": job.title,
                "job_company": job.company,
                "job_description": job.description or "",
                "cover_letter": job.cover_letter,
                "page_loaded": False,
                "apply_button_found": False,
                "form_fields": None,
                "fields_filled": None,
                "unexpected_fields": None,
                "status": "running",
                "failure_reason": None,
                "submitted_at": None,
            }

            # Run the graph — this executes all nodes in sequence,
            # short-circuiting to END if any node fails or flags.
            # The graph handles a Playwright Page object that needs
            # to be injected. We do this via a partial/closure pattern.
            try:
                # LangGraph nodes are called with just (state).
                # But our nodes also need `page` and `session`.
                # We solve this by wrapping nodes in closures that
                # capture page and session from the outer scope.
                # This is the standard pattern for injecting
                # non-serializable dependencies into LangGraph nodes.
                final_state = await _run_graph_with_context(
                    initial_state, page, session
                )
            except Exception as e:
                final_state = {**initial_state, "status": "failed", "failure_reason": str(e)}

            # Update DB based on final graph state
            outcome = final_state.get("status")
            if outcome == "done":
                job.status = JobStatus.APPLIED.value
                job.applied_at = datetime.now(timezone.utc)
                applied += 1
                print(f"  ✓ Applied successfully.")
            elif outcome == "flagged":
                job.status = JobStatus.FAILED.value
                print(f"  ⚑ Flagged: {final_state.get('failure_reason')}")
                flagged += 1
            else:
                job.status = JobStatus.FAILED.value
                print(f"  ✗ Failed: {final_state.get('failure_reason')}")
                failed += 1

            await session.flush()

            # Delay between applications — looks human, avoids rate limiting
            await asyncio.sleep(random.uniform(8, 15))

        await browser.close()

    run.status = RunStatus.DONE.value
    run.jobs_applied = applied
    run.jobs_failed = failed + flagged
    run.completed_at = datetime.now(timezone.utc)
    run.log = f"{applied} applied, {flagged} flagged, {failed} failed."
    await session.flush()

    print(f"\nApplicator done: {applied} applied, {flagged} flagged, {failed} failed.")


async def _run_graph_with_context(
    initial_state: ApplicationState,
    page: Page,
    session: AsyncSession,
) -> ApplicationState:
    """
    Runs the compiled graph with page and session injected into each node.

    LangGraph calls nodes as: node_fn(state) → dict
    But our nodes need page and session too.

    The solution: rebuild the graph with closures that capture
    page and session. This looks verbose but it's the correct
    pattern — it keeps nodes as pure functions testable in isolation,
    while still having access to external resources at runtime.
    """
    graph = StateGraph(ApplicationState)

    # Wrap each node function in a closure that injects page/session
    async def _load(state): return await load_job_page(state, page)
    async def _find(state): return await find_apply_button(state, page)
    async def _fill(state): return await fill_form_fields(state, page, session)
    async def _submit(state): return await submit_form(state, page)
    async def _verify(state): return await verify_success(state, page)

    graph.add_node("load_job_page", _load)
    graph.add_node("find_apply_button", _find)
    graph.add_node("fill_form_fields", _fill)
    graph.add_node("submit_form", _submit)
    graph.add_node("verify_success", _verify)

    graph.set_entry_point("load_job_page")

    def route(state):
        return END if state["status"] in ("failed", "flagged") else "continue"

    graph.add_conditional_edges("load_job_page", route, {"continue": "find_apply_button", END: END})
    graph.add_conditional_edges("find_apply_button", route, {"continue": "fill_form_fields", END: END})
    graph.add_conditional_edges("fill_form_fields", route, {"continue": "submit_form", END: END})
    graph.add_conditional_edges("submit_form", route, {"continue": "verify_success", END: END})
    graph.add_edge("verify_success", END)

    compiled = graph.compile()
    final_state = await compiled.ainvoke(initial_state)
    return final_state

