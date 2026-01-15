import yaml
from app.main import app
import json

def generate_openapi():
    # Make sure to set any necessary environment variables if needed for app import
    # But usually app definition is enough
    openapi_schema = app.openapi()
    
    # Convert to YAML
    with open("openapi.yaml", "w") as f:
        yaml.dump(openapi_schema, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    
    print("openapi.yaml generated successfully.")

if __name__ == "__main__":
    generate_openapi()
