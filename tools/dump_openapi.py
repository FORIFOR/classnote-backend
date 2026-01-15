
import sys
import yaml
import json
from fastapi.openapi.utils import get_openapi
from app.main import app

def dump_openapi():
    # Generate OpenAPI JSON
    openapi_json = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        description=app.description,
        routes=app.routes,
    )
    
    # Dump to YAML
    with open("openapi.yaml", "w") as f:
        yaml.dump(openapi_json, f, sort_keys=False, allow_unicode=True)

if __name__ == "__main__":
    try:
        dump_openapi()
        print("Successfully generated openapi.yaml")
    except Exception as e:
        print(f"Failed to generate openapi.yaml: {e}")
        sys.exit(1)
