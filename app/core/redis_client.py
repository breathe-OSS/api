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

# redis is used as the hot cache in front of sqlite. every zone payload we build
# gets stored here so repeat requests never touch the database or the upstream
# apis. losing redis is survivable, callers fall back to recomputing.

import os
from typing import Optional

import redis.asyncio as redis

redis_client: Optional[redis.Redis] = None


async def init_redis_pool() -> None:
    """Open the shared connection pool. Called once from the app lifespan."""
    global redis_client

    redis_url = os.getenv("UPSTASH_REDIS_URL", "redis://localhost:6379")

    # the pool is capped well below upstash's connection limit, since every
    # worker process opens its own.
    redis_pool = redis.ConnectionPool.from_url(
        redis_url,
        max_connections=15,
        decode_responses=True,
    )
    redis_client = redis.Redis(connection_pool=redis_pool)


async def close_redis_pool() -> None:
    """Close the client and drop every pooled connection."""
    if redis_client is None:
        return

    await redis_client.close()
    await redis_client.connection_pool.disconnect()


async def check_redis_health() -> bool:
    """Ping redis. Returns False rather than raising, so /health can report it."""
    if redis_client is None:
        return False

    try:
        return bool(await redis_client.ping())
    except Exception:
        return False
