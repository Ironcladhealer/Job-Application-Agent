# api/routes.py

from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel

from core.database import get_session
from core.models import Job, JobStatus, AgentRun, RunStatus, FormAttempt
from agents.analyst import run_analyst_phase
from agents.applicator import run_applicator_phase
from tools.scraper import run_scrape_phase

router = APIRouter()

# WHY PYDANTIC RESPONSE MODELS?
# FastAPI serializes SQLAlchemy ORM objects to JSON automatically,
# but only if you tell it the shape via a Pydantic model.
# More importantly: response models are your API contract.
# They prevent you from accidentally leaking internal fields
# (like raw passwords, internal flags) to the frontend.
# They also give you automatic OpenAPI docs at /docs.

class JobResponse(BaseModel):
    id: int
    title: str
    company: str
    location: Optional[str]
    score: Optional[float]
    status: str
    missing_skills: Optional[list]
    courses: Optional[list]
    scraped_at: datetime
    scored_at: Optional[datetime]
    applied_at: Optional[datetime]

    # orm_mode (now called from_attributes in Pydantic v2) tells Pydantic
    # to read data from ORM object attributes, not just dicts.
    # Without this, JobResponse(job) fails because job is a SQLAlchemy
    # object, not a dict.
    model_config = {"from_attributes": True}


class JobDetailResponse(JobResponse):
    description: Optional[str]
    cover_letter: Optional[str]
    url: str


class RunResponse(BaseModel):
    id: int
    phase: str
    status: str
    jobs_found: int
    jobs_scored: int
    jobs_applied: int
    jobs_failed: int
    log: Optional[str]
    error: Optional[str]
    started_at: datetime
    completed_at: Optional[datetime]

    model_config = {"from_attributes": True}


class FormAttemptResponse(BaseModel):
    id: int
    job_id: int
    field_name: str
    field_value: Optional[str]
    status: str
    error_detail: Optional[str]
    attempted_at: datetime

    model_config = {"from_attributes": True}


class StatsResponse(BaseModel):
    total_jobs: int
    scored: int
    queued: int
    applied: int
    failed: int
    skipped: int
    avg_score: Optional[float]
    flagged_fields: int   # unexpected form fields across all applications

# WHY BackgroundTasks INSTEAD OF JUST CALLING THE FUNCTION?
# Scraping + analysing takes minutes. If you call run_scrape_phase()
# directly inside the route handler, the HTTP request blocks until
# it completes. The frontend would hang waiting for a response.
#
# BackgroundTasks tells FastAPI: "return 200 immediately, then run
# this function in the background." The frontend gets an instant
# response and polls for status via GET /runs.
#
# For truly long tasks (30+ minutes), you'd use Celery or ARQ.
# BackgroundTasks is fine for tasks under ~10 minutes.

@router.post("/run/scrape", status_code=202)
async def trigger_scrape(
    background_tasks: BackgroundTasks,
    keyword: str = "backend engineer",
    db: AsyncSession = Depends(get_session),
):
    """
    Triggers the scrape phase in the background.
    Returns 202 Accepted immediately — not 200 OK.

    202 vs 200: 200 means "done." 202 means "accepted, processing."
    This distinction matters — the client knows not to expect
    results in this response.
    """
    background_tasks.add_task(run_scrape_phase, db, keyword)
    return {"message": f"Scrape started for '{keyword}'", "status": "accepted"}


@router.post("/run/analyse", status_code=202)
async def trigger_analyse(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
):
    background_tasks.add_task(run_analyst_phase, db)
    return {"message": "Analysis started", "status": "accepted"}


@router.post("/run/apply", status_code=202)
async def trigger_apply(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
):
    background_tasks.add_task(run_applicator_phase, db)
    return {"message": "Application phase started", "status": "accepted"}


