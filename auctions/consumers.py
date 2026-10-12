from decimal import Decimal, InvalidOperation
from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from .services import execute_proxy_bid, AuctionDomainException
from .serializers import mask_username


class AuctionConsumer(AsyncJsonWebsocketConsumer):
    """
    WebSocket consumer handling real-time live bidding, anti-sniping updates,
    and targeted outbid notifications.
    """

    async def connect(self):
        self.auction_id = str(self.scope["url_route"]["kwargs"]["auction_id"])
        self.room_group_name = f"auction_{self.auction_id}"
        self.user = self.scope.get("user")

        # 1. Join public auction group
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

    async def disconnect(self, close_code):
        # 1. Leave public auction group
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name,
        )

        # 2. Leave private user group
        if hasattr(self, "user_group_name"):
            await self.channel_layer.group_discard(
                self.user_group_name,
                self.channel_name,
            )

    async def receive_json(self, content, **kwargs):
        action = content.get("action")

        if action == "place_bid":
            user = self.scope.get("user")
            if not user or not user.is_authenticated:
                await self.send_json({
                    "type": "error",
                    "message": "Authentication required to place bids.",
                })
                return

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

            # Private acknowledgment to caller
            if content.get("max_amount"):
                await self.send_json({
                    "type": "proxy_bid_accepted",
                    "data": {
                        "your_max_budget": bid_result["caller_max_amount"],
                        "is_leading": bid_result["is_caller_leading"],
                    },
                })

            # Public broadcast to auction room
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

            # Targeted private notification to the displaced user
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
        """
        Pushed directly to the user's private channel group when outbid.
        """
        await self.send_json({
            "type": "outbid_alert",
            "data": event["data"],
        })