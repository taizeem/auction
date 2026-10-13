from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from .models import Auction, Bid, ProxyBid, Order, SecondChanceOffer, UserWallet, DepositHold

DEPOSIT_HOLD_PERCENTAGE = Decimal("0.10")
def calculate_required_deposit(amount: Decimal) -> Decimal:
    """Calculates 10% hold rounded to nearest cent."""
    return (amount * DEPOSIT_HOLD_PERCENTAGE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

class AuctionDomainException(Exception):
    """Domain exception raised when business rules are violated."""
    pass


ANTI_SNIPE_THRESHOLD_SECONDS = 120
AUTO_EXTEND_SECONDS = 120
PAYMENT_WINDOW_HOURS = 24
SECOND_CHANCE_WINDOW_HOURS = 24


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

    if auction.seller_id and auction.seller_id == user.id:
        raise AuctionDomainException("Sellers are not permitted to bid on their own auctions.")

    now = timezone.now()
    if auction.status != Auction.Status.ACTIVE or auction.is_expired:
        if auction.status == Auction.Status.ACTIVE:
            auction.status = Auction.Status.CLOSED
            auction.save(update_fields=["status", "updated_at"])
        raise AuctionDomainException("This auction has expired and is no longer accepting bids.")

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

    
    if auction.seller_id and auction.seller_id == user.id:
        raise AuctionDomainException("Sellers are not permitted to bid on their own auctions.")

    #Acquire or adjust 10% security deposit hold before processing bid
    acquire_or_adjust_deposit_hold(user, auction, max_amount)

    previous_leader = auction.highest_bidder
    now = timezone.now()

    previous_leader = auction.highest_bidder
    now = timezone.now()
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

    if previous_leader and auction.highest_bidder != previous_leader:
        # Check if the previous leader's proxy budget was completely exceeded
        prev_proxy = ProxyBid.objects.filter(auction=auction, bidder=previous_leader).first()
        if prev_proxy and prev_proxy.max_amount <= auction.current_price:
            release_deposit_hold(previous_leader, auction)
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

    if auction.seller_id and auction.seller_id == user.id:
        raise AuctionDomainException("Sellers cannot buy their own listings.")

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
    capture_deposit_hold(order.buyer, order.auction)

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

@transaction.atomic
def process_defaulted_order_and_second_chance(order_id: str) -> dict:
    """
    Called when a winner fails to pay. Expires the original order and
    either generates a second-chance offer for the runner-up or marks the item UNSOLD.
    """
    try:
        order = Order.objects.select_for_update().get(id=order_id)
    except Order.DoesNotExist:
        raise AuctionDomainException("Order not found.")
    
    forfeit_deposit_hold(order.buyer, auction)

    if order.status != Order.Status.PENDING_PAYMENT:
        return {"order_id": str(order.id), "status": order.status, "action": "ignored"}

    auction = Auction.objects.select_for_update().get(id=order.auction_id)

    # 1. Mark original winner's order as EXPIRED
    order.status = Order.Status.EXPIRED
    order.save(update_fields=["status", "updated_at"])

    # 2. Query runner-up from ProxyBid table (excluding defaulted winner)
    runner_up_proxy = (
        ProxyBid.objects.select_for_update()
        .filter(auction=auction)
        .exclude(bidder=order.buyer)
        .order_by("-max_amount", "created_at")
        .first()
    )

    # 3. Check viability of second-chance offer
    can_offer = False
    if runner_up_proxy:
        # Offered at runner-up's maximum budget ceiling
        offered_amount = runner_up_proxy.max_amount
        if not auction.reserve_price or offered_amount >= auction.reserve_price:
            can_offer = True

    if not can_offer:
        # No viable runner-up -> Listing becomes UNSOLD
        auction.status = Auction.Status.UNSOLD
        auction.save(update_fields=["status", "updated_at"])
        return {
            "order_id": str(order.id),
            "auction_id": str(auction.id),
            "action": "marked_unsold",
            "offer_id": None,
        }

    # 4. Create SecondChanceOffer
    expires_at = timezone.now() + timedelta(hours=SECOND_CHANCE_WINDOW_HOURS)
    offer, _ = SecondChanceOffer.objects.get_or_create(
        auction=auction,
        recipient=runner_up_proxy.bidder,
        defaults={
            "amount": runner_up_proxy.max_amount,
            "status": SecondChanceOffer.Status.PENDING,
            "expires_at": expires_at,
        },
    )

    # 5. Broadcast targeted WebSocket notification to runner-up
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            f"user_{runner_up_proxy.bidder.id}",
            {
                "type": "second_chance_notification",
                "data": {
                    "offer_id": str(offer.id),
                    "auction_id": str(auction.id),
                    "auction_title": auction.title,
                    "amount": str(offer.amount),
                    "expires_at": offer.expires_at.isoformat(),
                    "message": f"Good news! The winner defaulted. You have a second-chance offer to buy '{auction.title}' for ${offer.amount}.",
                },
            },
        )

    return {
        "order_id": str(order.id),
        "auction_id": str(auction.id),
        "action": "offer_created",
        "offer_id": str(offer.id),
        "recipient": runner_up_proxy.bidder.username,
        "amount": str(offer.amount),
    }


