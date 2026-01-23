import sys
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# FIX: Set the event loop policy at the absolute top for Windows
if sys.platform == 'win32':
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

from app.services.indexer import init_db
from app.api.routes import router as api_router
from app.api.websockets import router as ws_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log the loop type for verification
    loop = asyncio.get_running_loop()
    print(f"Current Event Loop: {type(loop).__name__}")
    
    # Initialize Database
    try:
        await init_db()
        print("Database initialized.")
    except Exception as e:
        print(f"Failed to initialize database: {e}")
    
    yield

app = FastAPI(lifespan=lifespan)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include Routers
app.include_router(api_router, prefix="/app")
app.include_router(ws_router, prefix="/app")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, reload=True)
