# auctions/tests/test_increments.py
from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.utils import timezone

from auctions.models import Auction, UserWallet
from auctions.services import (
    AuctionDomainException,
    execute_proxy_bid,
    get_minimum_bid_increment,
)

User = get_user_model()


class DynamicBidIncrementTests(TransactionTestCase):

    def setUp(self):
        self.seller = User.objects.create_user(username="inc_seller", password="pw")
        self.alice = User.objects.create_user(username="inc_alice", password="pw")
        self.bob = User.objects.create_user(username="inc_bob", password="pw")

        for u in (self.seller, self.alice, self.bob):
            w, _ = UserWallet.objects.get_or_create(user=u)
            w.balance = Decimal("50000.00")
            w.save()

    def test_increment_helper_tiers(self):
        """Validates that bracket lookup returns correct increment per tier."""
        self.assertEqual(get_minimum_bid_increment(Decimal("10.00")), Decimal("1.00"))
        self.assertEqual(get_minimum_bid_increment(Decimal("99.99")), Decimal("1.00"))
        self.assertEqual(get_minimum_bid_increment(Decimal("100.00")), Decimal("5.00"))
        self.assertEqual(get_minimum_bid_increment(Decimal("450.00")), Decimal("5.00"))
        self.assertEqual(get_minimum_bid_increment(Decimal("500.00")), Decimal("25.00"))
        self.assertEqual(get_minimum_bid_increment(Decimal("950.00")), Decimal("25.00"))
        self.assertEqual(get_minimum_bid_increment(Decimal("1000.00")), Decimal("50.00"))
        self.assertEqual(get_minimum_bid_increment(Decimal("4999.00")), Decimal("50.00"))
        self.assertEqual(get_minimum_bid_increment(Decimal("5000.00")), Decimal("100.00"))
        self.assertEqual(get_minimum_bid_increment(Decimal("25000.00")), Decimal("100.00"))

    def test_under_100_tier_requires_one_dollar_increment(self):
        """Items under $100 increment by $1.00."""
        auction = Auction.objects.create(
            seller=self.seller,
            title="Trading Card",
            starting_price=Decimal("20.00"),
            current_price=Decimal("20.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )
        execute_proxy_bid(str(auction.id), self.alice, Decimal("50.00"))

        # Next competitor must bid at least $20 + $1 = $21
        with self.assertRaises(AuctionDomainException) as ctx:
            execute_proxy_bid(str(auction.id), self.bob, Decimal("20.50"))
        self.assertIn("at least $21.00", str(ctx.exception))

        # Bob bids $30 -> Standing price becomes $30 + $1 = $31
        execute_proxy_bid(str(auction.id), self.bob, Decimal("30.00"))
        auction.refresh_from_db()
        self.assertEqual(auction.current_price, Decimal("31.00"))
        self.assertEqual(auction.highest_bidder, self.alice)

    def test_over_500_tier_requires_twenty_five_dollar_increment(self):
        """Items between $500 and $999.99 step by $25.00."""
        auction = Auction.objects.create(
            seller=self.seller,
            title="Vintage Guitar",
            starting_price=Decimal("500.00"),
            current_price=Decimal("500.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )
        # Alice bids $800, Bob bids $600
        execute_proxy_bid(str(auction.id), self.alice, Decimal("800.00"))
        execute_proxy_bid(str(auction.id), self.bob, Decimal("600.00"))

        auction.refresh_from_db()
        # Price must step over Bob's $600 by $25.00 -> $625.00
        self.assertEqual(auction.current_price, Decimal("625.00"))
        self.assertEqual(auction.highest_bidder, self.alice)

    def test_over_1000_tier_requires_fifty_dollar_increment(self):
        """Items between $1,000 and $4,999.99 step by $50.00."""
        auction = Auction.objects.create(
            seller=self.seller,
            title="Luxury Watch",
            starting_price=Decimal("1200.00"),
            current_price=Decimal("1200.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )
        # Alice bids $3000, Bob bids $1500
        execute_proxy_bid(str(auction.id), self.alice, Decimal("3000.00"))
        execute_proxy_bid(str(auction.id), self.bob, Decimal("1500.00"))

        auction.refresh_from_db()
        # Price must step over Bob's $1,500 by $50.00 -> $1,550.00
        self.assertEqual(auction.current_price, Decimal("1550.00"))
        self.assertEqual(auction.highest_bidder, self.alice)