from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from auctions.models import Auction, Bid, Order, UserWallet

User = get_user_model()


class AuctionModelTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="model_tester", password="pw")

    def test_auction_is_expired_property(self):
        active_auction = Auction.objects.create(
            title="Active Item",
            starting_price=Decimal("50.00"),
            current_price=Decimal("50.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )
        expired_auction = Auction.objects.create(
            title="Past Item",
            starting_price=Decimal("50.00"),
            current_price=Decimal("50.00"),
            ends_at=timezone.now() - timedelta(minutes=10),
            status=Auction.Status.ACTIVE,
        )
        self.assertFalse(active_auction.is_expired)
        self.assertTrue(expired_auction.is_expired)

    def test_order_is_payment_overdue_property(self):
        """Order.is_payment_overdue returns True if payment_due_at has passed."""
        auction = Auction.objects.create(
            title="Sample Listing",
            starting_price=Decimal("10.00"),
            current_price=Decimal("10.00"),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        order = Order.objects.create(
            auction=auction,
            buyer=self.user,
            amount=Decimal("10.00"),
            status=Order.Status.PENDING_PAYMENT,
            payment_due_at=timezone.now() + timedelta(hours=24),
        )
        self.assertFalse(order.is_payment_overdue)

        # Backdate payment_due_at to simulate overdue status
        Order.objects.filter(id=order.id).update(
            payment_due_at=timezone.now() - timedelta(minutes=5)
        )
        order.refresh_from_db()
        self.assertTrue(order.is_payment_overdue)

    def test_user_wallet_available_balance_property(self):
        wallet = UserWallet.objects.create(
            user=self.user,
            balance=Decimal("250.00"),
            held_balance=Decimal("50.00"),
        )
        self.assertEqual(wallet.available_balance, Decimal("200.00"))

    def test_auction_string_representation(self):
        auction = Auction.objects.create(
            title="Vintage Rolex",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        self.assertIn("Vintage Rolex", str(auction))

    def test_bid_string_representation(self):
        auction = Auction.objects.create(
            title="Vintage Omega",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        bid = Bid.objects.create(
            auction=auction,
            bidder=self.user,
            amount=Decimal("100.00"),
        )
        self.assertIn("100.00", str(bid))