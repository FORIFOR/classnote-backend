import sys
from unittest.mock import MagicMock

# Mock Google Cloud modules BEFORE importing app
def mock_pkg(name):
    m = MagicMock()
    sys.modules[name] = m
    return m

mock_google = mock_pkg("google")
mock_google_cloud = mock_pkg("google.cloud")
mock_firestore = mock_pkg("google.cloud.firestore")

# SYNC: Ensure google.cloud.firestore is accessible via both paths
mock_google_cloud.firestore = mock_firestore

def transparent_decorator(func):
    return func
mock_firestore.transactional = transparent_decorator

mock_pkg("google.cloud.firestore_v1")
mock_pkg("google.cloud.firestore_v1.base_query")
mock_pkg("google.cloud.storage")
mock_pkg("google.cloud.speech")
mock_pkg("google.cloud.speech_v2")
mock_pkg("google.cloud.speech_v2.types")
mock_pkg("google.cloud.aiplatform")
mock_pkg("google.cloud.logging")
mock_pkg("google.cloud.tasks_v2")
mock_pkg("google.protobuf")
mock_pkg("google.protobuf.timestamp_pb2")
mock_pkg("google.api_core")
mock_pkg("google.api_core.exceptions")
mock_pkg("google.api_core.client_options")
mock_pkg("google.api_core.gapic_v1")
mock_pkg("google.api_core.operation")
mock_pkg("google.auth")
mock_pkg("google.auth.transport")
mock_pkg("google.auth.transport.requests")
mock_pkg("google.oauth2")
mock_pkg("google.oauth2.service_account")
mock_pkg("firebase_admin")
mock_pkg("firebase_admin.auth")
mock_pkg("app_store_server_library")
mock_pkg("app_store_server_library.models")
mock_pkg("google.cloud.firestore_v1.client")
mock_pkg("google.cloud.firestore_v1.transaction")


import pytest
import os
from httpx import AsyncClient
# app.main import will now use mocked google deps
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
