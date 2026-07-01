import os
import ssl
import redis.asyncio as redis

redis_client: redis.Redis = None

async def init_redis_pool():
    global redis_client
    redis_url = os.getenv("UPSTASH_REDIS_URL", "redis://localhost:6379")
    
    kwargs = {
        "max_connections": 15,
        "decode_responses": True
    }
    
    redis_pool = redis.ConnectionPool.from_url(
        redis_url,
        **kwargs
    )
    redis_client = redis.Redis(connection_pool=redis_pool)

async def close_redis_pool():
    global redis_client
    if redis_client:
        await redis_client.close()
        await redis_client.connection_pool.disconnect()
