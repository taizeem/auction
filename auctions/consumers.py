from decimal import Decimal, InvalidOperation
from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from .services import execute_bid, AuctionDomainException


class AuctionConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.auction_id = str(self.scope["url_route"]["kwargs"]["auction_id"])
        self.room_group_name = f"auction_{self.auction_id}"

        # Join the auction pub/sub room group in Redis
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,
        )
        await self.accept()

    async def disconnect(self, close_code):
        # Leave room group
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name,
        )

    async def receive_json(self, content):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.send_json({
                "type": "error",
                "message": "Authentication required to participate in bidding.",
            })
            return

        action = content.get("action")

        if action == "place_bid":
            raw_amount = content.get("amount")
            try:
                amount = Decimal(str(raw_amount))
            except (InvalidOperation, TypeError):
                await self.send_json({
                    "type": "error",
                    "message": "Invalid bid amount provided.",
                })
                return

            # Offload atomic database transaction with row-locking to thread pool
            try:
                bid_data = await sync_to_async(execute_bid)(
                    auction_id=self.auction_id,
                    user=user,
                    amount=amount,
                )
            except AuctionDomainException as err:
                await self.send_json({
                    "type": "error",
                    "message": str(err),
                })
                return

            # Broadcast new winning bid to all connected clients
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "broadcast_bid",
                    "payload": bid_data,
                },
            )

    async def broadcast_bid(self, event):
        """Handler for 'broadcast_bid' messages dispatched by group_send."""
        await self.send_json({
            "type": "new_bid",
            "data": event["payload"],
        })

    async def broadcast_closure(self, event):
        """Handler for 'broadcast_closure' messages dispatched by Celery tasks."""
        await self.send_json({
            "type": "auction_closed",
            "data": event["payload"],
        })