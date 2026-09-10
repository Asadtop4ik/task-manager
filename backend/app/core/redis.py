from redis.asyncio import Redis, from_url

from app.core.config import settings

_client: Redis | None = None


def get_redis() -> Redis:
    """One lazily-created client for the process.

    Redis on this box runs with `noeviction` on purpose — it holds FSM state and
    the arq queue, and evicting those loses real user work. Treat it as a store,
    not a cache.
    """
    global _client
    if _client is None:
        _client = from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
