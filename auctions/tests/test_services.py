from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.utils import timezone

from auctions.models import Auction, Bid, Order, ProxyBid, SecondChanceOffer, UserWallet
from auctions.services import (
    AuctionDomainException,
    close_auction,
    execute_bid,
    execute_buy_it_now,
    execute_proxy_bid,
    process_defaulted_order_and_second_chance,
    respond_to_second_chance_offer,
)

User = get_user_model()


class ProxyBiddingEngineTests(TransactionTestCase):

    def setUp(self):
        self.alice = User.objects.create_user(username="alice", password="pw")
        self.bob = User.objects.create_user(username="bob", password="pw")
        self.charlie = User.objects.create_user(username="charlie", password="pw")

        for u in (self.alice, self.bob, self.charlie):
            UserWallet.objects.create(user=u, balance=Decimal("10000.00"))

        self.auction = Auction.objects.create(
            title="Vintage Omega Seamaster",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=2),
            status=Auction.Status.ACTIVE,
        )

    def test_first_bidder_starts_at_starting_price(self):
        res = execute_proxy_bid(str(self.auction.id), self.alice, Decimal("200.00"))
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.current_price, Decimal("100.00"))
        self.assertEqual(self.auction.highest_bidder, self.alice)

    def test_lower_competing_proxy_bids_up_winner_without_overtaking(self):
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("200.00"))
        execute_proxy_bid(str(self.auction.id), self.bob, Decimal("150.00"))
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.current_price, Decimal("155.00"))
        self.assertEqual(self.auction.highest_bidder, self.alice)

    def test_higher_competing_proxy_overtakes_and_calculates_correct_step(self):
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("200.00"))
        execute_proxy_bid(str(self.auction.id), self.bob, Decimal("300.00"))
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.current_price, Decimal("205.00"))
        self.assertEqual(self.auction.highest_bidder, self.bob)

    def test_tied_proxy_bids_awarded_to_first_timestamp(self):
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("250.00"))
        execute_proxy_bid(str(self.auction.id), self.bob, Decimal("250.00"))
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.current_price, Decimal("250.00"))
        self.assertEqual(self.auction.highest_bidder, self.alice)

    def test_leading_bidder_can_increase_budget_without_raising_price(self):
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("200.00"))
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("350.00"))
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.current_price, Decimal("100.00"))
        proxy = ProxyBid.objects.get(auction=self.auction, bidder=self.alice)
        self.assertEqual(proxy.max_amount, Decimal("350.00"))


class SecondChanceOfferTests(TransactionTestCase):

    def setUp(self):
        self.alice = User.objects.create_user(username="winner_alice", password="pw")
        self.bob = User.objects.create_user(username="runnerup_bob", password="pw")

        for u in (self.alice, self.bob):
            UserWallet.objects.create(user=u, balance=Decimal("10000.00"))

        self.auction = Auction.objects.create(
            title="Collectible Mechanical Keyboard",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=2),
            status=Auction.Status.ACTIVE,
        )

        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("300.00"))
        execute_proxy_bid(str(self.auction.id), self.bob, Decimal("250.00"))

        close_res = close_auction(str(self.auction.id))
        self.order = Order.objects.get(id=close_res["order_id"])

    def test_defaulted_order_creates_offer_for_runner_up(self):
        result = process_defaulted_order_and_second_chance(str(self.order.id))
        self.assertEqual(result["action"], "offer_created")
        offer = SecondChanceOffer.objects.get(id=result["offer_id"])
        self.assertEqual(offer.recipient, self.bob)
        self.assertEqual(offer.amount, Decimal("250.00"))

    def test_runner_up_accepting_offer_creates_new_order(self):
        result = process_defaulted_order_and_second_chance(str(self.order.id))
        offer_id = result["offer_id"]

        resp = respond_to_second_chance_offer(offer_id, self.bob, accept=True)
        self.assertEqual(resp["status"], SecondChanceOffer.Status.ACCEPTED)
        new_order = Order.objects.get(id=resp["order_id"])
        self.assertEqual(new_order.buyer, self.bob)
        self.assertEqual(new_order.amount, Decimal("250.00"))

    def test_runner_up_declining_offer_marks_auction_unsold(self):
        result = process_defaulted_order_and_second_chance(str(self.order.id))
        offer_id = result["offer_id"]

        resp = respond_to_second_chance_offer(offer_id, self.bob, accept=False)
        self.assertEqual(resp["status"], SecondChanceOffer.Status.DECLINED)
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.status, Auction.Status.UNSOLD)


class AuctionCoreServicesTests(TransactionTestCase):

    def setUp(self):
        self.alice = User.objects.create_user(username="alice_core", password="pw")
        self.bob = User.objects.create_user(username="bob_core", password="pw")

        for u in (self.alice, self.bob):
            UserWallet.objects.create(user=u, balance=Decimal("10000.00"))

        self.auction = Auction.objects.create(
            title="Signed First Edition Book",
            starting_price=Decimal("50.00"),
            current_price=Decimal("50.00"),
            reserve_price=Decimal("100.00"),
            buy_it_now_price=Decimal("500.00"),
            ends_at=timezone.now() + timedelta(seconds=60),
            status=Auction.Status.ACTIVE,
        )

    def test_anti_sniping_extends_auction_ends_at(self):
        initial_end = self.auction.ends_at
        res = execute_proxy_bid(str(self.auction.id), self.alice, Decimal("80.00"))
        self.assertTrue(res["extended"])
        self.auction.refresh_from_db()
        self.assertGreater(self.auction.ends_at, initial_end)

    def test_buy_it_now_immediately_closes_auction_and_creates_order(self):
        result = execute_buy_it_now(str(self.auction.id), self.bob)
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.status, Auction.Status.CLOSED)
        order = Order.objects.get(id=result["order_id"])
        self.assertEqual(order.buyer, self.bob)
        self.assertEqual(order.amount, Decimal("500.00"))

    def test_close_auction_creates_order_for_highest_bidder(self):
        # Create an auction without reserve so it sells cleanly
        auction = Auction.objects.create(
            title="Collectible Coin",
            starting_price=Decimal("50.00"),
            current_price=Decimal("50.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )
        execute_proxy_bid(str(auction.id), self.alice, Decimal("150.00"))
        res = close_auction(str(auction.id))
        self.assertEqual(res["status"], Auction.Status.CLOSED)
        order = Order.objects.get(id=res["order_id"])
        self.assertEqual(order.buyer, self.alice)

    def test_close_auction_below_reserve_marks_unsold(self):
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("70.00"))
        res = close_auction(str(self.auction.id))
        self.assertEqual(res["status"], Auction.Status.UNSOLD)
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.status, Auction.Status.UNSOLD)