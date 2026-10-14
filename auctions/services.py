from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from .models import Auction, Bid, ProxyBid, Order, SecondChanceOffer, UserWallet, DepositHold, LedgerAccount, LedgerEntry, LedgerTransaction

BID_INCREMENT_TIERS = [
    (Decimal("100.00"), Decimal("1.00")),
    (Decimal("500.00"), Decimal("5.00")),
    (Decimal("1000.00"), Decimal("25.00")),
    (Decimal("5000.00"), Decimal("50.00")),
    (Decimal("Infinity"), Decimal("100.00")),
]

def get_minimum_bid_increment(current_price: Decimal) -> Decimal:
    """Calculates the minimum required bid increment based on price brackets."""
    price = Decimal(str(current_price))
    if price < Decimal("100.00"):
        return Decimal("1.00")
    elif price < Decimal("500.00"):
        return Decimal("5.00")
    elif price < Decimal("1000.00"):
        return Decimal("25.00")
    elif price < Decimal("5000.00"):
        return Decimal("50.00")
    return Decimal("100.00")


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
PLATFORM_COMMISSION_RATE = Decimal("0.05")


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
def execute_proxy_bid(
    auction_id: str,
    user,
    max_amount: Decimal = None,
    amount: Decimal = None,
) -> dict:
    target_budget = max_amount if max_amount is not None else amount
    if target_budget is None:
        raise AuctionDomainException("A valid bid budget must be provided.")
    target_budget = Decimal(str(target_budget))

    try:
        auction = Auction.objects.select_for_update().get(id=auction_id)
    except Auction.DoesNotExist:
        raise AuctionDomainException("Auction not found.")

    if auction.seller_id and auction.seller_id == user.id:
        raise AuctionDomainException(
            "Sellers are not permitted to bid on their own auctions."
        )

    now = timezone.now()

    if auction.status != Auction.Status.ACTIVE or auction.is_expired:
        if auction.status == Auction.Status.ACTIVE:
            auction.status = Auction.Status.CLOSED
            auction.save(update_fields=["status", "updated_at"])
        raise AuctionDomainException(
            "This auction has expired and is no longer accepting bids."
        )

    # 1. Minimum required bid calculation using dynamic bracket increment
    current_highest_bid = (
        auction.bids.order_by("-amount", "-created_at").first()
    )
    if not current_highest_bid:
        min_bid = auction.starting_price
    elif auction.highest_bidder == user:
        min_bid = auction.current_price
    else:
        # Dynamic increment evaluated at the current standing price
        min_bid = auction.current_price + get_minimum_bid_increment(
            auction.current_price
        )

    # 2. Validate minimum increment BEFORE checking wallet funds
    if target_budget < min_bid:
        raise AuctionDomainException(f"Bid budget must be at least ${min_bid}.")

    # 3. Acquire or adjust 10% security deposit hold
    acquire_or_adjust_deposit_hold(user, auction, target_budget)

    # 4. Anti-sniping soft-close window extension
    extended = False
    if (auction.ends_at - now).total_seconds() <= 120:
        auction.ends_at = auction.ends_at + timedelta(seconds=120)
        extended = True

    previous_leader = auction.highest_bidder

    # 5. Create or update bidder's ProxyBid budget
    proxy_bid, _ = ProxyBid.objects.select_for_update().get_or_create(
        auction=auction,
        bidder=user,
        defaults={"max_amount": target_budget},
    )
    if proxy_bid.max_amount != target_budget:
        proxy_bid.max_amount = target_budget
        proxy_bid.save(update_fields=["max_amount", "updated_at"])

    # 6. Query top two competing proxy budgets
    proxies = list(
        ProxyBid.objects.select_for_update()
        .filter(auction=auction)
        .order_by("-max_amount", "created_at")[:2]
    )

    leading_proxy = proxies[0]
    runner_up_proxy = proxies[1] if len(proxies) > 1 else None

    # 7. Compute new public standing price using dynamic increment step
    if not runner_up_proxy:
        new_price = max(auction.starting_price, auction.current_price)
    else:
        if leading_proxy.max_amount == runner_up_proxy.max_amount:
            new_price = leading_proxy.max_amount
        else:
            # Dynamic step evaluated against the runner-up's price bracket
            step_increment = get_minimum_bid_increment(
                runner_up_proxy.max_amount
            )
            calculated_step = runner_up_proxy.max_amount + step_increment
            new_price = min(leading_proxy.max_amount, calculated_step)

    auction.current_price = new_price
    auction.highest_bidder = leading_proxy.bidder
    auction.save(
        update_fields=[
            "current_price",
            "highest_bidder",
            "ends_at",
            "updated_at",
        ]
    )

    # 8. Record the public bid entry
    bid = Bid.objects.create(
        auction=auction,
        bidder=auction.highest_bidder,
        amount=auction.current_price,
    )

    # 9. Displaced leader handling
    if previous_leader and auction.highest_bidder != previous_leader:
        prev_proxy = ProxyBid.objects.filter(
            auction=auction, bidder=previous_leader
        ).first()
        if prev_proxy and prev_proxy.max_amount <= auction.current_price:
            release_deposit_hold(previous_leader, auction)

        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"user_{previous_leader.id}",
                {
                    "type": "targeted_outbid_alert",
                    "data": {
                        "auction_id": str(auction.id),
                        "auction_title": auction.title,
                        "new_price": str(auction.current_price),
                        "message": (
                            f"You have been outbid on '{auction.title}'. "
                            f"Current price: ${auction.current_price}."
                        ),
                    },
                },
            )

    return {
        "bid_id": str(bid.id),
        "auction_id": str(auction.id),
        "current_price": str(auction.current_price),
        "amount": str(auction.current_price),
        "final_price": str(auction.current_price),
        "highest_bidder": auction.highest_bidder.username,
        "your_max_budget": str(target_budget),
        "ends_at": auction.ends_at.isoformat(),
        "extended": extended,
    }

