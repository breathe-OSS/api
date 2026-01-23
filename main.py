import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.api.routes import register_zone_routes
from app.services.fetchers import update_all_zones_background

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(periodic_updates())
    yield
    task.cancel()

async def periodic_updates():
    while True:
        try:
            await update_all_zones_background()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"CRITICAL: Background loop error: {e}")
        
        # Wait 15 minutes
        await asyncio.sleep(900)

app = FastAPI(title="breathe backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://breatheoss.app",       
        "https://www.breatheoss.app",   
        "http://localhost:3000",        
        "http://localhost:8080",        
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_zone_routes(app)