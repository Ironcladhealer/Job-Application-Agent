# agents/analyst.py

import json
import re
from datetime import datetime, timezone

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.models import Job, JobStatus, AgentRun, RunStatus
from agents.prompts import CV_TEXT, SCORING_PROMPT, GAP_ANALYSIS_PROMPT, COVER_LETTER_PROMPT
from tools.pdf_writer import generate_pdf_report

# TWO LLM INSTANCES, TWO TEMPERATURES.
# This is a deliberate design decision, not an oversight.
#
# Scoring needs CONSISTENCY — run it twice on the same job,
# get the same score. Low temperature (0.1) means the model
# picks the most probable token at each step. Deterministic.
#
# Cover letters need NATURALNESS — robotic, repetitive letters
# get ignored. Higher temperature (0.7) introduces variation
# so each letter feels individually written.
#
# Using one temperature for both is a common mistake.

_scorer_llm = ChatGoogleGenerativeAI(
    model=settings.GEMINI_MODEL,
    google_api_key=settings.GEMINI_API_KEY,
    temperature=0.1,
    max_tokens=512,    # scores are short — cap tokens to save cost
)

_writer_llm = ChatGoogleGenerativeAI(
    model=settings.GEMINI_MODEL,
    google_api_key=settings.GEMINI_API_KEY,
    temperature=0.7,
    max_tokens=1024,   # cover letters need more room
)

async def _call_llm(llm, system: str, user: str) -> str:
    """
    Wrapper around every Gemini call. Handles the two things
    that go wrong most often with LLM APIs:

    1. The model wraps JSON in markdown fences (```json ... ```)
       even when you explicitly say not to. We strip them.

    2. The API call itself can fail (rate limit, timeout, network).
       We let the exception propagate — the caller decides whether
       to retry, skip, or abort. Don't swallow errors silently.

    WHY SystemMessage + HumanMessage instead of one big string?
    The system message sets the model's persistent behavior for
    the whole call ("you are a precise evaluator, output only JSON").
    The human message is the actual task.
    This two-part structure reliably produces cleaner outputs than
    stuffing everything into one message.
    """
    response = await llm.ainvoke([
        SystemMessage(content=system),
        HumanMessage(content=user),
    ])

    text = response.content.strip()
    # Strip markdown fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()

async def score_job(job: Job) -> dict:
    """
    Calls Gemini to score a job against the CV.
    Returns the parsed score dict.

    WHY WE VALIDATE THE OUTPUT:
    The LLM is instructed to return JSON with specific fields.
    But it might return a score of 105, or omit a field, or return
    "skills_match": "high" instead of an integer. We validate every
    field before trusting it. An invalid score silently propagating
    into the DB is worse than a loud exception here.
    """
    prompt = SCORING_PROMPT.format(
        cv=CV_TEXT,
        title=job.title,
        company=job.company,
        location=job.location or "Not specified",
        description=(job.description or "")[:3000],
        # Truncate description to 3000 chars.
        # WHY? Gemini flash has a large context window, but:
        # 1. Most of a job description's signal is in the first 2000 chars
        # 2. Longer prompts = higher latency + cost
        # 3. The model's attention dilutes over very long inputs
        # 3000 chars captures requirements, responsibilities, and qualifications.
    )

    raw = await _call_llm(
        _scorer_llm,
        system="You are a precise job-fit evaluator. Output only valid JSON.",
        user=prompt,
    )

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError(f"Scorer returned invalid JSON for job {job.id}:\n{raw[:300]}")

    # Validate score is a number in range
    score = result.get("score")
    if not isinstance(score, (int, float)) or not (0 <= score <= 100):
        raise ValueError(f"Invalid score value '{score}' for job {job.id}")

    return result

