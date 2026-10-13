# In auctions/tests/test_tasks.py
from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.utils import timezone

from auctions.models import (
    Auction,
    DepositHold,
    Order,
    SecondChanceOffer,
    UserWallet,
)
from auctions.services import execute_proxy_bid, close_auction
from auctions.tasks import (
    close_expired_auctions_task,
    process_overdue_orders_task,
    expire_second_chance_offers_task,
)

User = get_user_model()


class CeleryAuctionTasksTests(TransactionTestCase):

    def setUp(self):
        self.seller = User.objects.create_user(username="task_seller", password="pw")
        self.alice = User.objects.create_user(username="task_alice", password="pw")
        self.bob = User.objects.create_user(username="task_bob", password="pw")

        for u in (self.seller, self.alice, self.bob):
            w, _ = UserWallet.objects.get_or_create(user=u)
            w.balance = Decimal("10000.00")
            w.held_balance = Decimal("0.00")
            w.save()

    def test_close_expired_auctions_task_closes_expired_and_leaves_active(self):
        # 1. Create auction with future ends_at so bid succeeds
        past_auction = Auction.objects.create(
            seller=self.seller,
            title="Expired Collector Item",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )
        execute_proxy_bid(str(past_auction.id), self.alice, Decimal("200.00"))

        # Backdate ends_at to past so it qualifies for the expiration task
        past_auction.ends_at = timezone.now() - timedelta(minutes=5)
        past_auction.save(update_fields=["ends_at"])

        # 2. Auction still active for 2 hours
        active_auction = Auction.objects.create(
            seller=self.seller,
            title="Still Active Item",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=2),
            status=Auction.Status.ACTIVE,
        )
        execute_proxy_bid(str(active_auction.id), self.bob, Decimal("150.00"))

        results = close_expired_auctions_task()
        self.assertEqual(len(results), 1)

        past_auction.refresh_from_db()
        active_auction.refresh_from_db()

        self.assertEqual(past_auction.status, Auction.Status.CLOSED)
        order = Order.objects.filter(auction=past_auction).first()
        self.assertIsNotNone(order)
        self.assertEqual(order.buyer, self.alice)

        self.assertEqual(active_auction.status, Auction.Status.ACTIVE)

    def test_close_expired_auctions_task_marks_unsold_when_reserve_not_met(self):
        auction = Auction.objects.create(
            seller=self.seller,
            title="High Reserve Item",
            starting_price=Decimal("50.00"),
            current_price=Decimal("50.00"),
            reserve_price=Decimal("500.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )
        execute_proxy_bid(str(auction.id), self.alice, Decimal("100.00"))

        # Backdate to trigger expiration
        auction.ends_at = timezone.now() - timedelta(minutes=1)
        auction.save(update_fields=["ends_at"])

        results = close_expired_auctions_task()
        self.assertEqual(len(results), 1)

        auction.refresh_from_db()
        self.assertEqual(auction.status, Auction.Status.UNSOLD)
        self.assertFalse(Order.objects.filter(auction=auction).exists())

    def test_process_overdue_orders_task_forfeits_deposit_and_offers_runner_up(self):
        auction = Auction.objects.create(
            seller=self.seller,
            title="Defaulted Item",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )
        execute_proxy_bid(str(auction.id), self.alice, Decimal("300.00"))
        execute_proxy_bid(str(auction.id), self.bob, Decimal("250.00"))

        close_res = close_auction(str(auction.id))
        order = Order.objects.get(id=close_res["order_id"])

        # Backdate payment_due_at to simulate overdue payment
        order.payment_due_at = timezone.now() - timedelta(hours=1)
        order.save(update_fields=["payment_due_at"])

        results = process_overdue_orders_task()
        self.assertEqual(len(results), 1)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.EXPIRED)

        alice_hold = DepositHold.objects.get(user=self.alice, auction=auction)
        self.assertEqual(alice_hold.status, DepositHold.Status.FORFEITED)

        offer = SecondChanceOffer.objects.get(auction=auction, recipient=self.bob)
        self.assertEqual(offer.status, SecondChanceOffer.Status.PENDING)
        self.assertEqual(offer.amount, Decimal("250.00"))

    def test_process_overdue_orders_task_ignores_valid_non_overdue_orders(self):
        auction = Auction.objects.create(
            seller=self.seller,
            title="Pending Payment Item",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )
        execute_proxy_bid(str(auction.id), self.alice, Decimal("200.00"))
        close_res = close_auction(str(auction.id))
        order = Order.objects.get(id=close_res["order_id"])

        results = process_overdue_orders_task()
        self.assertEqual(len(results), 0)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING_PAYMENT)

        alice_hold = DepositHold.objects.get(user=self.alice, auction=auction)
        self.assertEqual(alice_hold.status, DepositHold.Status.HELD)

    def test_expire_second_chance_offers_task_expires_stale_offers(self):
        auction = Auction.objects.create(
            seller=self.seller,
            title="Unsold Collectible",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            status=Auction.Status.CLOSED,
            ends_at=timezone.now() - timedelta(days=2),
        )
        stale_offer = SecondChanceOffer.objects.create(
            auction=auction,
            recipient=self.bob,
            amount=Decimal("150.00"),
            status=SecondChanceOffer.Status.PENDING,
            expires_at=timezone.now() - timedelta(minutes=10),
        )

        expired_count = expire_second_chance_offers_task()
        self.assertEqual(expired_count, 1)

        stale_offer.refresh_from_db()
        self.assertEqual(stale_offer.status, SecondChanceOffer.Status.EXPIRED)

        auction.refresh_from_db()
        self.assertEqual(auction.status, Auction.Status.UNSOLD)