@transaction.atomic
def execute_proxy_bid(
    auction_id: str,
    user,
    max_amount: Decimal = None,
    amount: Decimal = None,
) -> dict:
    target_budget = max_amount if max_amount is not None else amount
    if target_budget is None:
        raise AuctionDomainException("A valid bid budget must be provided.")
    target_budget = Decimal(str(target_budget))

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

    current_highest_bid = auction.bids.order_by("-amount", "-created_at").first()
    if not current_highest_bid:
        min_bid = auction.starting_price
    elif auction.highest_bidder == user:
        min_bid = auction.current_price
    else:
        min_bid = auction.current_price + Decimal("5.00")

    if target_budget < min_bid:
        raise AuctionDomainException(f"Bid budget must be at least ${min_bid}.")

    acquire_or_adjust_deposit_hold(user, auction, target_budget)

    extended = False
    if (auction.ends_at - now).total_seconds() <= 120:
        auction.ends_at = auction.ends_at + timedelta(seconds=120)
        extended = True

    previous_leader = auction.highest_bidder

    proxy_bid, _ = ProxyBid.objects.select_for_update().get_or_create(
        auction=auction,
        bidder=user,
        defaults={"max_amount": target_budget},
    )
    if proxy_bid.max_amount != target_budget:
        proxy_bid.max_amount = target_budget
        proxy_bid.save(update_fields=["max_amount", "updated_at"])

    proxies = list(
        ProxyBid.objects.select_for_update()
        .filter(auction=auction)
        .order_by("-max_amount", "created_at")[:2]
    )

    leading_proxy = proxies[0]
    runner_up_proxy = proxies[1] if len(proxies) > 1 else None

    if not runner_up_proxy:
        new_price = max(auction.starting_price, auction.current_price)
    else:
        if leading_proxy.max_amount == runner_up_proxy.max_amount:
            new_price = leading_proxy.max_amount
        else:
            calculated_step = runner_up_proxy.max_amount + Decimal("5.00")
            new_price = min(leading_proxy.max_amount, calculated_step)

    auction.current_price = new_price
    auction.highest_bidder = leading_proxy.bidder
    auction.save(update_fields=["current_price", "highest_bidder", "ends_at", "updated_at"])

    Bid.objects.create(
        auction=auction,
        bidder=auction.highest_bidder,
        amount=auction.current_price,
    )

    if previous_leader and auction.highest_bidder != previous_leader:
        prev_proxy = ProxyBid.objects.filter(auction=auction, bidder=previous_leader).first()
        if prev_proxy and prev_proxy.max_amount <= auction.current_price:
            release_deposit_hold(previous_leader, auction)

        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"user_{previous_leader.id}",
                {
                    "type": "targeted_outbid_alert",
                    "data": {
                        "auction_id": str(auction.id),
                        "auction_title": auction.title,
                        "new_price": str(auction.current_price),
                        "message": f"You have been outbid on '{auction.title}'. Current price: ${auction.current_price}.",
                    },
                },
            )


    return {
        "auction_id": str(auction.id),
        "current_price": str(auction.current_price),
        "amount": str(auction.current_price),  # Satisfies views expecting 'amount'
        "highest_bidder": auction.highest_bidder.username,
        "your_max_budget": str(target_budget),
        "ends_at": auction.ends_at.isoformat(),
        "extended": extended,
    }


