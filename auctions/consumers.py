import time
from decimal import Decimal, InvalidOperation
from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
import redis.asyncio as aioredis

from .services import execute_proxy_bid, AuctionDomainException
from .serializers import mask_username


# In auctions/consumers.py

def get_redis_url() -> str:
    """Extract Redis connection URL from Django settings with safe fallbacks."""
    try:
        hosts = settings.CHANNEL_LAYERS["default"]["CONFIG"]["hosts"]
        host = hosts[0]
        if isinstance(host, dict):
            return host.get("address", "redis://127.0.0.1:6379/0")
        if isinstance(host, str):
            return host
        if isinstance(host, (list, tuple)):
            return f"redis://{host[0]}:{host[1]}/0"
    except (KeyError, IndexError, TypeError):
        pass
    return getattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")


_redis_pool = None


def get_redis_client():
    """Lazily creates async Redis connection pool."""
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            get_redis_url(),
            decode_responses=True,
            socket_timeout=30,
            socket_connect_timeout=5,
        )
    return _redis_pool
# Shared asynchronous Redis connection pool
redis_client = aioredis.from_url(get_redis_url(), decode_responses=True)

# Rate limiter parameters: Burst capacity of 3 bids, refills 1 token every second
RATE_LIMIT_BURST_CAPACITY = 3
RATE_LIMIT_REFILL_RATE = 1.0  # Tokens per second

# Atomic Token-Bucket Lua Script executed on Redis
TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'last_updated')
local tokens = tonumber(data[1])
local last_updated = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    last_updated = now
else
    local elapsed = math.max(0, now - last_updated)
    tokens = math.min(capacity, tokens + (elapsed * refill_rate))
    last_updated = now
end

if tokens >= cost then
    tokens = tokens - cost
    redis.call('HSET', key, 'tokens', tokens, 'last_updated', last_updated)
    redis.call('EXPIRE', key, math.ceil(capacity / refill_rate) + 10)
    return {1, '0'}
else
    redis.call('HSET', key, 'tokens', tokens, 'last_updated', last_updated)
    redis.call('EXPIRE', key, math.ceil(capacity / refill_rate) + 10)
    local wait_time = (cost - tokens) / refill_rate
    return {0, tostring(wait_time)}
