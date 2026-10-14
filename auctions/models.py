import uuid
from decimal import Decimal
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.core.exceptions import ValidationError


class Auction(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        CLOSED = "CLOSED", "Closed"
        UNSOLD = "UNSOLD", "Unsold"
        CANCELLED = "CANCELLED", "Cancelled"  

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="auctions",
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    starting_price = models.DecimalField(max_digits=12, decimal_places=2)
    current_price = models.DecimalField(max_digits=12, decimal_places=2)
    
    reserve_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    buy_it_now_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    highest_bidder = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="highest_bids",
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.ACTIVE
    )
    ends_at = models.DateTimeField()
    ending_soon_notified = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "ends_at"]),
            models.Index(fields=["seller", "status"]),
        ]

    def __str__(self):
        return f"{self.title} (${self.current_price})"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.ends_at

    @property
    def has_reserve(self) -> bool:
        return self.reserve_price is not None

    @property
    def is_reserve_met(self) -> bool:
        if not self.reserve_price:
            return True
        return self.current_price >= self.reserve_price
    @property
    def next_minimum_bid(self) -> Decimal:
        """Computes the minimum next allowable bid for competitors."""
        from .services import get_minimum_bid_increment

        if not self.bids.exists():
            return self.starting_price
        return self.current_price + get_minimum_bid_increment(self.current_price)


class Bid(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    auction = models.ForeignKey(
        Auction, on_delete=models.CASCADE, related_name="bids"
    )
    bidder = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bids"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["auction", "-created_at"]),
        ]

    def __str__(self):
        return f"${self.amount} on {self.auction.title} by {self.bidder.username}"


class ProxyBid(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    auction = models.ForeignKey(
        Auction, on_delete=models.CASCADE, related_name="proxy_bids"
    )
    bidder = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="proxy_bids"
    )
    max_amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["auction", "bidder"], name="unique_auction_bidder_proxy"
            )
        ]
        indexes = [
            models.Index(fields=["auction", "max_amount"]),
        ]

    def __str__(self):
        return f"{self.bidder.username} - {self.auction.title}: ${self.max_amount}"




class Order(models.Model):
    class Status(models.TextChoices):
        PENDING_PAYMENT = "PENDING", "Pending Payment"
        PAID = "PAID", "Paid"
        EXPIRED = "EXPIRED", "Expired"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    auction = models.ForeignKey(
        Auction, on_delete=models.CASCADE, related_name="orders"
    )
    buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="orders"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING_PAYMENT
    )
    payment_due_at = models.DateTimeField()
    paid_at = models.DateTimeField(null=True, blank=True)
    transaction_id = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "payment_due_at"]),
            models.Index(fields=["buyer", "status"]),
        ]

    def __str__(self):
        return f"Order {self.id} - {self.auction.title} (${self.amount})"

    @property
    def is_payment_overdue(self) -> bool:
        return timezone.now() > self.payment_due_at


class SecondChanceOffer(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACCEPTED = "ACCEPTED", "Accepted"
        DECLINED = "DECLINED", "Declined"
        EXPIRED = "EXPIRED", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    auction = models.ForeignKey(
        Auction, on_delete=models.CASCADE, related_name="second_chance_offers"
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="second_chance_offers"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["recipient", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["auction", "recipient"],
                name="unique_auction_recipient_offer",
            )
        ]

    def __str__(self):
        return f"Offer {self.id} - {self.recipient.username} on {self.auction.title}: ${self.amount}"

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at


class UserWallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet"
    )
    balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    held_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def available_balance(self) -> Decimal:
        return self.balance - self.held_balance

    def __str__(self):
        return f"{self.user.username}'s Wallet (${self.available_balance} available)"


class DepositHold(models.Model):
    class Status(models.TextChoices):
        HELD = "HELD", "Held"
        RELEASED = "RELEASED", "Released"
        CAPTURED = "CAPTURED", "Captured"
        FORFEITED = "FORFEITED", "Forfeited"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="deposit_holds"
    )
    auction = models.ForeignKey(
        Auction, on_delete=models.CASCADE, related_name="deposit_holds"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.HELD
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "auction", "status"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"Hold ${self.amount} for {self.user.username} on {self.auction.title} [{self.status}]"


class LedgerAccount(models.Model):
    class AccountType(models.TextChoices):
        ASSET = "ASSET", "Asset"
        LIABILITY = "LIABILITY", "Liability"
        REVENUE = "REVENUE", "Revenue"
        EXPENSE = "EXPENSE", "Expense"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    account_type = models.CharField(max_length=16, choices=AccountType.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_accounts",
        help_text="Optional link to a specific user (e.g. for user-specific seller payable accounts).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        user_suffix = f" [{self.user.username}]" if self.user else ""
        return f"{self.code} - {self.name}{user_suffix}"

    @property
    def current_balance(self) -> Decimal:
        """Computes current balance from immutable entries."""
        totals = self.entries.aggregate(
            debits=models.Sum("amount", filter=models.Q(entry_type=LedgerEntry.EntryType.DEBIT)),
            credits=models.Sum("amount", filter=models.Q(entry_type=LedgerEntry.EntryType.CREDIT)),
        )
        debits = totals["debits"] or Decimal("0.00")
        credits = totals["credits"] or Decimal("0.00")

        if self.account_type in (self.AccountType.ASSET, self.AccountType.EXPENSE):
            return debits - credits
        else:
            return credits - debits


class LedgerTransaction(models.Model):
    class TransactionType(models.TextChoices):
        ORDER_SETTLEMENT = "ORDER_SETTLEMENT", "Order Settlement"
        SELLER_PAYOUT = "SELLER_PAYOUT", "Seller Payout"
        DEPOSIT_FORFEITURE = "DEPOSIT_FORFEITURE", "Deposit Forfeiture"
        REFUND = "REFUND", "Refund"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transaction_type = models.CharField(max_length=32, choices=TransactionType.choices)
    reference_id = models.CharField(max_length=128, db_index=True)
    description = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Txn {self.id} [{self.transaction_type}] - Ref: {self.reference_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding and LedgerTransaction.objects.filter(pk=self.pk).exists():
            raise ValidationError("LedgerTransaction records are strictly immutable and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("LedgerTransaction records cannot be deleted.")


class LedgerEntry(models.Model):
    class EntryType(models.TextChoices):
        DEBIT = "DEBIT", "Debit"
        CREDIT = "CREDIT", "Credit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transaction = models.ForeignKey(
        LedgerTransaction, on_delete=models.PROTECT, related_name="entries"
    )
    account = models.ForeignKey(
        LedgerAccount, on_delete=models.PROTECT, related_name="entries"
    )
    entry_type = models.CharField(max_length=8, choices=EntryType.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "entry_type"]),
            models.Index(fields=["transaction", "created_at"]),
        ]

    def __str__(self):
        return f"{self.entry_type} ${self.amount} -> {self.account.code}"

    def clean(self):
        if self.amount <= Decimal("0.00"):
            raise ValidationError("LedgerEntry amount must be strictly positive.")

    def save(self, *args, **kwargs):
        if not self._state.adding and LedgerEntry.objects.filter(pk=self.pk).exists():
            raise ValidationError("LedgerEntry records are strictly immutable and cannot be modified.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("LedgerEntry records cannot be deleted.")


class Watchlist(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="watchlist_items",
    )
    auction = models.ForeignKey(
        "Auction",
        on_delete=models.CASCADE,
        related_name="watchers",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "auction"],
                name="unique_user_auction_watch",
            )
        ]
        indexes = [
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self):
        return f"{self.user.username} watching {self.auction.title}"