import requests
import sys

# BASE_URL = "http://localhost:8000" # Local
BASE_URL = "https://classnote-api-735504746686.asia-northeast1.run.app" # Production (Need token...)

# Since I cannot easily get a token without UI interaction or a service account key that I can use to sign a token,
# I will output the curl commands for the user to run.

print("This script is a placeholder. Please run the following curl command to test:")
print("\n# 1. Claim Username")
print('curl -X POST "https://api.classnote-x.app/users/claim-username" \\')
print('  -H "Authorization: Bearer <ID_TOKEN>" \\')
print('  -H "Content-Type: application/json" \\')
print('  -d \'{"username": "your_handle"}\'')

print("\n# 2. Lookup Users")
print('curl "https://api.classnote-x.app/users/lookup?uids=<UID>" \\')
print('  -H "Authorization: Bearer <ID_TOKEN>"')
