import uuid
from decimal import Decimal
from django.conf import settings
from django.db import models
from django.utils import timezone


class Auction(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        CLOSED = "CLOSED", "Closed"
        UNSOLD = "UNSOLD", "Unsold"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    starting_price = models.DecimalField(max_digits=12, decimal_places=2)
    current_price = models.DecimalField(max_digits=12, decimal_places=2)
    
    # Secret reserve price and instant purchase price
    reserve_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Secret minimum price required for a valid sale.",
    )
    buy_it_now_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Instant checkout price.",
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "ends_at"]),
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