async def analyse_gaps(job: Job) -> list[dict]:
    """
    Calls Gemini to identify missing skills and suggest courses.
    Returns a list of gap dicts.

    This is a separate LLM call from scoring — deliberately.
    WHY NOT COMBINE THEM INTO ONE PROMPT?

    Two reasons:
    1. Focus: a model asked to do two things at once does both worse.
       Scoring requires comparative judgment. Gap analysis requires
       knowledge of the skills landscape and available courses.
       Separate prompts = separate focused reasoning chains.

    2. Temperature: scoring needs 0.1, but gap analysis benefits
       from slightly more creative course suggestions. If combined,
       you're forced to pick one temperature for both.
    """
    prompt = GAP_ANALYSIS_PROMPT.format(
        cv=CV_TEXT,
        title=job.title,
        description=(job.description or "")[:3000],
    )

    raw = await _call_llm(
        _scorer_llm,   # still low temp — we want consistent gap identification
        system="You are a precise skill gap analyst. Output only valid JSON.",
        user=prompt,
    )

    try:
        result = json.loads(raw)
        gaps = result.get("missing_skills", [])
        if not isinstance(gaps, list):
            return []
        return gaps
    except json.JSONDecodeError:
        print(f"  Gap analysis returned invalid JSON for job {job.id}. Defaulting to [].")
        return []
        # WHY RETURN [] INSTEAD OF RAISING?
        # Gap analysis failure is non-critical. The job can still be
        # scored, saved, and applied to. We log the issue (via the
        # return value being empty) but don't abort the whole job.
        # Score failure IS critical — we raise there. Choose your
        # error severity deliberately.

async def draft_cover_letter(job: Job, score_data: dict, gaps: list[dict]) -> str:
    """
    Generates a tailored cover letter using the score and gap data.

    Notice we pass score_data and gaps INTO this function.
    The cover letter prompt uses the strengths from the score breakdown
    and the gaps list to know what to emphasize and what to address.
    This is "context chaining" — each LLM call's output feeds the next.
    """
    strengths = [
        k for k, v in {
            "skills": score_data.get("skills_match", 0),
            "experience": score_data.get("experience_match", 0),
            "role fit": score_data.get("role_fit", 0),
        }.items() if v >= 15   # only mention dimensions that scored well
    ]

    gap_names = [g["skill"] for g in gaps[:2]]  # top 2 gaps only — don't dwell

    prompt = COVER_LETTER_PROMPT.format(
        cv=CV_TEXT,
        title=job.title,
        company=job.company,
        description=(job.description or "")[:2000],
        score=score_data.get("score", 0),
        strengths=", ".join(strengths) if strengths else "general fit",
        gaps=", ".join(gap_names) if gap_names else "none significant",
    )

    letter = await _call_llm(
        _writer_llm,   # higher temp — natural writing
        system="You are a professional cover letter writer. Output only the letter text.",
        user=prompt,
    )

    return letter

async def run_analyst_phase(session: AsyncSession):
    """
    Fetches all NEW jobs from DB and runs the analyst sequence on each.
    Logs the run to AgentRun for the dashboard.
    """
    # Fetch only NEW jobs — don't re-process already scored ones
    result = await session.execute(
        select(Job).where(Job.status == JobStatus.NEW.value)
    )
    jobs = result.scalars().all()

    if not jobs:
        print("No new jobs to analyse.")
        return

    print(f"Analysing {len(jobs)} new jobs...")

    run = AgentRun(phase="analyse", status=RunStatus.RUNNING.value)
    session.add(run)
    await session.flush()

    scored = 0
    queued = 0
    failed = 0

    for job in jobs:
        try:
            success = await process_job(job, session)
            if success:
                scored += 1
                if job.status == JobStatus.QUEUED.value:
                    queued += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  Unexpected error on job {job.id}: {e}")
            failed += 1

    # --- Generate PDF after all jobs are processed ---
    # WHY AFTER, NOT DURING?
    # The PDF is a report across ALL jobs — the heatmap needs
    # data from every job. If you generate it per-job, you get
    # 20 single-job PDFs instead of one cross-job analysis.
    queued_jobs = [j for j in jobs if j.status == JobStatus.QUEUED.value]
    if queued_jobs:
        try:
            pdf_path = await generate_pdf_report(queued_jobs)
            print(f"\nPDF report saved: {pdf_path}")
        except Exception as e:
            print(f"PDF generation failed: {e}")

    run.status = RunStatus.DONE.value
    run.jobs_scored = scored
    run.jobs_applied = queued
    run.jobs_failed = failed
    run.completed_at = datetime.now(timezone.utc)
    run.log = f"{scored} scored, {queued} queued for application, {failed} failed."
    await session.flush()

    print(f"\nAnalysis complete: {scored} scored, {queued} queued, {failed} failed.")

