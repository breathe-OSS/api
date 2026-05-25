import os
import redis.asyncio as redis

# We will initialize this from main.py's lifespan
redis_client: redis.Redis = None

async def init_redis_pool():
    global redis_client
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    
    # We use a max_connections=15 to stay well below the 20 connection limit 
    # of the Heroku Key-Value Store Mini plan.
    redis_pool = redis.ConnectionPool.from_url(
        redis_url,
        max_connections=15,
        decode_responses=True
    )
    redis_client = redis.Redis(connection_pool=redis_pool)

async def close_redis_pool():
    global redis_client
    if redis_client:
        await redis_client.close()
