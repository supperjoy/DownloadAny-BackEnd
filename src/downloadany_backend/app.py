from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .database import Base, engine
from .analytics_routes import router as analytics_router
from .feedback_routes import router as feedback_router
from .media_routes import router as media_router
from .routes import router
from .schema_migrations import migrate_feedback_contact
from .update_routes import router as update_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Create fresh tables first, then upgrade schemas from older deployments.
    Base.metadata.create_all(bind=engine)
    migrate_feedback_contact(engine)
    yield


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(media_router)
app.include_router(analytics_router)
app.include_router(update_router)
app.include_router(feedback_router)


@app.get("/health")
def health():
    return {"status": "ok"}
