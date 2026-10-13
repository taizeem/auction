from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.utils import timezone
from channels.testing import WebsocketCommunicator
from rest_framework_simplejwt.tokens import AccessToken

from auctions.models import Auction, UserWallet
from config.asgi import application

User = get_user_model()


async def drain_until_type(communicator, expected_type, timeout=3.0):
    start = timezone.now()
    while (timezone.now() - start).total_seconds() < timeout:
        msg = await communicator.receive_json_from(timeout=timeout)
        msg_type = msg.get("type")
        if msg_type == expected_type or (
            expected_type in ("viewer_count", "broadcast_viewer_count")
            and msg_type in ("viewer_count", "broadcast_viewer_count")
        ):
            return msg
    raise TimeoutError(f"Never received message type '{expected_type}'")


class AuctionConsumerTests(TransactionTestCase):

    def setUp(self):
        self.seller = User.objects.create_user(username="item_seller", password="pw")
        self.user = User.objects.create_user(username="live_bidder", password="pw")
        self.user_b = User.objects.create_user(username="live_bidder_b", password="pw")

        for u in (self.seller, self.user, self.user_b):
            UserWallet.objects.create(user=u, balance=Decimal("10000.00"))

        self.auction = Auction.objects.create(
            seller=self.seller,
            title="Sealed Base Set Booster Pack",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )

    async def test_authenticated_websocket_bid_flow(self):
        token = AccessToken.for_user(self.user)
        communicator = WebsocketCommunicator(
            application,
            f"/ws/auctions/{self.auction.id}/?token={str(token)}",
        )
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        await communicator.send_json_to({"action": "place_bid", "max_amount": "150.00"})
        new_bid_msg = await drain_until_type(communicator, "new_bid")
        self.assertEqual(new_bid_msg["type"], "new_bid")
        self.assertEqual(Decimal(str(new_bid_msg["data"]["amount"])), Decimal("100.00"))

        await communicator.disconnect()

    async def test_unauthenticated_websocket_bid_rejected(self):
        communicator = WebsocketCommunicator(
            application,
            f"/ws/auctions/{self.auction.id}/",
        )
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        await communicator.send_json_to({"action": "place_bid", "max_amount": "150.00"})
        err_msg = await drain_until_type(communicator, "error")
        self.assertEqual(err_msg["type"], "error")
        self.assertIn("Authentication required", err_msg["message"])

        await communicator.disconnect()

    async def test_displaced_bidder_receives_targeted_outbid_alert(self):
        token_a = AccessToken.for_user(self.user)
        token_b = AccessToken.for_user(self.user_b)

        comm_a = WebsocketCommunicator(
            application,
            f"/ws/auctions/{self.auction.id}/?token={str(token_a)}",
        )
        comm_b = WebsocketCommunicator(
            application,
            f"/ws/auctions/{self.auction.id}/?token={str(token_b)}",
        )

        connected_a, _ = await comm_a.connect()
        connected_b, _ = await comm_b.connect()
        self.assertTrue(connected_a)
        self.assertTrue(connected_b)

        # Alice bids 150
        await comm_a.send_json_to({"action": "place_bid", "max_amount": "150.00"})
        await drain_until_type(comm_a, "new_bid")

        # Bob outbids Alice with 200
        await comm_b.send_json_to({"action": "place_bid", "max_amount": "200.00"})

        # Alice must receive targeted outbid alert
        outbid_alert = await drain_until_type(comm_a, "targeted_outbid_alert")
        self.assertEqual(outbid_alert["type"], "targeted_outbid_alert")
        self.assertIn("outbid", outbid_alert["data"]["message"])

        await comm_a.disconnect()
        await comm_b.disconnect()

    async def test_rapid_bid_spam_triggers_token_bucket_rate_limiter(self):
        seller = await User.objects.acreate(username="spam_seller", password="pw")
        spammer = await User.objects.acreate(username="bot_user", password="pw")
        await UserWallet.objects.acreate(user=spammer, balance=Decimal("10000.00"))

        auction = await Auction.objects.acreate(
            seller=seller,
            title="Rate Limit Target",
            starting_price=Decimal("10.00"),
            current_price=Decimal("10.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )

        token = AccessToken.for_user(spammer)
        communicator = WebsocketCommunicator(
            application,
            f"/ws/auctions/{auction.id}/?token={str(token)}",
        )
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # Send a rapid burst of 10 bids to guarantee exhausting bucket capacity
        for i in range(1, 11):
            await communicator.send_json_to({
                "action": "place_bid",
                "max_amount": str(10 + (i * 10)),
            })

        rate_limited = False
        # Drain frames until the rate_limited message arrives
        for _ in range(25):
            try:
                msg = await communicator.receive_json_from(timeout=1.0)
                if msg.get("code") == "rate_limited":
                    rate_limited = True
                    break
            except TimeoutError:
                break

        self.assertTrue(rate_limited)
        await communicator.disconnect()

    async def test_viewer_presence_count_broadcasts_on_connect_and_disconnect(self):
        communicator = WebsocketCommunicator(
            application,
            f"/ws/auctions/{self.auction.id}/",
        )
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        viewer_msg = await drain_until_type(communicator, "viewer_count")
        self.assertIn(viewer_msg["type"], ("viewer_count", "broadcast_viewer_count"))
        self.assertGreaterEqual(viewer_msg["count"], 1)

        await communicator.disconnect()