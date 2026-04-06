import uvicorn
from backend.core.config import settings

def main():
    print(f"Starting qwen2API v2.0 Enterprise Gateway on port {settings.PORT} with {settings.WORKERS} workers...", flush=True)
    uvicorn.run("backend.main:app", host="0.0.0.0", port=settings.PORT, workers=settings.WORKERS)

if __name__ == "__main__":
    main()