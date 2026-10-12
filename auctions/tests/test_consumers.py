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
from auctions.serializers import mask_username

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
        self.assertEqual(response["data"]["amount"], "50.00")
        self.assertEqual(response["data"]["highest_bidder"], mask_username("ws_bidder"))

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

    async def test_displaced_bidder_receives_targeted_outbid_alert(self):
        # 1. Setup bidders and auction
        bidder_a = await User.objects.acreate(username="alice", password="pw")
        bidder_b = await User.objects.acreate(username="bob", password="pw")

        auction = await Auction.objects.acreate(
            title="Vintage Omega Watch",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )

        # 2. Alice connects and places a $150 budget
        token_a = str(AccessToken.for_user(bidder_a))
        comm_a = WebsocketCommunicator(application, f"/ws/auctions/{auction.id}/?token={token_a}")
        connected_a, _ = await comm_a.connect()
        self.assertTrue(connected_a)

        await comm_a.send_json_to({
            "action": "place_bid",
            "max_amount": "150.00",
        })

        # Alice receives private confirmation and public broadcast
        _ = await comm_a.receive_json_from()  # proxy_bid_accepted
        _ = await comm_a.receive_json_from()  # new_bid

        # 3. Bob connects and outbids Alice with $250
        token_b = str(AccessToken.for_user(bidder_b))
        comm_b = WebsocketCommunicator(application, f"/ws/auctions/{auction.id}/?token={token_b}")
        connected_b, _ = await comm_b.connect()
        self.assertTrue(connected_b)

        await comm_b.send_json_to({
            "action": "place_bid",
            "max_amount": "250.00",
        })

        # Alice's socket receives the public new_bid broadcast
        public_event_alice = await comm_a.receive_json_from()
        self.assertEqual(public_event_alice["type"], "new_bid")

        # Alice's socket receives the TARGETED outbid alert
        outbid_event_alice = await comm_a.receive_json_from()
        self.assertEqual(outbid_event_alice["type"], "outbid_alert")
        self.assertIn("Vintage Omega Watch", outbid_event_alice["data"]["message"])
        self.assertEqual(outbid_event_alice["data"]["auction_id"], str(auction.id))

        await comm_a.disconnect()
        await comm_b.disconnect()