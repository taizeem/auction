from decimal import Decimal
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.utils import timezone
from auctions.models import Auction, UserWallet, DepositHold, Order
from auctions.services import (
    execute_proxy_bid,
    close_auction,
    settle_order_payment,
    process_defaulted_order_and_second_chance,
    AuctionDomainException,
)

User = get_user_model()


class DepositHoldTests(TransactionTestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username="seller", password="pw")
        self.alice = User.objects.create_user(username="alice", password="pw")
        self.bob = User.objects.create_user(username="bob", password="pw")

        # Alice starts with $100 balance, Bob starts with $20 balance
        UserWallet.objects.create(user=self.alice, balance=Decimal("100.00"))
        UserWallet.objects.create(user=self.bob, balance=Decimal("20.00"))

        self.auction = Auction.objects.create(
            seller=self.seller,
            title="Vintage Leica M3",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=2),
            status=Auction.Status.ACTIVE,
        )

    def test_bid_rejected_if_wallet_balance_insufficient_for_ten_percent_hold(self):
        # Bob only has $20.00 available. 10% of $300.00 requires $30.00 hold -> Should be rejected
        with self.assertRaises(AuctionDomainException) as ctx:
            execute_proxy_bid(str(self.auction.id), self.bob, Decimal("300.00"))
        self.assertIn("Insufficient funds: A 10% security deposit hold", str(ctx.exception))

    def test_deposit_hold_acquired_on_valid_bid(self):
        # Alice bids $300.00 -> Requires $30.00 hold
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("300.00"))

        alice_wallet = UserWallet.objects.get(user=self.alice)
        self.assertEqual(alice_wallet.held_balance, Decimal("30.00"))
        self.assertEqual(alice_wallet.available_balance, Decimal("70.00"))

        hold = DepositHold.objects.get(user=self.alice, auction=self.auction)
        self.assertEqual(hold.amount, Decimal("30.00"))
        self.assertEqual(hold.status, DepositHold.Status.HELD)

    def test_outbid_bidder_hold_is_released(self):
        # Alice bids $150 (hold = $15)
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("150.00"))

        # Give Bob sufficient funds, Bob outbids Alice with $200 (hold = $20)
        bob_wallet = UserWallet.objects.get(user=self.bob)
        bob_wallet.balance = Decimal("200.00")
        bob_wallet.save()

        execute_proxy_bid(str(self.auction.id), self.bob, Decimal("200.00"))

        # Alice's hold should be released back to her wallet
        alice_wallet = UserWallet.objects.get(user=self.alice)
        self.assertEqual(alice_wallet.held_balance, Decimal("0.00"))
        self.assertEqual(alice_wallet.available_balance, Decimal("100.00"))

        alice_hold = DepositHold.objects.get(user=self.alice, auction=self.auction)
        self.assertEqual(alice_hold.status, DepositHold.Status.RELEASED)

    def test_deposit_forfeited_when_winner_defaults(self):
        # Alice bids $300 and wins
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("300.00"))
        close_res = close_auction(str(self.auction.id))
        order_id = close_res["order_id"]

        # Alice defaults on payment
        process_defaulted_order_and_second_chance(order_id)

        alice_wallet = UserWallet.objects.get(user=self.alice)
        # 10% of $300 ($30) forfeited: balance becomes $70, held becomes $0
        self.assertEqual(alice_wallet.balance, Decimal("70.00"))
        self.assertEqual(alice_wallet.held_balance, Decimal("0.00"))

        hold = DepositHold.objects.get(user=self.alice, auction=self.auction)
        self.assertEqual(hold.status, DepositHold.Status.FORFEITED)

    def test_deposit_captured_on_successful_checkout(self):
        # Alice bids $300 and wins
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("300.00"))
        close_res = close_auction(str(self.auction.id))
        order_id = close_res["order_id"]

        # Alice pays for order
        settle_order_payment(order_id, self.alice, "txn_stripe_success_123")

        alice_wallet = UserWallet.objects.get(user=self.alice)
        self.assertEqual(alice_wallet.held_balance, Decimal("0.00"))
        self.assertEqual(alice_wallet.balance, Decimal("70.00"))

        hold = DepositHold.objects.get(user=self.alice, auction=self.auction)
        self.assertEqual(hold.status, DepositHold.Status.CAPTURED)