@transaction.atomic
def respond_to_second_chance_offer(offer_id: str, user, accept: bool) -> dict:
    """
    Processes recipient acceptance or rejection of a second-chance offer.
    """
    try:
        offer = SecondChanceOffer.objects.select_for_update().get(id=offer_id)
    except SecondChanceOffer.DoesNotExist:
        raise AuctionDomainException("Second-chance offer not found.")

    if offer.recipient != user:
        raise AuctionDomainException("You do not have permission to respond to this offer.")

    if offer.status != SecondChanceOffer.Status.PENDING:
        raise AuctionDomainException(f"Offer is no longer pending (current status: {offer.status}).")

    if offer.is_expired:
        offer.status = SecondChanceOffer.Status.EXPIRED
        offer.save(update_fields=["status", "updated_at"])
        raise AuctionDomainException("This second-chance offer has expired.")

    auction = Auction.objects.select_for_update().get(id=offer.auction_id)

    if not accept:
        offer.status = SecondChanceOffer.Status.DECLINED
        offer.save(update_fields=["status", "updated_at"])
        auction.status = Auction.Status.UNSOLD
        auction.save(update_fields=["status", "updated_at"])
        return {
            "offer_id": str(offer.id),
            "status": offer.status,
            "order_id": None,
        }

    # Recipient accepts: Mark ACCEPTED, update auction winner, create new Order
    offer.status = SecondChanceOffer.Status.ACCEPTED
    offer.save(update_fields=["status", "updated_at"])

    auction.highest_bidder = user
    auction.current_price = offer.amount
    auction.status = Auction.Status.CLOSED
    auction.save(update_fields=["highest_bidder", "current_price", "status", "updated_at"])

    new_order = Order.objects.create(
        auction=auction,
        buyer=user,
        amount=offer.amount,
        payment_due_at=timezone.now() + timedelta(hours=PAYMENT_WINDOW_HOURS),
        status=Order.Status.PENDING_PAYMENT,
    )

    return {
        "offer_id": str(offer.id),
        "status": offer.status,
        "order_id": str(new_order.id),
        "amount": str(new_order.amount),
        "payment_due_at": new_order.payment_due_at.isoformat(),
    }
@transaction.atomic
def cancel_auction(auction_id: str, user) -> dict:
    """
    Cancels an auction atomically. Sellers may only cancel if zero bids have been placed.
    """
    try:
        auction = Auction.objects.select_for_update().get(id=auction_id)
    except Auction.DoesNotExist:
        raise AuctionDomainException("Auction not found.")

    if auction.seller and auction.seller != user and not user.is_staff:
        raise AuctionDomainException("You do not have permission to cancel this auction.")

    if auction.status != Auction.Status.ACTIVE:
        raise AuctionDomainException(f"Cannot cancel auction in '{auction.status}' status.")

    # Strictly reject cancellation if any bids exist
    if Bid.objects.filter(auction=auction).exists():
        raise AuctionDomainException(
            "Cannot cancel an auction after bids have already been placed."
        )

    auction.status = Auction.Status.CANCELLED
    auction.save(update_fields=["status", "updated_at"])

    return {
        "auction_id": str(auction.id),
        "title": auction.title,
        "status": auction.status,
    }

