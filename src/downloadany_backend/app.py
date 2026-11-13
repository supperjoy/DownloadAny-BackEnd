from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .database import Base, engine
from .analytics_routes import router as analytics_router
from .feedback_routes import router as feedback_router
from .media_routes import router as media_router
from .routes import router
from .update_routes import router as update_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    # For a small fresh deployment this creates the schema. Use migrations before
    # changing columns in an existing production database.
    Base.metadata.create_all(bind=engine)
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
