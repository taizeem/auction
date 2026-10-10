from decimal import Decimal
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import Auction, Bid


class AuctionDomainException(Exception):
    pass


# Anti-sniping parameters (can also be pulled from settings/.env)
ANTI_SNIPE_THRESHOLD_SECONDS = 120  # Trigger if bid arrives in the final 2 minutes
AUTO_EXTEND_SECONDS = 120           # Reset clock to 2 minutes from now


@transaction.atomic
def execute_bid(auction_id: str, user, amount: Decimal) -> dict:
    try:
        auction = Auction.objects.select_for_update().get(id=auction_id)
    except Auction.DoesNotExist:
        raise AuctionDomainException("Auction not found.")

    now = timezone.now()

    # 1. State validation
    if auction.status != Auction.Status.ACTIVE or auction.is_expired:
        if auction.status == Auction.Status.ACTIVE:
            auction.status = Auction.Status.CLOSED
            auction.save(update_fields=["status", "updated_at"])
        raise AuctionDomainException("This auction has expired and is no longer accepting bids.")

    # 2. Price validation
    if amount <= auction.current_price:
        raise AuctionDomainException(
            f"Bid of ${amount:.2f} must strictly exceed current price of ${auction.current_price:.2f}."
        )

    # 3. Prevent self-outbidding
    if auction.highest_bidder == user:
        raise AuctionDomainException("You are already the highest bidder on this item.")

    # 4. Soft-Close / Auto-Extend Check
    # If the bid arrives within the threshold window, extend ends_at
    remaining_time = auction.ends_at - now
    extended = False
    update_fields = ["current_price", "highest_bidder", "updated_at"]

    if timedelta(seconds=0) < remaining_time <= timedelta(seconds=ANTI_SNIPE_THRESHOLD_SECONDS):
        auction.ends_at = now + timedelta(seconds=AUTO_EXTEND_SECONDS)
        update_fields.append("ends_at")
        extended = True

    # 5. Persist bid and updated auction state
    bid = Bid.objects.create(
        auction=auction,
        bidder=user,
        amount=amount,
    )

    auction.current_price = amount
    auction.highest_bidder = user
    auction.save(update_fields=update_fields)

    return {
        "auction_id": str(auction.id),
        "amount": str(auction.current_price),
        "highest_bidder": user.username,
        "bid_id": str(bid.id),
        "ends_at": auction.ends_at.isoformat(),
        "extended": extended,
        "timestamp": bid.created_at.isoformat(),
    }