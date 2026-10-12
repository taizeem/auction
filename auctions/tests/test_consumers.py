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

    async def test_live_spectator_counter_increments_and_decrements(self):
        auction = await Auction.objects.acreate(
            title="Presence Test Item",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )

        path = f"/ws/auctions/{auction.id}/"

        # 1. First viewer connects
        client_1 = WebsocketCommunicator(application, path)
        connected_1, _ = await client_1.connect()
        self.assertTrue(connected_1)

        msg_1 = await client_1.receive_json_from()
        self.assertEqual(msg_1["type"], "viewer_count")
        self.assertEqual(msg_1["count"], 1)

        # 2. Second viewer connects
        client_2 = WebsocketCommunicator(application, path)
        connected_2, _ = await client_2.connect()
        self.assertTrue(connected_2)

        # Both clients receive the incremented count of 2
        msg_2_self = await client_2.receive_json_from()
        self.assertEqual(msg_2_self["type"], "viewer_count")
        self.assertEqual(msg_2_self["count"], 2)

        msg_1_update = await client_1.receive_json_from()
        self.assertEqual(msg_1_update["type"], "viewer_count")
        self.assertEqual(msg_1_update["count"], 2)

        # 3. Second viewer disconnects
        await client_2.disconnect()

        # Client 1 receives the decremented count of 1
        msg_1_after_disconnect = await client_1.receive_json_from()
        self.assertEqual(msg_1_after_disconnect["type"], "viewer_count")
        self.assertEqual(msg_1_after_disconnect["count"], 1)

        await client_1.disconnect()

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

        # Consume initial spectator count broadcast
        viewer_msg = await communicator.receive_json_from()
        self.assertEqual(viewer_msg["type"], "viewer_count")

        # Place bid
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

        # Consume initial spectator count broadcast
        viewer_msg = await communicator.receive_json_from()
        self.assertEqual(viewer_msg["type"], "viewer_count")

        await communicator.send_json_to({
            "action": "place_bid",
            "amount": "20.00",
        })

        response = await communicator.receive_json_from()
        self.assertEqual(response["type"], "error")
        self.assertIn("Authentication required", response["message"])

        await communicator.disconnect()

    async def test_displaced_bidder_receives_targeted_outbid_alert(self):
        bidder_a = await User.objects.acreate(username="alice", password="pw")
        bidder_b = await User.objects.acreate(username="bob", password="pw")

        auction = await Auction.objects.acreate(
            title="Vintage Omega Watch",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )

        token_a = str(AccessToken.for_user(bidder_a))
        comm_a = WebsocketCommunicator(application, f"/ws/auctions/{auction.id}/?token={token_a}")
        connected_a, _ = await comm_a.connect()
        self.assertTrue(connected_a)
        _ = await comm_a.receive_json_from()  # Consume initial viewer_count: 1

        await comm_a.send_json_to({
            "action": "place_bid",
            "max_amount": "150.00",
        })

        _ = await comm_a.receive_json_from()  # proxy_bid_accepted
        _ = await comm_a.receive_json_from()  # new_bid

        # Bob connects (triggers viewer_count: 2 for both sockets)
        token_b = str(AccessToken.for_user(bidder_b))
        comm_b = WebsocketCommunicator(application, f"/ws/auctions/{auction.id}/?token={token_b}")
        connected_b, _ = await comm_b.connect()
        self.assertTrue(connected_b)

        _ = await comm_b.receive_json_from()  # comm_b's initial viewer_count: 2
        _ = await comm_a.receive_json_from()  # comm_a's updated viewer_count: 2

        # Bob outbids Alice
        await comm_b.send_json_to({
            "action": "place_bid",
            "max_amount": "250.00",
        })

        # Alice receives the public price tick
        public_event_alice = await comm_a.receive_json_from()
        self.assertEqual(public_event_alice["type"], "new_bid")

        # Alice receives her targeted outbid notification
        outbid_event_alice = await comm_a.receive_json_from()
        self.assertEqual(outbid_event_alice["type"], "outbid_alert")
        self.assertIn("Vintage Omega Watch", outbid_event_alice["data"]["message"])
        self.assertEqual(outbid_event_alice["data"]["auction_id"], str(auction.id))

        await comm_a.disconnect()
        await comm_b.disconnect()
    async def test_rapid_bid_spam_triggers_token_bucket_rate_limiter(self):
        spammer = await User.objects.acreate(
            username="bot_user",
            password="pw",
            email="bot@example.com",
        )
        auction = await Auction.objects.acreate(
            title="Rate Limit Target",
            starting_price=Decimal("10.00"),
            current_price=Decimal("10.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )

        token = str(AccessToken.for_user(spammer))
        path = f"/ws/auctions/{auction.id}/?token={token}"

        communicator = WebsocketCommunicator(application, path)
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        _ = await communicator.receive_json_from()  # viewer_count

        # Send 3 rapid bids (consumes all 3 burst tokens)
        for amount in ["20.00", "30.00", "40.00"]:
            await communicator.send_json_to({"action": "place_bid", "max_amount": amount})
            _ = await communicator.receive_json_from()  # proxy_bid_accepted
            _ = await communicator.receive_json_from()  # new_bid

        # Send 4th rapid bid immediately (burst bucket is empty)
        await communicator.send_json_to({"action": "place_bid", "max_amount": "50.00"})
        throttled_response = await communicator.receive_json_from()

        self.assertEqual(throttled_response["type"], "error")
        self.assertEqual(throttled_response["code"], "rate_limited")
        self.assertIn("Bid rate limit exceeded", throttled_response["message"])
        self.assertGreater(throttled_response["retry_after"], 0.0)

        await communicator.disconnect()