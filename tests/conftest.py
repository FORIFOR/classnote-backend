
import pytest
import os
from httpx import AsyncClient
from app.main import app

# Set Test Environment
os.environ["FIRESTORE_EMULATOR_HOST"] = "localhost:8080"
os.environ["GCP_PROJECT"] = "test-project"
os.environ["USE_MOCK_DB"] = "1" # Use internal mock for unit level

@pytest.fixture
def anyio_backend():
    return "asyncio"

@pytest.fixture
async def client():
    async with AsyncClient(app=app, base_url="http://test") as c:
        yield c

@pytest.fixture
def auth_headers():
    # Mock Token structure if needed, or use a dependency override
    return {"Authorization": "Bearer mock-token-uid-123"}
