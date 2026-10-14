# auctions/tests/test_ledger.py
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TransactionTestCase
from django.utils import timezone
from datetime import timedelta

from auctions.models import (
    Auction,
    Order,
    UserWallet,
    LedgerAccount,
    LedgerTransaction,
    LedgerEntry,
)
from auctions.services import (
    AuctionDomainException,
    record_double_entry_transaction,
    record_order_settlement_ledger,
    record_seller_payout_ledger,
    record_deposit_forfeiture_ledger,
    get_or_create_system_accounts,
    get_or_create_seller_payable_account,
)

User = get_user_model()


class DoubleEntryLedgerTests(TransactionTestCase):

    def setUp(self):
        self.seller = User.objects.create_user(username="ledger_seller", password="pw")
        self.buyer = User.objects.create_user(username="ledger_buyer", password="pw")

        UserWallet.objects.create(user=self.seller, balance=Decimal("1000.00"))
        UserWallet.objects.create(user=self.buyer, balance=Decimal("1000.00"))

        self.auction = Auction.objects.create(
            seller=self.seller,
            title="Vintage Submariner",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=2),
            status=Auction.Status.ACTIVE,
        )

    def test_imbalanced_transaction_is_rejected(self):
        clearing_cash, _, commission_rev, _ = get_or_create_system_accounts()

        imbalanced_entries = [
            {
                "account": clearing_cash,
                "entry_type": LedgerEntry.EntryType.DEBIT,
                "amount": Decimal("100.00"),
            },
            {
                "account": commission_rev,
                "entry_type": LedgerEntry.EntryType.CREDIT,
                "amount": Decimal("50.00"),  # Only $50.00 credit -> fails balance check
            },
        ]

        with self.assertRaises(AuctionDomainException) as ctx:
            record_double_entry_transaction(
                transaction_type=LedgerTransaction.TransactionType.ORDER_SETTLEMENT,
                reference_id="test_imbalance",
                description="Testing imbalance detection",
                entry_specs=imbalanced_entries,
            )
        self.assertIn("Ledger imbalance detected", str(ctx.exception))

    def test_order_settlement_records_five_percent_fee_and_seller_payable(self):
        order = Order.objects.create(
            auction=self.auction,
            buyer=self.buyer,
            amount=Decimal("200.00"),
            status=Order.Status.PAID,
            payment_due_at=timezone.now() + timedelta(hours=24),
        )

        txn = record_order_settlement_ledger(order)
        self.assertEqual(txn.entries.count(), 3)

        clearing_cash, _, commission_rev, _ = get_or_create_system_accounts()
        seller_account = get_or_create_seller_payable_account(self.seller)

        # 5% of $200 = $10.00 commission, $190.00 payable to seller
        self.assertEqual(clearing_cash.current_balance, Decimal("200.00"))
        self.assertEqual(commission_rev.current_balance, Decimal("10.00"))
        self.assertEqual(seller_account.current_balance, Decimal("190.00"))

    def test_seller_payout_deducts_payable_and_clearing_cash(self):
        order = Order.objects.create(
            auction=self.auction,
            buyer=self.buyer,
            amount=Decimal("100.00"),
            status=Order.Status.PAID,
            payment_due_at=timezone.now() + timedelta(hours=24),
        )
        record_order_settlement_ledger(order)

        seller_account = get_or_create_seller_payable_account(self.seller)
        clearing_cash, _, _, _ = get_or_create_system_accounts()

        # Seller has $95 net payable
        self.assertEqual(seller_account.current_balance, Decimal("95.00"))

        # Disburse full $95 payout
        record_seller_payout_ledger(self.seller, Decimal("95.00"), payout_ref="stripe_po_123")

        # Seller payable reduces to zero; clearing cash reduces by $95 (leaving $5 platform commission)
        self.assertEqual(seller_account.current_balance, Decimal("0.00"))
        self.assertEqual(clearing_cash.current_balance, Decimal("5.00"))

    def test_seller_payout_rejected_when_amount_exceeds_payable_balance(self):
        seller_account = get_or_create_seller_payable_account(self.seller)
        self.assertEqual(seller_account.current_balance, Decimal("0.00"))

        with self.assertRaises(AuctionDomainException) as ctx:
            record_seller_payout_ledger(self.seller, Decimal("50.00"), payout_ref="po_fail")
        self.assertIn("Insufficient seller balance", str(ctx.exception))

    def test_deposit_forfeiture_transfers_liability_to_revenue(self):
        record_deposit_forfeiture_ledger(self.buyer, self.auction, Decimal("30.00"))

        _, user_wallets, _, forfeiture_rev = get_or_create_system_accounts()
        self.assertEqual(forfeiture_rev.current_balance, Decimal("30.00"))
        self.assertEqual(user_wallets.current_balance, Decimal("-30.00"))

    def test_ledger_entries_and_transactions_are_immutable(self):
        order = Order.objects.create(
            auction=self.auction,
            buyer=self.buyer,
            amount=Decimal("100.00"),
            status=Order.Status.PAID,
            payment_due_at=timezone.now() + timedelta(hours=24),
        )
        txn = record_order_settlement_ledger(order)
        entry = txn.entries.first()

        # Attempted UPDATE must fail
        with self.assertRaises(ValidationError):
            entry.amount = Decimal("999.00")
            entry.save()

        with self.assertRaises(ValidationError):
            txn.description = "Tampered description"
            txn.save()

        # Attempted DELETE must fail
        with self.assertRaises(ValidationError):
            entry.delete()

        with self.assertRaises(ValidationError):
            txn.delete()