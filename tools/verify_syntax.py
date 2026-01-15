
import ast
import sys

files = [
    "/Users/horioshuuhei/Projects/classnote-api/app/routes/sessions.py",
    "/Users/horioshuuhei/Projects/classnote-api/app/services/google_speech.py"
]

for file_path in files:
    try:
        with open(file_path, "r") as f:
            source = f.read()
        ast.parse(source)
        print(f"Syntax OK: {file_path}")
    except Exception as e:
        print(f"Syntax Error in {file_path}: {e}")
        sys.exit(1)
