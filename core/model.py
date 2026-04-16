from datetime import datetime, timezone
from enum import Enum as PyEnum
from typing import Optional
from sqlalchemy import String, Text, Integer, Float, DateTime, Enum as SAEnum, JSON
from sqlalchemy.orm import Mapped, mapped_column
from core.database import Base

def utcnow():
    return datetime.now(timezone.utc)

# --- Status Enums ---
# We define these as Python string enums, not PostgreSQL native enums.
# Why string? SQLite doesn't have a native enum type anyway — it stores
# everything as text. Using Python enums just gives us IDE autocomplete
# and prevents typos like status="aplied" silently passing through.

class JobStatus(str, PyEnum):
    NEW       = "new"        # just scraped, not yet scored
    SCORED    = "scored"     # LangChain agent has scored it
    QUEUED    = "queued"     # score >= threshold, ready for LangGraph
    APPLYING  = "applying"   # LangGraph is currently working on it
    APPLIED   = "applied"    # successfully submitted
    FAILED    = "failed"     # submission failed (captcha, error, etc.)
    SKIPPED   = "skipped"    # score < threshold, not applying

class RunStatus(str, PyEnum):
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"

class Job(Base):
    """
    Represents one scraped job listing.
    
    Everything the scraper finds goes here first (status=NEW).
    The analyst agent then enriches it with score + gap data.
    The applicator agent updates it again with submission result.
    
    One row = one job posting = one full lifecycle.
    """
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- Scraped data ---
    url: Mapped[str] = mapped_column(String(2048), unique=True)
    # unique=True is doing real work here: when we re-scrape LinkedIn tomorrow,
    # the same job will appear again. INSERT will fail on this constraint,
    # we catch the IntegrityError, and skip it. Free deduplication.
    
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(300))
    location: Mapped[Optional[str]] = mapped_column(String(300))
    description: Mapped[Optional[str]] = mapped_column(Text)
    # Text vs String: String has a declared max length (better for indexed
    # columns like url, title). Text is unbounded — right for job descriptions
    # which can be 3000+ words.

    # --- Analyst output ---
    score: Mapped[Optional[float]] = mapped_column(Float)
    
    missing_skills: Mapped[Optional[list]] = mapped_column(JSON)
    # Stored as JSON because it's a variable-length list: 
    # ["Docker", "Go", "Kubernetes"] — no need for a separate skills table
    # at this scale. JSON column lets you store and retrieve Python lists directly.
    
    courses: Mapped[Optional[list]] = mapped_column(JSON)
    # Same pattern: [{"skill": "Docker", "course": "Docker Mastery - Udemy"}]
    
    cover_letter: Mapped[Optional[str]] = mapped_column(Text)

    # --- Status ---
    status: Mapped[str] = mapped_column(
        String(50),
        default=JobStatus.NEW.value,
        nullable=False,
    )
    # We use String(50) instead of SAEnum here because SQLite's enum
    # support through SQLAlchemy can be quirky. String + Python enum
    # validation in code is simpler and equally safe for this scale.

    # --- Timestamps ---
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    scored_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    def __repr__(self):
        return f"<Job {self.id} | {self.title} @ {self.company} | score={self.score} | {self.status}>"

class AgentRun(Base):
    """
    One row per pipeline execution.
    
    WHY TRACK RUNS SEPARATELY?
    Without this table you have no answer to:
      "Did the agent run at 6am today?"
      "How many jobs did last night's run find?"
      "Did the applicator crash, or did it just find nothing to apply to?"
    
    This is your audit log. LangSmith traces the LLM calls.
    AgentRun tracks the business-level outcomes.
    """
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    
    # Which part of the pipeline ran?
    # "scrape", "analyse", "apply" — one row per phase per execution
    phase: Mapped[str] = mapped_column(String(50))
    
    status: Mapped[str] = mapped_column(String(50), default=RunStatus.RUNNING.value)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Counters — filled in as the phase completes
    jobs_found: Mapped[int] = mapped_column(Integer, default=0)
    jobs_scored: Mapped[int] = mapped_column(Integer, default=0)
    jobs_applied: Mapped[int] = mapped_column(Integer, default=0)
    jobs_failed: Mapped[int] = mapped_column(Integer, default=0)

    # Freeform log for this run — the agent appends lines here.
    # Example: "Scraped 12 jobs. 3 duplicates skipped. 9 new saved."
    log: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)

    def duration(self) -> Optional[float]:
        if self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

class FormAttempt(Base):
    """
    One row per form field interaction during a LangGraph application run.
    
    WHY THIS TABLE EXISTS:
    This is what feeds LangSmith's audit data into your own DB.
    When LinkedIn asks an unexpected question ("What's your expected salary?"),
    LangGraph logs it here as status="unexpected_field".
    
    You can then query: SELECT * FROM form_attempts WHERE status='unexpected_field'
    and see exactly which jobs are asking questions your agent can't handle.
    This is the "where the jobs were not properly filled" requirement you mentioned.
    
    LangSmith shows you the LLM trace. This table shows you the form-level outcome.
    Both together give you the full picture.
    """
    __tablename__ = "form_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer)  # references jobs.id
    
    field_name: Mapped[str] = mapped_column(String(200))
    field_value: Mapped[Optional[str]] = mapped_column(Text)  # what we tried to fill
    
    status: Mapped[str] = mapped_column(String(50))
    # "filled"             — successfully filled
    # "unexpected_field"   — question the agent didn't know how to answer
    # "captcha"            — hit a CAPTCHA wall
    # "error"              — Playwright threw an exception

    error_detail: Mapped[Optional[str]] = mapped_column(Text)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)