end
"""


async def is_bid_rate_limited(user_id: int | str) -> tuple[bool, float]:
    """
    Evaluates token availability atomically in Redis.
    Returns: (is_allowed: bool, retry_after_seconds: float)
    """
    key = f"rate_limit:bid:{user_id}"
    now = time.time()
    try:
        result = await redis_client.eval(
            TOKEN_BUCKET_LUA,
            1,
            key,
            RATE_LIMIT_BURST_CAPACITY,
            RATE_LIMIT_REFILL_RATE,
            now,
            1,  # Cost per bid attempt
        )
        is_allowed = int(result[0]) == 1
        wait_time = float(result[1]) if not is_allowed else 0.0
        return is_allowed, wait_time
    except Exception:
        # Fail open if Redis is temporarily unreachable so bids are not dropped
        return True, 0.0


class AuctionConsumer(AsyncJsonWebsocketConsumer):
    """
    WebSocket consumer handling real-time live bidding, anti-sniping updates,
    targeted outbid notifications, spectator counters, and DDoS rate-limiting.
    """

    async def connect(self):
        self.auction_id = str(self.scope["url_route"]["kwargs"]["auction_id"])
        self.room_group_name = f"auction_{self.auction_id}"
        self.presence_key = f"viewers:auction:{self.auction_id}"
        self.user = self.scope.get("user")

        # 1. Join public auction room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,
        )

        # 2. Join private user notification group if authenticated
        if self.user and self.user.is_authenticated:
            self.user_group_name = f"user_{self.user.id}"
            await self.channel_layer.group_add(
                self.user_group_name,
                self.channel_name,
            )

        await self.accept()

        # 3. Register presence in Redis Set and set safety TTL
        try:
            await redis_client.sadd(self.presence_key, self.channel_name)
            await redis_client.expire(self.presence_key, 86400)
            active_viewers = await redis_client.scard(self.presence_key)
        except Exception:
            active_viewers = 1

        # 4. Broadcast updated spectator count to all viewers in the room
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "broadcast_viewer_count",
                "count": active_viewers,
            },
        )

    async def disconnect(self, close_code):
        # 1. Remove connection from Redis presence set
        if hasattr(self, "presence_key"):
            try:
                await redis_client.srem(self.presence_key, self.channel_name)
                remaining_viewers = await redis_client.scard(self.presence_key)
            except Exception:
                remaining_viewers = 0

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "broadcast_viewer_count",
                    "count": remaining_viewers,
                },
            )

        # 2. Leave channel groups
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name,
        )

        if hasattr(self, "user_group_name"):
            await self.channel_layer.group_discard(
                self.user_group_name,
                self.channel_name,
            )

    async def receive_json(self, content, **kwargs):
        action = content.get("action")

        if action == "place_bid":
            user = self.scope.get("user")

            # 1. Authentication Check
            if not user or not user.is_authenticated:
                await self.send_json({
                    "type": "error",
                    "message": "Authentication required to place bids.",
                })
                return

            # 2. Token-Bucket Rate Limiter Defense
            allowed, retry_after = await is_bid_rate_limited(user.id)
            if not allowed:
                await self.send_json({
                    "type": "error",
                    "code": "rate_limited",
                    "message": f"Bid rate limit exceeded. Please wait {retry_after:.1f}s before bidding again.",
                    "retry_after": round(retry_after, 2),
                })
                return

            # 3. Payload validation
            raw_amount = content.get("max_amount") or content.get("amount")
            if raw_amount is None:
                await self.send_json({
                    "type": "error",
                    "message": "Bid amount is required.",
                })
                return

            try:
                max_amount = Decimal(str(raw_amount))
            except (InvalidOperation, TypeError, ValueError):
                await self.send_json({
                    "type": "error",
                    "message": "Invalid bid amount format.",
                })
                return

            # 4. Invoke pessimistic locking transaction in PostgreSQL
            try:
                bid_result = await sync_to_async(execute_proxy_bid)(
                    auction_id=self.auction_id,
                    user=user,
                    max_amount=max_amount,
                )
            except AuctionDomainException as exc:
                await self.send_json({
                    "type": "error",
                    "message": str(exc),
                })
                return

            # 5. Private acknowledgment if max budget was supplied
            if content.get("max_amount"):
                await self.send_json({
                    "type": "proxy_bid_accepted",
                    "data": {
                        "your_max_budget": bid_result["caller_max_amount"],
                        "is_leading": bid_result["is_caller_leading"],
                    },
                })

            # 6. Public room broadcast (masked username)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "broadcast_bid",
                    "bid_data": {
                        "auction_id": bid_result["auction_id"],
                        "amount": bid_result["amount"],
                        "highest_bidder": mask_username(bid_result["highest_bidder"]),
                        "bid_id": bid_result["bid_id"],
                        "ends_at": bid_result["ends_at"],
                        "extended": bid_result["extended"],
                        "timestamp": bid_result["timestamp"],
                    },
                },
            )

            # 7. Targeted private alert to displaced highest bidder
            displaced_id = bid_result.get("displaced_user_id")
            if displaced_id:
                await self.channel_layer.group_send(
                    f"user_{displaced_id}",
                    {
                        "type": "outbid_notification",
                        "data": {
                            "auction_id": bid_result["auction_id"],
                            "auction_title": bid_result["auction_title"],
                            "new_price": bid_result["amount"],
                            "message": f"You have been outbid on '{bid_result['auction_title']}'. Current price is now ${bid_result['amount']}.",
                        },
                    },
                )

        else:
            await self.send_json({
                "type": "error",
                "message": f"Unsupported action: '{action}'.",
            })

    # -------------------------------------------------------------------------
    # Channel Layer Group Event Handlers
    # -------------------------------------------------------------------------

    async def broadcast_bid(self, event):
        await self.send_json({
            "type": "new_bid",
            "data": event["bid_data"],
        })

    async def broadcast_auction_closed(self, event):
        await self.send_json({
            "type": "auction_closed",
            "data": event["data"],
        })

    async def outbid_notification(self, event):
        await self.send_json({
            "type": "outbid_alert",
            "data": event["data"],
        })

    async def broadcast_viewer_count(self, event):
        await self.send_json({
            "type": "viewer_count",
            "count": event["count"],
        })
    async def second_chance_notification(self, event):
        """
        Pushed to user_<id> when a second-chance offer is issued.
        """
        await self.send_json({
            "type": "second_chance_offer",
            "data": event["data"],
        })