@transaction.atomic
def acquire_or_adjust_deposit_hold(user, auction: Auction, target_max_amount: Decimal) -> DepositHold:
    """
    Ensures user has an active deposit hold equal to 10% of their target proxy budget.
    Deducts delta from available wallet balance.
    """
    wallet, _ = UserWallet.objects.select_for_update().get_or_create(user=user)
    required_hold = calculate_required_deposit(target_max_amount)

    # Check for existing active hold for this user on this auction
    current_hold = (
        DepositHold.objects.select_for_update()
        .filter(user=user, auction=auction, status=DepositHold.Status.HELD)
        .first()
    )

    previous_hold_amount = current_hold.amount if current_hold else Decimal("0.00")
    delta = required_hold - previous_hold_amount

    if delta > Decimal("0.00"):
        if wallet.available_balance < delta:
            raise AuctionDomainException(
                f"Insufficient funds: A 10% security deposit hold of ${required_hold} "
                f"is required to place this bid. Available balance: ${wallet.available_balance}."
            )
        wallet.held_balance += delta
        wallet.save(update_fields=["held_balance", "updated_at"])

    if current_hold:
        current_hold.amount = required_hold
        current_hold.save(update_fields=["amount", "updated_at"])
        return current_hold
    else:
        return DepositHold.objects.create(
            user=user,
            auction=auction,
            amount=required_hold,
            status=DepositHold.Status.HELD,
        )


@transaction.atomic
def release_deposit_hold(user, auction: Auction) -> None:
    """
    Releases active deposit hold back to user's available wallet balance (e.g. when outbid).
    """
    hold = (
        DepositHold.objects.select_for_update()
        .filter(user=user, auction=auction, status=DepositHold.Status.HELD)
        .first()
    )
    if not hold:
        return

    wallet, _ = UserWallet.objects.select_for_update().get_or_create(user=user)
    wallet.held_balance = max(Decimal("0.00"), wallet.held_balance - hold.amount)
    wallet.save(update_fields=["held_balance", "updated_at"])

    hold.status = DepositHold.Status.RELEASED
    hold.save(update_fields=["status", "updated_at"])


@transaction.atomic
def forfeit_deposit_hold(user, auction: Auction) -> None:
    """
    Forfeits deposit hold as penalty when a winner defaults. Deducts directly from balance.
    """
    hold = (
        DepositHold.objects.select_for_update()
        .filter(user=user, auction=auction, status=DepositHold.Status.HELD)
        .first()
    )
    if not hold:
        return

    wallet, _ = UserWallet.objects.select_for_update().get_or_create(user=user)
    wallet.held_balance = max(Decimal("0.00"), wallet.held_balance - hold.amount)
    wallet.balance = max(Decimal("0.00"), wallet.balance - hold.amount)
    wallet.save(update_fields=["held_balance", "balance", "updated_at"])

    hold.status = DepositHold.Status.FORFEITED
    hold.save(update_fields=["status", "updated_at"])


@transaction.atomic
def capture_deposit_hold(user, auction: Auction) -> None:
    """
    Captures deposit hold upon successful checkout payment.
    """
    hold = (
        DepositHold.objects.select_for_update()
        .filter(user=user, auction=auction, status=DepositHold.Status.HELD)
        .first()
    )
    if not hold:
        return

    wallet, _ = UserWallet.objects.select_for_update().get_or_create(user=user)
    wallet.held_balance = max(Decimal("0.00"), wallet.held_balance - hold.amount)
    wallet.balance = max(Decimal("0.00"), wallet.balance - hold.amount)
    wallet.save(update_fields=["held_balance", "balance", "updated_at"])

    hold.status = DepositHold.Status.CAPTURED
    hold.save(update_fields=["status", "updated_at"])