from decimal import Decimal
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import Auction, Bid, ProxyBid, Order


class AuctionDomainException(Exception):
    """Domain exception raised when business rules are violated."""
    pass


ANTI_SNIPE_THRESHOLD_SECONDS = 120
AUTO_EXTEND_SECONDS = 120
PAYMENT_WINDOW_HOURS = 24


def get_minimum_increment(current_price: Decimal) -> Decimal:
    """Tiered minimum bid increment schedule."""
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
def execute_bid(auction_id: str, user, amount: Decimal) -> dict:
    try:
        auction = Auction.objects.select_for_update().get(id=auction_id)
    except Auction.DoesNotExist:
        raise AuctionDomainException("Auction not found.")

    now = timezone.now()

    if auction.status != Auction.Status.ACTIVE or auction.is_expired:
        if auction.status == Auction.Status.ACTIVE:
            auction.status = Auction.Status.CLOSED
            auction.save(update_fields=["status", "updated_at"])
        raise AuctionDomainException("This auction has expired and is no longer accepting bids.")

    if amount <= auction.current_price:
        raise AuctionDomainException(
            f"Bid of ${amount:.2f} must strictly exceed current price of ${auction.current_price:.2f}."
        )

    if auction.highest_bidder == user:
        raise AuctionDomainException("You are already the highest bidder on this item.")

    remaining_time = auction.ends_at - now
    extended = False
    update_fields = ["current_price", "highest_bidder", "updated_at"]

    if timedelta(seconds=0) < remaining_time <= timedelta(seconds=ANTI_SNIPE_THRESHOLD_SECONDS):
        auction.ends_at = now + timedelta(seconds=AUTO_EXTEND_SECONDS)
        update_fields.append("ends_at")
        extended = True

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
        "auction_title": auction.title,
        "amount": str(auction.current_price),
        "highest_bidder": user.username,
        "bid_id": str(bid.id),
        "ends_at": auction.ends_at.isoformat(),
        "extended": extended,
        "timestamp": bid.created_at.isoformat(),
    }


