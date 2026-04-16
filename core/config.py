from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    # --- Gemini ---
    GEMINI_API_KEY: str
    GEMINI_MODEL: str = "gemini-1.5-flash"

    # --- LangSmith (observability) ---
    # LangSmith is LangChain's tracing platform. When these are set,
    # every agent call, tool invocation, and LLM response is automatically
    # recorded. You don't write any logging code — it just works.
    # Get these free at: https://smith.langchain.com
    LANGCHAIN_API_KEY: str
    LANGCHAIN_TRACING_V2: str = "true"     # enables auto-tracing
    LANGCHAIN_PROJECT: str = "job-agent"   # groups traces in the UI

    # --- LinkedIn ---
    # We need your credentials so Playwright can log in and access
    # full job descriptions (gated behind login on LinkedIn).
    LINKEDIN_EMAIL: str
    LINKEDIN_PASSWORD: str

    # --- Scoring ---
    # Jobs scoring below this are saved to DB but never applied to.
    # The LangGraph applicator checks this before submitting.
    MIN_SCORE: int = Field(default=70, ge=0, le=100)

    # --- SQLite ---
    # A file path, not a server URL. That's the entire setup.
    # SQLite creates the file if it doesn't exist.
    DATABASE_URL: str = "sqlite+aiosqlite:///./jobs.db"
    #              ↑ async driver  ↑ relative path — file appears in project root

    class Config:
        env_file = ".env"

settings = Settings()