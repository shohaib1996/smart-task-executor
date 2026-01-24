import ssl
from sqlmodel import SQLModel
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.core.config import get_settings

settings = get_settings()

# Create SSL context for Neon PostgreSQL
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

# Remove sslmode from URL and pass ssl via connect_args
database_url = settings.DATABASE_URL.split("?")[0]

engine = create_async_engine(
    database_url,
    echo=settings.DEBUG,
    connect_args={"ssl": ssl_context},
    # Pool settings for Neon serverless PostgreSQL
    pool_pre_ping=True,  # Check connection health before using
    pool_recycle=300,  # Recycle connections after 5 minutes
    pool_size=5,
    max_overflow=10,
)

async_session = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
