from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.database import init_db
from app.api.routes import auth, workflows, sse
from app.api.websockets import handlers as ws_handlers


settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    yield
    # Shutdown


app = FastAPI(
    title=settings.APP_NAME,
    description="AI-powered task executor with human-in-the-loop approval",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(workflows.router, prefix="/api/workflows", tags=["Workflows"])
app.include_router(sse.router, prefix="/api/sse", tags=["Server-Sent Events"])
app.include_router(ws_handlers.router, tags=["WebSocket"])


@app.get("/")
async def root():
    return {"message": "Smart Task Executor API", "version": "1.0.0"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}
