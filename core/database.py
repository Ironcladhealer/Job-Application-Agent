from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from core.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    # SQLite doesn't support multiple simultaneous writers at all.
    # pool_size doesn't apply — SQLite has its own simpler connection model.
    # check_same_thread=False is required because SQLAlchemy may use
    # the connection from a different thread than the one that created it.
    connect_args={"check_same_thread": False},
    echo=False,  # set True to see raw SQL in terminal during dev
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    # expire_on_commit=False means after session.commit(), your ORM objects
    # still have their data. With True (default), accessing job.title after
    # a commit would trigger a new DB query — confusing in async code.
)

class Base(DeclarativeBase):
    pass

async def init_db():
    """
    Called once at app startup. Creates all tables that don't exist yet.
    
    This is NOT a migration system. It's "create if missing."
    If you add a column to a model, you need to either:
      - Delete jobs.db and let it recreate (fine during dev)
      - Or use Alembic for real migrations (Phase 4+)
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_session():
    """
    FastAPI dependency. Use it like this in any route:
    
        async def my_route(db: AsyncSession = Depends(get_session)):
            result = await db.execute(select(Job))
    
    The `yield` makes this a context manager — the session is always
    closed after the route finishes, even if it throws an exception.
    Forgetting to close sessions is the #1 cause of SQLite lock errors.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise