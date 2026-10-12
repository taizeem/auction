from decimal import Decimal
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import Auction, Bid, ProxyBid


class AuctionDomainException(Exception):
    pass


ANTI_SNIPE_THRESHOLD_SECONDS = 120
AUTO_EXTEND_SECONDS = 120


def get_minimum_increment(current_price: Decimal) -> Decimal:
    if current_price < Decimal("50.00"):
        return Decimal("1.00")
    elif current_price < Decimal("250.00"):
        return Decimal("5.00")
    elif current_price < Decimal("1000.00"):
        return Decimal("10.00")
    elif current_price < Decimal("5000.00"):
        return Decimal("25.00")
    return Decimal("50.00")


@transaction.atomic
def execute_proxy_bid(auction_id: str, user, max_amount: Decimal) -> dict:
    """
    Evaluates proxy maximum budgets atomically with pessimistic locks.
    Resolves top 2 budgets, calculates required public price, and records public bids.
    """
    try:
        auction = Auction.objects.select_for_update().get(id=auction_id)
    except Auction.DoesNotExist:
        raise AuctionDomainException("Auction not found.")

    now = timezone.now()

    # 1. State check
    if auction.status != Auction.Status.ACTIVE or auction.is_expired:
        if auction.status == Auction.Status.ACTIVE:
            auction.status = Auction.Status.CLOSED
            auction.save(update_fields=["status", "updated_at"])
        raise AuctionDomainException("This auction has expired and is no longer accepting bids.")

    # 2. Minimum entry validation
    increment = get_minimum_increment(auction.current_price)
    has_existing_bids = Bid.objects.filter(auction=auction).exists()

    if not has_existing_bids:
        min_required = auction.starting_price
    else:
        min_required = auction.current_price + increment

    if max_amount < min_required:
        raise AuctionDomainException(
            f"Your maximum bid must be at least ${min_required:.2f}."
        )

    # 3. Lock or create user's proxy budget
    user_proxy = (
        ProxyBid.objects.select_for_update()
        .filter(auction=auction, bidder=user)
        .first()
    )

    if user_proxy:
        if max_amount <= user_proxy.max_amount:
            raise AuctionDomainException(
                f"Your new budget (${max_amount:.2f}) must strictly exceed your current budget (${user_proxy.max_amount:.2f})."
            )
        user_proxy.max_amount = max_amount
        user_proxy.save(update_fields=["max_amount", "updated_at"])
    else:
        user_proxy = ProxyBid.objects.create(
            auction=auction,
            bidder=user,
            max_amount=max_amount,
        )

    # 4. Fetch and lock all competing proxy bids for evaluation
    active_proxies = list(
        ProxyBid.objects.select_for_update()
        .filter(auction=auction)
        .order_by("-max_amount", "created_at")
    )

    rank_1 = active_proxies[0]
    rank_2 = active_proxies[1] if len(active_proxies) > 1 else None

    # 5. Resolve new public price
    if rank_2 is None:
        # Sole bidder gets starting price
        new_price = auction.starting_price
    elif rank_1.max_amount == rank_2.max_amount:
        # Tie: Earliest bidder wins at the tied ceiling
        new_price = rank_1.max_amount
    else:
        # Rank 1 beats Rank 2 by the minimum step
        second_increment = get_minimum_increment(rank_2.max_amount)
        calculated_step = rank_2.max_amount + second_increment
        new_price = min(rank_1.max_amount, calculated_step)

    # Ensure price strictly advances or matches start price
    new_price = max(new_price, auction.current_price)

    # 6. Anti-sniping check
    remaining_time = auction.ends_at - now
    extended = False
    update_fields = ["current_price", "highest_bidder", "updated_at"]

    if timedelta(seconds=0) < remaining_time <= timedelta(seconds=ANTI_SNIPE_THRESHOLD_SECONDS):
        auction.ends_at = now + timedelta(seconds=AUTO_EXTEND_SECONDS)
        update_fields.append("ends_at")
        extended = True

    # 7. Persist public bid and auction changes
    auction.current_price = new_price
    auction.highest_bidder = rank_1.bidder
    auction.save(update_fields=update_fields)

    # Record public bid entry
    public_bid = Bid.objects.create(
        auction=auction,
        bidder=rank_1.bidder,
        amount=new_price,
    )

    return {
        "auction_id": str(auction.id),
        "amount": str(auction.current_price),
        "highest_bidder": rank_1.bidder.username,
        "is_caller_leading": (rank_1.bidder == user),
        "caller_max_amount": str(max_amount),
        "bid_id": str(public_bid.id),
        "ends_at": auction.ends_at.isoformat(),
        "extended": extended,
        "timestamp": public_bid.created_at.isoformat(),
    }


@transaction.atomic
def close_auction(auction_id: str) -> dict:
    try:
        auction = Auction.objects.select_for_update().get(id=auction_id)
    except Auction.DoesNotExist:
        raise AuctionDomainException("Auction not found.")

    if auction.status != Auction.Status.ACTIVE:
        return {
            "auction_id": str(auction.id),
            "status": auction.status,
            "already_closed": True,
        }

    auction.status = Auction.Status.CLOSED
    auction.save(update_fields=["status", "updated_at"])

    return {
        "auction_id": str(auction.id),
        "status": auction.status,
        "winner": auction.highest_bidder.username if auction.highest_bidder else None,
        "final_price": str(auction.current_price),
        "already_closed": False,
    }