@transaction.atomic
def execute_proxy_bid(auction_id: str, user, max_amount: Decimal) -> dict:
    try:
        auction = Auction.objects.select_for_update().get(id=auction_id)
    except Auction.DoesNotExist:
        raise AuctionDomainException("Auction not found.")

    previous_leader = auction.highest_bidder
    now = timezone.now()

    if auction.status != Auction.Status.ACTIVE or auction.is_expired:
        if auction.status == Auction.Status.ACTIVE:
            auction.status = Auction.Status.CLOSED
            auction.save(update_fields=["status", "updated_at"])
        raise AuctionDomainException("This auction has expired and is no longer accepting bids.")

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

    active_proxies = list(
        ProxyBid.objects.select_for_update()
        .filter(auction=auction)
        .order_by("-max_amount", "created_at")
    )

    rank_1 = active_proxies[0]
    rank_2 = active_proxies[1] if len(active_proxies) > 1 else None

    if rank_2 is None:
        new_price = auction.starting_price
    elif rank_1.max_amount == rank_2.max_amount:
        new_price = rank_1.max_amount
    else:
        second_increment = get_minimum_increment(rank_2.max_amount)
        calculated_step = rank_2.max_amount + second_increment
        new_price = min(rank_1.max_amount, calculated_step)

    new_price = max(new_price, auction.current_price)

    remaining_time = auction.ends_at - now
    extended = False
    update_fields = ["current_price", "highest_bidder", "updated_at"]

    if timedelta(seconds=0) < remaining_time <= timedelta(seconds=ANTI_SNIPE_THRESHOLD_SECONDS):
        auction.ends_at = now + timedelta(seconds=AUTO_EXTEND_SECONDS)
        update_fields.append("ends_at")
        extended = True

    new_leader = rank_1.bidder
    displaced_user_id = None
    if previous_leader and previous_leader.id != new_leader.id:
        displaced_user_id = str(previous_leader.id)

    auction.current_price = new_price
    auction.highest_bidder = new_leader
    auction.save(update_fields=update_fields)

    public_bid = Bid.objects.create(
        auction=auction,
        bidder=new_leader,
        amount=new_price,
    )

    return {
        "auction_id": str(auction.id),
        "auction_title": auction.title,
        "amount": str(auction.current_price),
        "highest_bidder": new_leader.username,
        "is_caller_leading": (new_leader == user),
        "caller_max_amount": str(max_amount),
        "bid_id": str(public_bid.id),
        "ends_at": auction.ends_at.isoformat(),
        "extended": extended,
        "timestamp": public_bid.created_at.isoformat(),
        "displaced_user_id": displaced_user_id,
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

    # Reserve price verification
    if auction.reserve_price and auction.current_price < auction.reserve_price:
        auction.status = Auction.Status.UNSOLD
        auction.save(update_fields=["status", "updated_at"])
        return {
            "auction_id": str(auction.id),
            "status": auction.status,
            "winner": None,
            "final_price": str(auction.current_price),
            "reserve_met": False,
            "order_id": None,
            "already_closed": False,
        }

    auction.status = Auction.Status.CLOSED
    auction.save(update_fields=["status", "updated_at"])

    # Instantiate order for winner
    order_id = None
    if auction.highest_bidder:
        order, _ = Order.objects.get_or_create(
            auction=auction,
            defaults={
                "buyer": auction.highest_bidder,
                "amount": auction.current_price,
                "payment_due_at": timezone.now() + timedelta(hours=PAYMENT_WINDOW_HOURS),
                "status": Order.Status.PENDING_PAYMENT,
            },
        )
        order_id = str(order.id)

    return {
        "auction_id": str(auction.id),
        "status": auction.status,
        "winner": auction.highest_bidder.username if auction.highest_bidder else None,
        "final_price": str(auction.current_price),
        "reserve_met": True,
        "order_id": order_id,
        "already_closed": False,
    }


@transaction.atomic
def execute_buy_it_now(auction_id: str, user) -> dict:
    try:
        auction = Auction.objects.select_for_update().get(id=auction_id)
    except Auction.DoesNotExist:
        raise AuctionDomainException("Auction not found.")

    if auction.status != Auction.Status.ACTIVE or auction.is_expired:
        raise AuctionDomainException("This auction is not open for instant purchase.")

    if not auction.buy_it_now_price:
        raise AuctionDomainException("Buy It Now is not available for this auction.")

    bid = Bid.objects.create(
        auction=auction,
        bidder=user,
        amount=auction.buy_it_now_price,
    )

    auction.current_price = auction.buy_it_now_price
    auction.highest_bidder = user
    auction.status = Auction.Status.CLOSED
    auction.save(update_fields=["current_price", "highest_bidder", "status", "updated_at"])

    # Instantiate Order for buyer
    order, _ = Order.objects.get_or_create(
        auction=auction,
        defaults={
            "buyer": user,
            "amount": auction.buy_it_now_price,
            "payment_due_at": timezone.now() + timedelta(hours=PAYMENT_WINDOW_HOURS),
            "status": Order.Status.PENDING_PAYMENT,
        },
    )

    return {
        "auction_id": str(auction.id),
        "auction_title": auction.title,
        "final_price": str(auction.current_price),
        "winner": user.username,
        "bid_id": str(bid.id),
        "order_id": str(order.id),
        "status": auction.status,
    }


def settle_order_payment(order_id: str, user, transaction_id: str) -> dict:
    """
    Settles an order atomically. Validates ownership, deadline, and payment status.
    If the order is overdue, commits the EXPIRED status before raising the domain error.
    """
    is_overdue = False

    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().get(id=order_id)
        except Order.DoesNotExist:
            raise AuctionDomainException("Order not found.")

        if order.buyer != user:
            raise AuctionDomainException("You do not have permission to pay for this order.")

        if order.status == Order.Status.PAID:
            raise AuctionDomainException("This order has already been paid.")

        if order.status != Order.Status.PENDING_PAYMENT:
            raise AuctionDomainException(f"Cannot settle order in '{order.status}' status.")

        if order.is_payment_overdue:
            order.status = Order.Status.EXPIRED
            order.save(update_fields=["status", "updated_at"])
            is_overdue = True
        else:
            now = timezone.now()
            order.status = Order.Status.PAID
            order.paid_at = now
            order.transaction_id = transaction_id
            order.save(update_fields=["status", "paid_at", "transaction_id", "updated_at"])

    # Raise after the atomic transaction has safely committed the EXPIRED state
    if is_overdue:
        raise AuctionDomainException("Payment window has expired for this order.")

    return {
        "order_id": str(order.id),
        "auction_id": str(order.auction_id),
        "amount": str(order.amount),
        "status": order.status,
        "transaction_id": order.transaction_id,
        "paid_at": order.paid_at.isoformat(),
    }