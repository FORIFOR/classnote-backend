
import sys
import os

# Mock google-auth and others if needed, but try real import first
# fast-fail on import errors
try:
    print("Importing app.util_models...")
    import app.util_models
    print("Importing app.routes.share...")
    import app.routes.share
    print("Importing app.routes.users...")
    import app.routes.users
    print("Importing app.main...")
    import app.main
    print("SUCCESS: app.main imported")
except Exception as e:
    print(f"CRASH: {e}")
    import traceback
    traceback.print_exc()
