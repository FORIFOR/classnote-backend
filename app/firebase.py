import os
import uuid
from google.cloud import firestore, storage
import firebase_admin
from firebase_admin import credentials

# Initialize Firebase Admin SDK (needed for Auth functions like create_custom_token)
if not firebase_admin._apps:
    try:
        # Try default credentials (works on Cloud Run with Service Account)
        firebase_admin.initialize_app()
    except Exception as e:
        print(f"Firebase Admin init with default credentials failed: {e}")
        # Try with explicit key file for local development
        key_path = os.path.join(os.path.dirname(__file__), "..", "classnote-api-key.json")
        if os.path.exists(key_path):
            cred = credentials.Certificate(key_path)
            firebase_admin.initialize_app(cred)
            print("Firebase Admin initialized with key file")
        else:
            print("WARNING: Firebase Admin not initialized - create_custom_token will fail")

class MockDocumentReference:
    def __init__(self, collection, id, data=None, exists=True):
        self.collection = collection
        self.id = id or "mock_id"
        self._data = data or {}
        self._exists = exists

    @property
    def exists(self):
        return self._exists

    def get(self):
        return self

    def to_dict(self):
        return self._data
    
    def set(self, data):
        self._data = data
        self._exists = True
        self.collection._docs[self.id] = data
        print(f"[MockDB] Set {self.id}: {data}")
    
    def update(self, data):
        self._data.update(data)
        self.collection._docs[self.id] = self._data
        print(f"[MockDB] Update {self.id}: {data}")

    def delete(self):
        self._exists = False
        self._data = {}
        if self.id in self.collection._docs:
            del self.collection._docs[self.id]
        print(f"[MockDB] Delete {self.id}")

class MockCollectionReference:
    def __init__(self):
        self._docs = {} # id -> data

    def document(self, doc_id):
        exists = doc_id in self._docs
        data = self._docs.get(doc_id)
        if data:
             return MockDocumentReference(collection=self, id=doc_id, data=data.copy(), exists=True)
        else:
             return MockDocumentReference(collection=self, id=doc_id, data={}, exists=False)
        
    def add(self, data):
        new_id = str(uuid.uuid4())
        self._docs[new_id] = data
        return None, MockDocumentReference(collection=self, id=new_id, data=data, exists=True)

    def order_by(self, *args, **kwargs):
        return self
    
    def limit(self, *args, **kwargs):
        return self

    def stream(self):
        for doc_id, data in self._docs.items():
            yield MockDocumentReference(collection=self, id=doc_id, data=data, exists=True)
            
    def where(self, field, op, value):
        # Very simple mock filter (only supports == for now)
        filtered = {}
        for doc_id, data in self._docs.items():
            if op == "==" and data.get(field) == value:
                filtered[doc_id] = data
        
        new_col = MockCollectionReference()
        new_col._docs = filtered
        return new_col

class MockFirestoreClient:
    def __init__(self):
        self._collections = {}

    def collection(self, name):
        if name not in self._collections:
            self._collections[name] = MockCollectionReference()
        return self._collections[name]
    
    def batch(self):
        return MockBatch(self)

class MockBatch:
    def __init__(self, client):
        self.client = client
        
    def delete(self, ref):
        ref.delete()
        
    def commit(self):
        pass

class MockBlob:
    def __init__(self, name):
        self.name = name
    
    def generate_signed_url(self, **kwargs):
        return f"https://storage.googleapis.com/mock-bucket/{self.name}?signed=true"
    
    def upload_from_filename(self, filename, **kwargs):
        print(f"[MockStorage] Uploaded {filename} to {self.name}")

class MockBucket:
    def blob(self, name):
        return MockBlob(name)

class MockStorageClient:
    def bucket(self, name):
        return MockBucket()

# ---------- Initialization ---------- #

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
USE_MOCK_DB = os.environ.get("USE_MOCK_DB", "0") == "1"

if USE_MOCK_DB:
    print("!!! USING MOCK DB !!!")
    db = MockFirestoreClient()
    storage_client = MockStorageClient()
    AUDIO_BUCKET_NAME = "mock-bucket"
    MEDIA_BUCKET_NAME = "mock-media-bucket"
else:
    if not PROJECT_ID:
        PROJECT_ID = os.environ.get("GCP_PROJECT")

    if not PROJECT_ID:
         print("WARNING: GOOGLE_CLOUD_PROJECT not set. Some features may not work.")

    AUDIO_BUCKET_URI = os.environ.get("AUDIO_BUCKET")
    if not AUDIO_BUCKET_URI:
         AUDIO_BUCKET_URI = os.environ.get("AUDIO_BUCKET_NAME", "classnote-x-audio")

    if AUDIO_BUCKET_URI.startswith("gs://"):
        AUDIO_BUCKET_NAME = AUDIO_BUCKET_URI.replace("gs://", "").rstrip("/")
    else:
        AUDIO_BUCKET_NAME = AUDIO_BUCKET_URI
        
    MEDIA_BUCKET_URI = os.environ.get("MEDIA_BUCKET_NAME", "classnote-x-media")
    if MEDIA_BUCKET_URI.startswith("gs://"):
        MEDIA_BUCKET_NAME = MEDIA_BUCKET_URI.replace("gs://", "").rstrip("/")
    else:
        MEDIA_BUCKET_NAME = MEDIA_BUCKET_URI

    # Client Initialization
    try:
        if PROJECT_ID:
            db = firestore.Client(project=PROJECT_ID)
            storage_client = storage.Client(project=PROJECT_ID)
        else:
            db = firestore.Client()
            storage_client = storage.Client()
    except Exception as e:
        print(f"Failed to initialize Google Cloud Clients: {e}")
        # Fallback to mock if init fails (optional, but useful for local dev without creds)
        print("Falling back to Mock DB due to initialization failure.")
        db = MockFirestoreClient()
        storage_client = MockStorageClient()
