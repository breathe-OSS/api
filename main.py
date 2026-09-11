# SPDX-License-Identifier: MIT
#
# Copyright (C) 2026 The Breathe Open Source Project
# Copyright (C) 2026 sidharthify <wednisegit@gmail.com>
# Copyright (C) 2026 FlashWreck <theghost3370@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# application entry point. wires up sentry, cors and the routes, and owns the
# background loop that keeps every zone's readings fresh.

import os
import asyncio
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import register_zone_routes
from app.core.redis_client import init_redis_pool, close_redis_pool
from app.services.fetchers import update_all_zones_background

# how long the background loop sleeps between passes. the airgradient sensors
# report roughly every fifteen minutes, so polling faster only costs quota.
UPDATE_INTERVAL_SECONDS = 900


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open redis and start the update loop on boot, tear both down on shutdown."""
    await init_redis_pool()
    task = asyncio.create_task(periodic_updates())

    yield

    task.cancel()
    await close_redis_pool()


async def periodic_updates() -> None:
    """Refresh every zone, then the derived rollups, forever."""
    while True:
        try:
            await update_all_zones_background()

            # these are imported here rather than at module scope because both
            # modules import from app.core, and pulling them in at the top would
            # close an import cycle.
            from app.core.database import refresh_15m_rollups, refresh_stale_node_offsets
            from app.services.seasonal import refresh_stale_climatology

            # the rollups are blocking sqlite work, so they go to a worker thread
            # instead of stalling the event loop for everyone else.
            await asyncio.to_thread(refresh_15m_rollups)
            await asyncio.to_thread(refresh_stale_node_offsets)

            await refresh_stale_climatology()

        except asyncio.CancelledError:
            # raised when lifespan cancels us on shutdown. this is the one
            # exception we must not swallow, or the process will not exit.
            break

        except Exception as e:
            # anything else is a bad pass, not a reason to stop polling.
            sentry_sdk.capture_exception(e)
            print(f"CRITICAL: Background loop error: {e}")

        await asyncio.sleep(UPDATE_INTERVAL_SECONDS)


sentry_dsn = os.getenv("SENTRY_DSN")
if sentry_dsn:
    sentry_sdk.init(
        dsn=sentry_dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.0")),
        send_default_pii=False,
    )

app = FastAPI(title="breathe backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://breatheoss.app",
        "https://www.breatheoss.app",
        "https://about.breatheoss.app",
        "http://localhost:3000",
        "http://localhost:8080",
        "https://claude.ai",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_zone_routes(app)
