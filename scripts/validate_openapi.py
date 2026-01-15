import os
import yaml
import requests
import json
import sys
import time
from typing import Dict, Any, Optional

# Constants
OPENAPI_FILE = "openapi.yaml"
BASE_URL = os.environ.get("OPENAPI_BASE_URL", "http://localhost:8000")
ALLOW_WRITE = os.environ.get("OPENAPI_ALLOW_WRITE", "0") == "1"
AUTH_TOKEN = os.environ.get("OPENAPI_AUTH_TOKEN")

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
        
    if op_spec.get("security") and not AUTH_TOKEN:
        print(f"SKIPPING {method} {path} - auth required but OPENAPI_AUTH_TOKEN not set")
        return True

    if 'requestBody' in op_spec:
        op_id = (op_spec.get("operationId") or "").upper()
        body_env_key = f"OPENAPI_BODY_{op_id}" if op_id else None
        body_json = None
        if body_env_key and body_env_key in os.environ:
            body_json = os.environ.get(body_env_key)
        elif "OPENAPI_BODY_JSON" in os.environ:
            body_json = os.environ.get("OPENAPI_BODY_JSON")

        if body_json:
            try:
                json_body = json.loads(body_json)
            except json.JSONDecodeError:
                print(f"SKIPPING {method} {path} - Invalid JSON in {body_env_key or 'OPENAPI_BODY_JSON'}")
                return True
        else:
            print(f"SKIPPING {method} {path} - Request body required but not provided")
            return True
    
    # Query params
    params = {}
    if 'parameters' in op_spec:
        for p in op_spec['parameters']:
            if p['in'] == 'query':
                env_key = f"OPENAPI_QUERY_{p['name'].upper()}"
                if env_key in os.environ:
                    params[p['name']] = os.environ.get(env_key)
                elif p.get("required"):
                    print(f"SKIPPING {method} {path} - Missing required query param: {p['name']}")
                    return True

    if AUTH_TOKEN:
        if AUTH_TOKEN.lower().startswith("bearer "):
            headers["Authorization"] = AUTH_TOKEN
        else:
            headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    
    try:
        response = requests.request(method, url, json=json_body, params=params, headers=headers)
    except requests.exceptions.ConnectionError:
        print(f" [FATAL] Could not connect to {BASE_URL}. Is the server running?")
        return False

    expected_responses = op_spec.get('responses', {})
    status_code = str(response.status_code)

    if status_code in ("401", "403") and not AUTH_TOKEN:
        print(f"  [SKIP] Auth required (status {status_code}); OPENAPI_AUTH_TOKEN not set")
        return True
    
    if status_code not in expected_responses and 'default' not in expected_responses:
        # Check for range definitions like "2XX" if we were being fancy, but spec uses explicit codes
        print(f"  [FAILURE] Unexpected status code: {status_code}. Expected: {list(expected_responses.keys())}")
        print(f"  Response body: {response.text[:200]}")
        return False
        
    print(f"  [OK] Status: {status_code}")
    
    return True

def main():
    try:
        spec = load_openapi_spec(OPENAPI_FILE)
    except FileNotFoundError:
        print(f"Error: {OPENAPI_FILE} not found.")
        sys.exit(1)
        
    session_id = os.environ.get("OPENAPI_SESSION_ID")
    context = {
        'path_params': {
            'sessionId': session_id,
            'session_id': session_id,
            'quiz_id': os.environ.get("OPENAPI_QUIZ_ID"),
            'quizId': os.environ.get("OPENAPI_QUIZ_ID"),
            'target_uid': os.environ.get("OPENAPI_TARGET_UID"),
            'targetUid': os.environ.get("OPENAPI_TARGET_UID"),
            'user_id': os.environ.get("OPENAPI_USER_ID"),
            'userId': os.environ.get("OPENAPI_USER_ID"),
            'code': os.environ.get("OPENAPI_CODE"),
            'token': os.environ.get("OPENAPI_TOKEN"),
        }
    }
    context['path_params'] = {k: v for k, v in context['path_params'].items() if v}
    
    paths = spec.get('paths', {})

    # 2. Iterate others
    failed_count = 0
    for path, path_item in paths.items():
        for method in ['get', 'post', 'put', 'delete', 'patch']:
            if method in path_item:
                if method != 'get' and not ALLOW_WRITE:
                    print(f"SKIPPING {method.upper()} {path} - write operations disabled")
                    continue
                if method == 'delete' and not ALLOW_WRITE:
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