@router.post("/run/pipeline", status_code=202)
async def trigger_full_pipeline(
    background_tasks: BackgroundTasks,
    keyword: str = "backend engineer",
    db: AsyncSession = Depends(get_session),
):
    """
    Runs all three phases in sequence as one background task.
    This is what you'd hook up to a cron job.
    """
    async def full_pipeline():
        await run_scrape_phase(db, keyword)
        await run_analyst_phase(db)
        await run_applicator_phase(db)

    background_tasks.add_task(full_pipeline)
    return {"message": "Full pipeline started", "status": "accepted"}

@router.get("/jobs", response_model=list[JobResponse])
async def get_jobs(
    status: Optional[str] = None,
    min_score: Optional[float] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_session),
):
    """
    Returns jobs with optional filtering by status and minimum score.

    WHY QUERY PARAMS INSTEAD OF A POST WITH A BODY?
    GET requests are for reading. Filters are query parameters, not bodies.
    This is REST convention — it also means the URL is shareable:
    /jobs?status=queued&min_score=75 is a bookmarkable filtered view.
    """
    query = select(Job).order_by(Job.scraped_at.desc()).limit(limit)

    if status:
        query = query.where(Job.status == status)
    if min_score is not None:
        query = query.where(Job.score >= min_score)

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
async def get_job(job_id: int, db: AsyncSession = Depends(get_session)):
    """
    Returns full details for one job including description and cover letter.
    We keep description/cover_letter out of the list endpoint — they're
    large text fields that would make the list response slow and heavy.
    """
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()

    if not job:
        # 404 with a clear message — not a generic "not found"
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return job


@router.get("/jobs/{job_id}/form-attempts", response_model=list[FormAttemptResponse])
async def get_form_attempts(job_id: int, db: AsyncSession = Depends(get_session)):
    """
    Returns all form field interactions for a job application.
    This is the "where did the agent struggle?" view — your LangSmith complement.
    """
    result = await db.execute(
        select(FormAttempt)
        .where(FormAttempt.job_id == job_id)
        .order_by(FormAttempt.attempted_at.asc())
    )
    return result.scalars().all()


@router.get("/runs", response_model=list[RunResponse])
async def get_runs(
    limit: int = 20,
    db: AsyncSession = Depends(get_session)
):
    """Returns the most recent agent run records."""
    result = await db.execute(
        select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit)
    )
    return result.scalars().all()


@router.get("/stats", response_model=StatsResponse)
async def get_stats(db: AsyncSession = Depends(get_session)):
    """
    Aggregated dashboard stats in one query.

    WHY ONE ENDPOINT INSTEAD OF COMPUTING THIS IN REACT?
    React would need to fetch all jobs then count statuses client-side.
    That's N records over the wire just to display 6 numbers.
    A single SQL query with COUNT + GROUP BY is O(1) network and DB time.
    Always aggregate on the server, not the client.
    """
    # Count jobs per status in one query
    status_counts = await db.execute(
        select(Job.status, func.count(Job.id))
        .group_by(Job.status)
    )
    counts = dict(status_counts.all())

    # Average score of scored jobs
    avg_result = await db.execute(
        select(func.avg(Job.score)).where(Job.score.isnot(None))
    )
    avg_score = avg_result.scalar()

    # Count flagged form fields
    flagged_result = await db.execute(
        select(func.count(FormAttempt.id))
        .where(FormAttempt.status == "unexpected_field")
    )
    flagged_fields = flagged_result.scalar() or 0

    return StatsResponse(
        total_jobs=sum(counts.values()),
        scored=counts.get(JobStatus.SCORED.value, 0),
        queued=counts.get(JobStatus.QUEUED.value, 0),
        applied=counts.get(JobStatus.APPLIED.value, 0),
        failed=counts.get(JobStatus.FAILED.value, 0),
        skipped=counts.get(JobStatus.SKIPPED.value, 0),
        avg_score=round(avg_score, 1) if avg_score else None,
        flagged_fields=flagged_fields,
    )
