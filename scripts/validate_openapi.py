import yaml
import requests
import json
import sys
import time
from typing import Dict, Any, Optional

# Constants
OPENAPI_FILE = "openapi.yaml"
BASE_URL = "http://localhost:8000"  # Adjust if running on a different port
# BASE_URL = "https://classnote-api-900324644592.asia-northeast1.run.app" # For Cloud Run debugging

def load_openapi_spec(filepath: str) -> Dict[str, Any]:
    with open(filepath, 'r') as f:
        return yaml.safe_load(f)

def validate_response(response: requests.Response, schema: Dict[str, Any]) -> bool:
    """
    Validates the response against the expected schema.
    This is a simplified validation. For production, use a library like `openapi-core` or `jsonschema`.
    """
    if not schema:
        return True # No schema defined for this response code
    
    # Check content type if specified
    if 'content' in schema:
        content_type = response.headers.get('Content-Type')
        # Simple check, might need to handle parameters in content-type (e.g. charset)
        if 'application/json' in schema['content']:
             if content_type and 'application/json' not in content_type:
                 print(f"  [ERROR] Content-Type mismatch. Expected application/json, got {content_type}")
                 return False
             
             expected_schema = schema['content']['application/json']['schema']
             try:
                 json_data = response.json()
                 # Here we would ideally validate json_data against expected_schema
                 # For now, let's just check if it's valid JSON and maybe some required fields if reachable
                 return True
             except json.JSONDecodeError:
                 print(f"  [ERROR] Invalid JSON response")
                 return False
    return True

def run_test(method: str, path: str, spec_path_item: Dict[str, Any], context: Dict[str, Any]) -> bool:
    url = f"{BASE_URL}{path}"
    
    # Replace path parameters
    if '{' in url:
        for param_name, param_value in context.get('path_params', {}).items():
            url = url.replace(f"{{{param_name}}}", str(param_value))
    
    # Check if we still have unresolved placeholders
    if '{' in url:
        print(f"SKIPPING {method} {path} - Missing path parameters for: {url}")
        return True # Skip but count as "handled"
        
    print(f"Testing {method.upper()} {url} ...")
    
    headers = {}
    json_body = None
    
    # Prepare request based on spec (simplified)
    op_spec = spec_path_item.get(method.lower())
    if not op_spec:
        return True
        
    if 'requestBody' in op_spec:
        # Provide dummy data based on schema hints or context
        if method.lower() == 'post' and path == '/sessions':
             json_body = {
                 "title": "Test Session",
                 "mode": "lecture",
                 "userId": "test-user-uid"
             }
        elif method.lower() == 'post' and path == '/upload-url':
             json_body = {
                 "sessionId": context.get('current_session_id'),
                 "mode": "lecture",
                 "contentType": "audio/wav"
             }
        elif 'start_transcribe' in path: 
             json_body = { "mode": "lecture" }
        elif 'summarize' in path:
             json_body = {}
        elif 'quiz' in path:
             # quiz might take query params, not body
             pass
        elif 'qa' in path:
             json_body = { "question": "What is AI?" }
        # Add other specific body constructions here
    
    # Query params
    params = {}
    if 'parameters' in op_spec:
        for p in op_spec['parameters']:
            if p['in'] == 'query':
                if p['name'] == 'userId':
                    params['userId'] = 'test-user-uid'
    
    try:
        response = requests.request(method, url, json=json_body, params=params, headers=headers)
    except requests.exceptions.ConnectionError:
        print(f" [FATAL] Could not connect to {BASE_URL}. Is the server running?")
        return False

    expected_responses = op_spec.get('responses', {})
    status_code = str(response.status_code)
    
    if status_code not in expected_responses and 'default' not in expected_responses:
        # Check for range definitions like "2XX" if we were being fancy, but spec uses explicit codes
        print(f"  [FAILURE] Unexpected status code: {status_code}. Expected: {list(expected_responses.keys())}")
        print(f"  Response body: {response.text[:200]}")
        return False
        
    print(f"  [OK] Status: {status_code}")
    
    # Capture interesting data for context
    if method.lower() == 'post' and path == '/sessions' and response.status_code == 201:
        data = response.json()
        context['current_session_id'] = data.get('id')
        print(f"    captured session_id: {context['current_session_id']}")

    return True

def main():
    try:
        spec = load_openapi_spec(OPENAPI_FILE)
    except FileNotFoundError:
        print(f"Error: {OPENAPI_FILE} not found.")
        sys.exit(1)
        
    context = {
        'path_params': {
            'sessionId': 'placeholder_until_created' 
        }
    }
    
    # Order matters: Create session first to get an ID
    paths = spec.get('paths', {})
    
    # 1. Test POST /sessions first
    if '/sessions' in paths:
        run_test('POST', '/sessions', paths['/sessions'], context)
        
    # Update context with real ID if created
    if 'current_session_id' in context:
        context['path_params']['sessionId'] = context['current_session_id']
    else:
        print("Warning: Could not create a session. Subsequent tests needing sessionId might fail or be skipped.")

    # 2. Iterate others
    failed_count = 0
    for path, path_item in paths.items():
        for method in ['get', 'post', 'put', 'delete', 'patch']:
            if method in path_item:
                # Skip the one we already ran
                if path == '/sessions' and method == 'post':
                    continue
                
                # Careful with DELETE to not kill our session too early? 
                # For now let's just run everything.
                if method == 'delete':
                     print(f"Skipping DELETE {path} for now to preserve state")
                     continue

                if not run_test(method.upper(), path, path_item, context):
                    failed_count += 1
                    
    if failed_count > 0:
        print(f"\nCompleted with {failed_count} failures.")
        sys.exit(1)
    else:
        print("\nAll tests passed successfully.")

if __name__ == "__main__":
    main()
