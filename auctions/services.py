from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from .models import Auction, Bid


class AuctionDomainException(Exception):
    """Domain exception raised when business rules are violated."""
    pass


@transaction.atomic
def execute_bid(auction_id: str, user, amount: Decimal) -> dict:
    """
    Acquires an exclusive row lock on the auction record via PostgreSQL SELECT ... FOR UPDATE.
    Guarantees sequential bid evaluation and prevents race conditions under high concurrency.
    """
    try:
        # Acquire an exclusive row-level lock on the auction record
        auction = Auction.objects.select_for_update().get(id=auction_id)
    except Auction.DoesNotExist:
        raise AuctionDomainException("Auction not found.")

    # 1. State validation
    if auction.status != Auction.Status.ACTIVE or auction.is_expired:
        if auction.status == Auction.Status.ACTIVE:
            auction.status = Auction.Status.CLOSED
            auction.save(update_fields=["status", "updated_at"])
        raise AuctionDomainException("This auction has expired and is no longer accepting bids.")

    # 2. Amount verification
    if amount <= auction.current_price:
        raise AuctionDomainException(
            f"Bid of ${amount:.2f} must strictly exceed current price of ${auction.current_price:.2f}."
        )

    # 3. Prevent self-outbidding
    if auction.highest_bidder == user:
        raise AuctionDomainException("You are already the highest bidder on this item.")

    # 4. Create the bid log
    bid = Bid.objects.create(
        auction=auction,
        bidder=user,
        amount=amount,
    )

    # 5. Advance auction price and set highest bidder
    auction.current_price = amount
    auction.highest_bidder = user
    auction.save(update_fields=["current_price", "highest_bidder", "updated_at"])

    return {
        "auction_id": str(auction.id),
        "amount": str(auction.current_price),
        "highest_bidder": user.username,
        "bid_id": str(bid.id),
        "timestamp": bid.created_at.isoformat(),
    }


@transaction.atomic
def close_auction(auction_id: str) -> dict:
    """Closes an expired auction and records the winning bidder atomically."""
    try:
        auction = Auction.objects.select_for_update().get(id=auction_id)
    except Auction.DoesNotExist:
        raise AuctionDomainException("Auction not found.")

    if auction.status != Auction.Status.ACTIVE:
        return {"auction_id": str(auction.id), "status": auction.status, "already_closed": True}

    auction.status = Auction.Status.CLOSED
    auction.save(update_fields=["status", "updated_at"])

    return {
        "auction_id": str(auction.id),
        "status": auction.status,
        "winner": auction.highest_bidder.username if auction.highest_bidder else None,
        "final_price": str(auction.current_price),
        "already_closed": False,
    }