def execute_bid(auction_id: str, user, amount: Decimal) -> dict:
    return execute_proxy_bid(auction_id=auction_id, user=user, max_amount=amount)

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
            "action": "already_closed",
        }

    if (auction.reserve_price and auction.current_price < auction.reserve_price) or not auction.highest_bidder:
        auction.status = Auction.Status.UNSOLD
        auction.save(update_fields=["status", "updated_at"])
        return {
            "auction_id": str(auction.id),
            "status": auction.status,
            "order_id": None,
        }

    auction.status = Auction.Status.CLOSED
    auction.save(update_fields=["status", "updated_at"])

    # Provide payment_due_at to satisfy the non-null database constraint
    order = Order.objects.create(
        auction=auction,
        buyer=auction.highest_bidder,
        amount=auction.current_price,
        status=Order.Status.PENDING_PAYMENT,
        payment_due_at=timezone.now() + timedelta(hours=24),
    )

    return {
        "auction_id": str(auction.id),
        "status": auction.status,
        "order_id": str(order.id),
        "winner": auction.highest_bidder.username,
        "final_price": str(auction.current_price),
    }


@transaction.atomic
def execute_buy_it_now(auction_id: str, user) -> dict:
    try:
        auction = Auction.objects.select_for_update().get(id=auction_id)
    except Auction.DoesNotExist:
        raise AuctionDomainException("Auction not found.")

    if not auction.buy_it_now_price:
        raise AuctionDomainException("Buy It Now is not available for this auction.")

    if auction.status != Auction.Status.ACTIVE or auction.is_expired:
        raise AuctionDomainException("Auction is no longer active.")

    if auction.seller_id and auction.seller_id == user.id:
        raise AuctionDomainException("Sellers cannot buy their own item.")

    auction.status = Auction.Status.CLOSED
    auction.highest_bidder = user
    auction.current_price = auction.buy_it_now_price
    auction.save(update_fields=["status", "highest_bidder", "current_price", "updated_at"])

    order = Order.objects.create(
        auction=auction,
        buyer=user,
        amount=auction.buy_it_now_price,
        status=Order.Status.PENDING_PAYMENT,
        payment_due_at=timezone.now() + timedelta(hours=24),
    )

    return {
        "auction_id": str(auction.id),
        "order_id": str(order.id),
        "amount": str(order.amount),
        "final_price": str(order.amount),  
        "winner": user.username,            
        "status": auction.status,
    }


