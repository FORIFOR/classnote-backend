import uvicorn
import os

# Set dummy project ID to avoid warnings/errors
os.environ["GOOGLE_CLOUD_PROJECT"] = "classnote-debug"
os.environ["FIRESTORE_EMULATOR_HOST"] = "localhost:8080" # Optional: if you have emulator

if __name__ == "__main__":
    # Reload=True allows you to see changes immediately
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
