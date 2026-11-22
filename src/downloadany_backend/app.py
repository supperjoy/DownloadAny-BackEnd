import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .database import Base, engine
from .admin_stats_routes import router as admin_stats_router
from .analytics_routes import router as analytics_router
from .feedback_routes import router as feedback_router
from .media_routes import router as media_router
from .routes import router
from .schema_migrations import migrate_feedback_contact
from .update_routes import router as update_router

# uvicorn 默认只配置 uvicorn.* 的日志器；root 无 handler 时，应用包的 INFO
# 会被 lastResort(WARNING) 吞掉。给 downloadany_backend 命名空间挂一个自己的
# handler，保证代理池出口切换等业务日志在容器 stdout 里可见（compose logs）。
_package_logger = logging.getLogger("downloadany_backend")
if not _package_logger.handlers:
    _stream_handler = logging.StreamHandler()
    _stream_handler.setFormatter(logging.Formatter("%(levelname)s: %(name)s: %(message)s"))
    _package_logger.addHandler(_stream_handler)
    _package_logger.setLevel(logging.INFO)


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
app.include_router(admin_stats_router)
app.include_router(update_router)
app.include_router(feedback_router)


@app.get("/health")
def health():
    return {"status": "ok"}