def settle_order_payment(order_id: str, user, transaction_id: str) -> dict:
    """
    Settles an order atomically. Validates ownership, deadline, and payment status.
    If the order is overdue, commits the EXPIRED status before raising the domain error.
    Captures the deposit hold upon successful payment.
    """
    is_overdue = False

    with transaction.atomic():
        try:
            order = (
                Order.objects.select_for_update()
                .select_related("auction")
                .get(id=order_id)
            )
        except Order.DoesNotExist:
            raise AuctionDomainException("Order not found.")

        if order.buyer != user:
            raise AuctionDomainException(
                "You do not have permission to pay for this order."
            )

        if order.status == Order.Status.PAID:
            raise AuctionDomainException("This order has already been paid.")

        if order.status != Order.Status.PENDING_PAYMENT:
            raise AuctionDomainException(
                f"Cannot settle order in '{order.status}' status."
            )

        if order.is_payment_overdue:
            order.status = Order.Status.EXPIRED
            order.save(update_fields=["status", "updated_at"])
            is_overdue = True
        else:
            now = timezone.now()
            order.status = Order.Status.PAID
            order.paid_at = now
            order.transaction_id = transaction_id
            order.save(
                update_fields=["status", "paid_at", "transaction_id", "updated_at"]
            )

            # Capture deposit hold now that order is assigned and marked PAID
            capture_deposit_hold(order.buyer, order.auction)

            if order.auction.seller:
                record_order_settlement_ledger(order)

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
    Called when a winner fails to pay. Expires the original order, forfeits
    the winner's security deposit, and either generates a second-chance offer
    for the runner-up or marks the item UNSOLD.
    """
    try:
        order = (
            Order.objects.select_for_update()
            .select_related("auction")
            .get(id=order_id)
        )
    except Order.DoesNotExist:
        raise AuctionDomainException("Order not found.")

    if order.status != Order.Status.PENDING_PAYMENT:
        return {
            "order_id": str(order.id),
            "status": order.status,
            "action": "ignored",
        }

    # Lock and retrieve auction BEFORE referencing it
    auction = Auction.objects.select_for_update().get(id=order.auction_id)

    # 1. Mark original winner's order as EXPIRED
    order.status = Order.Status.EXPIRED
    order.save(update_fields=["status", "updated_at"])

    # 2. Forfeit deposit hold for the defaulting buyer
    forfeit_deposit_hold(order.buyer, auction)

    # 3. Query runner-up from ProxyBid table (excluding defaulted winner)
    runner_up_proxy = (
        ProxyBid.objects.select_for_update()
        .filter(auction=auction)
        .exclude(bidder=order.buyer)
        .order_by("-max_amount", "created_at")
        .first()
    )

    # 4. Check viability of second-chance offer
    can_offer = False
    if runner_up_proxy:
        offered_amount = runner_up_proxy.max_amount
        if not auction.reserve_price or offered_amount >= auction.reserve_price:
            can_offer = True

    if not can_offer:
        auction.status = Auction.Status.UNSOLD
        auction.save(update_fields=["status", "updated_at"])
        return {
            "order_id": str(order.id),
            "auction_id": str(auction.id),
            "action": "marked_unsold",
            "offer_id": None,
        }

    # 5. Create SecondChanceOffer
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

    # 6. Broadcast targeted WebSocket notification to runner-up
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
                    "message": (
                        f"Good news! The winner defaulted. You have a second-chance offer "
                        f"to buy '{auction.title}' for ${offer.amount}."
                    ),
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
    try:
        offer = (
            SecondChanceOffer.objects.select_for_update()
            .select_related("auction")
            .get(id=offer_id)
        )
    except SecondChanceOffer.DoesNotExist:
        raise AuctionDomainException("Offer not found.")

    if offer.recipient != user:
        raise AuctionDomainException("You do not have permission to respond to this offer.")

    if offer.status != SecondChanceOffer.Status.PENDING:
        raise AuctionDomainException(f"Cannot respond to offer in '{offer.status}' status.")

    if offer.is_expired:
        offer.status = SecondChanceOffer.Status.EXPIRED
        offer.save(update_fields=["status", "updated_at"])
        raise AuctionDomainException("This offer has expired.")

    if not accept:
        offer.status = SecondChanceOffer.Status.DECLINED
        offer.save(update_fields=["status", "updated_at"])
        offer.auction.status = Auction.Status.UNSOLD
        offer.auction.save(update_fields=["status", "updated_at"])
        return {"offer_id": str(offer.id), "status": offer.status, "order_id": None}

    offer.status = SecondChanceOffer.Status.ACCEPTED
    offer.save(update_fields=["status", "updated_at"])

    offer.auction.status = Auction.Status.CLOSED
    offer.auction.highest_bidder = user
    offer.auction.current_price = offer.amount
    offer.auction.save(update_fields=["status", "highest_bidder", "current_price", "updated_at"])

    order = Order.objects.create(
        auction=offer.auction,
        buyer=offer.recipient,
        amount=offer.amount,
        status=Order.Status.PENDING_PAYMENT,
        payment_due_at=timezone.now() + timedelta(hours=24),
    )

    return {
        "offer_id": str(offer.id),
        "status": offer.status,
        "order_id": str(order.id),
    }
@transaction.atomic
def cancel_auction(auction_id: str, user) -> dict:
    """Cancels an auction atomically.

    Sellers may only cancel if zero bids have been placed.
    """
    try:
        auction = Auction.objects.select_for_update().get(id=auction_id)
    except Auction.DoesNotExist:
        raise AuctionDomainException("Auction not found.")

    # Strictly reject non-sellers (or if auction has no seller set)
    if (auction.seller is None or auction.seller != user) and not user.is_staff:
        raise AuctionDomainException(
            "You do not have permission to cancel this auction."
        )

    if auction.status != Auction.Status.ACTIVE:
        raise AuctionDomainException(
            f"Cannot cancel auction in '{auction.status}' status."
        )

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
    record_deposit_forfeiture_ledger(user, auction, hold.amount)

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




def get_or_create_system_accounts():
    """Retrieves or provisions standard chart of accounts."""
    clearing_cash, _ = LedgerAccount.objects.get_or_create(
        code="1000_CLEARING_CASH",
        defaults={
            "name": "Platform Clearing Cash / Stripe",
            "account_type": LedgerAccount.AccountType.ASSET,
        },
    )
    user_wallets, _ = LedgerAccount.objects.get_or_create(
        code="2100_USER_WALLET",
        defaults={
            "name": "User Wallet Liabilities",
            "account_type": LedgerAccount.AccountType.LIABILITY,
        },
    )
    commission_rev, _ = LedgerAccount.objects.get_or_create(
        code="4000_PLATFORM_COMMISSION",
        defaults={
            "name": "Marketplace Commission Revenue",
            "account_type": LedgerAccount.AccountType.REVENUE,
        },
    )
    forfeiture_rev, _ = LedgerAccount.objects.get_or_create(
        code="4100_FORFEITURE_PENALTY",
        defaults={
            "name": "Defaulted Hold Forfeiture Revenue",
            "account_type": LedgerAccount.AccountType.REVENUE,
        },
    )
    return clearing_cash, user_wallets, commission_rev, forfeiture_rev


def get_or_create_seller_payable_account(seller) -> LedgerAccount:
    """Returns or creates a designated liability account for a specific seller."""
    account, _ = LedgerAccount.objects.get_or_create(
        code=f"2000_SELLER_{seller.id}",
        defaults={
            "name": f"Payable to {seller.username}",
            "account_type": LedgerAccount.AccountType.LIABILITY,
            "user": seller,
        },
    )
    return account


@transaction.atomic
def record_double_entry_transaction(
    transaction_type: str,
    reference_id: str,
    description: str,
    entry_specs: list[dict],
) -> LedgerTransaction:
    """
    Records an atomic financial transaction verifying mathematical balance.
    Each spec: {"account": LedgerAccount, "entry_type": "DEBIT"|"CREDIT", "amount": Decimal}
    """
    total_debits = Decimal("0.00")
    total_credits = Decimal("0.00")

    for spec in entry_specs:
        amount = Decimal(str(spec["amount"]))
        if spec["entry_type"] == LedgerEntry.EntryType.DEBIT:
            total_debits += amount
        elif spec["entry_type"] == LedgerEntry.EntryType.CREDIT:
            total_credits += amount

    # Enforce accounting equation: Debits must balance Credits
    if total_debits != total_credits:
        raise AuctionDomainException(
            f"Ledger imbalance detected: Total debits (${total_debits}) "
            f"do not equal total credits (${total_credits})."
        )

    txn = LedgerTransaction.objects.create(
        transaction_type=transaction_type,
        reference_id=str(reference_id),
        description=description,
    )

    for spec in entry_specs:
        LedgerEntry.objects.create(
            transaction=txn,
            account=spec["account"],
            entry_type=spec["entry_type"],
            amount=Decimal(str(spec["amount"])),
        )

    return txn


@transaction.atomic
def record_order_settlement_ledger(order: Order) -> LedgerTransaction:
    """
    Splits gross order settlement into:
    - Debit Clearing Cash (Asset) for gross amount
    - Credit Platform Commission (Revenue) for 5%
    - Credit Seller Payable (Liability) for net payout
    """
    clearing_cash, _, commission_rev, _ = get_or_create_system_accounts()
    seller_account = get_or_create_seller_payable_account(order.auction.seller)

    gross_amount = order.amount
    commission_fee = (gross_amount * PLATFORM_COMMISSION_RATE).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    seller_net = gross_amount - commission_fee

    entries = [
        {
            "account": clearing_cash,
            "entry_type": LedgerEntry.EntryType.DEBIT,
            "amount": gross_amount,
        },
        {
            "account": commission_rev,
            "entry_type": LedgerEntry.EntryType.CREDIT,
            "amount": commission_fee,
        },
        {
            "account": seller_account,
            "entry_type": LedgerEntry.EntryType.CREDIT,
            "amount": seller_net,
        },
    ]

    return record_double_entry_transaction(
        transaction_type=LedgerTransaction.TransactionType.ORDER_SETTLEMENT,
        reference_id=str(order.id),
        description=f"Order settlement for Order #{order.id} ({order.auction.title})",
        entry_specs=entries,
    )


@transaction.atomic
def record_deposit_forfeiture_ledger(user, auction, hold_amount: Decimal) -> LedgerTransaction:
    """
    Transfers defaulted deposit hold from user liability to platform forfeiture revenue.
    """
    _, user_wallets, _, forfeiture_rev = get_or_create_system_accounts()

    entries = [
        {
            "account": user_wallets,
            "entry_type": LedgerEntry.EntryType.DEBIT,
            "amount": hold_amount,
        },
        {
            "account": forfeiture_rev,
            "entry_type": LedgerEntry.EntryType.CREDIT,
            "amount": hold_amount,
        },
    ]

    return record_double_entry_transaction(
        transaction_type=LedgerTransaction.TransactionType.DEPOSIT_FORFEITURE,
        reference_id=f"forfeit_{auction.id}_{user.id}",
        description=f"Forfeited deposit hold for {user.username} on auction #{auction.id}",
        entry_specs=entries,
    )


@transaction.atomic
def record_seller_payout_ledger(seller, amount: Decimal, payout_ref: str) -> LedgerTransaction:
    """
    Reduces seller payable liability and decreases platform clearing cash upon payout.
    """
    clearing_cash, _, _, _ = get_or_create_system_accounts()
    seller_account = get_or_create_seller_payable_account(seller)

    if seller_account.current_balance < amount:
        raise AuctionDomainException(
            f"Insufficient seller balance: Available payable balance is ${seller_account.current_balance}."
        )

    entries = [
        {
            "account": seller_account,
            "entry_type": LedgerEntry.EntryType.DEBIT,
            "amount": amount,
        },
        {
            "account": clearing_cash,
            "entry_type": LedgerEntry.EntryType.CREDIT,
            "amount": amount,
        },
    ]

    return record_double_entry_transaction(
        transaction_type=LedgerTransaction.TransactionType.SELLER_PAYOUT,
        reference_id=payout_ref,
        description=f"Disbursed payout to seller {seller.username}",
        entry_specs=entries,
    )