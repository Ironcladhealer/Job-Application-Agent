# run.py - completely new approach
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the app directly instead of passing a string to uvicorn.
# When you pass a STRING like "api.main:app", uvicorn uses importlib
# which starts fresh without your sys.path.
# When you pass the APP OBJECT directly, it skips importlib entirely.
from api.main import app
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        app,          # ← object, not string "api.main:app"
        host="127.0.0.1",
        port=8000,
        reload=False, # reload MUST be False when passing an object
    )