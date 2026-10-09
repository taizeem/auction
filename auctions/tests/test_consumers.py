from decimal import Decimal
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone
from channels.testing import WebsocketCommunicator
from rest_framework_simplejwt.tokens import AccessToken
from config.asgi import application
from auctions.models import Auction

User = get_user_model()


class AuctionConsumerTests(TransactionTestCase):
    def tearDown(self):
        connections.close_all()

    async def test_authenticated_websocket_bid_flow(self):
        user = await User.objects.acreate(
            username="ws_bidder",
            password="Password123!",
            email="ws@test.com",
        )
        auction = await Auction.objects.acreate(
            title="Collector Vinyl",
            starting_price=Decimal("50.00"),
            current_price=Decimal("50.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )

        token = str(AccessToken.for_user(user))
        path = f"/ws/auctions/{auction.id}/?token={token}"

        communicator = WebsocketCommunicator(application, path)
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        await communicator.send_json_to({
            "action": "place_bid",
            "amount": "75.00",
        })

        response = await communicator.receive_json_from()
        self.assertEqual(response["type"], "new_bid")
        self.assertEqual(response["data"]["amount"], "75.00")
        self.assertEqual(response["data"]["highest_bidder"], "ws_bidder")

        await communicator.disconnect()

    async def test_unauthenticated_connection_cannot_bid(self):
        auction = await Auction.objects.acreate(
            title="Unauthenticated Item",
            starting_price=Decimal("10.00"),
            current_price=Decimal("10.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )

        communicator = WebsocketCommunicator(application, f"/ws/auctions/{auction.id}/")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        await communicator.send_json_to({
            "action": "place_bid",
            "amount": "20.00",
        })

        response = await communicator.receive_json_from()
        self.assertEqual(response["type"], "error")
        self.assertIn("Authentication required", response["message"])

        await communicator.disconnect()