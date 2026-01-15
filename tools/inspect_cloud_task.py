
import os
import json
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2
import datetime

# Env Setup (Mocking Config)
PROJECT_ID = "classnote-x-dev"
LOCATION = "asia-northeast1"
QUEUE_NAME = "summarize-queue"
CLOUD_RUN_URL = "https://classnote-api-900324644592.asia-northeast1.run.app"

def enqueue_test_task():
    # Load creds from default path or env
    # Assuming GCLOUD auth is active or key file present
    key_path = "classnote-api-key.json"
    if os.path.exists(key_path):
         os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key_path

    try:
        client = tasks_v2.CloudTasksClient()
    except Exception as e:
        print(f"Client Init Failed: {e}")
        return

    parent = client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)
    url = f"{CLOUD_RUN_URL}/internal/tasks/summarize"
    payload = {"sessionId": "manual-inspection-test", "jobId": "test-job-123"}
    
    print(f"Target URL: {url}")
    print(f"Queue: {parent}")

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
        }
    }

    try:
        response = client.create_task(parent=parent, task=task)
        print(f"Created task: {response.name}")
        # View task details?
        print(f"Task View: {response.http_request.url}")
    except Exception as e:
        print(f"Create Failed: {e}")

if __name__ == "__main__":
    enqueue_test_task()
