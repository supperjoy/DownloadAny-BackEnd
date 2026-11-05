import os

os.environ["DATABASE_URL"] = "sqlite+pysqlite://"
os.environ["JWT_SECRET"] = "test-secret-that-is-at-least-32-characters-long"
os.environ["ENVIRONMENT"] = "test"

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from downloadany_backend.app import app
from downloadany_backend.database import Base, SessionLocal, engine


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session
