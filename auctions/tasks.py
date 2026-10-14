from datetime import timedelta
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from celery import shared_task
from django.utils import timezone
from .models import Watchlist

from auctions.models import Auction, Order, SecondChanceOffer
from auctions.services import (
    close_auction,
    process_defaulted_order_and_second_chance,
)


@shared_task
def close_expired_auctions_task():
    now = timezone.now()
    expired_auction_ids = list(
        Auction.objects.filter(
            status=Auction.Status.ACTIVE,
            ends_at__lte=now,
        ).values_list("id", flat=True)
    )

    results = []
    for auction_id in expired_auction_ids:
        try:
            res = close_auction(str(auction_id))
            results.append(res)
        except Exception as exc:
            results.append({"auction_id": str(auction_id), "error": str(exc)})
    return results


@shared_task
def process_overdue_orders_task():
    now = timezone.now()
    overdue_order_ids = list(
        Order.objects.filter(
            status=Order.Status.PENDING_PAYMENT,
            payment_due_at__lte=now,
        ).values_list("id", flat=True)
    )

    results = []
    for order_id in overdue_order_ids:
        try:
            res = process_defaulted_order_and_second_chance(str(order_id))
            results.append(res)
        except Exception as exc:
            results.append({"order_id": str(order_id), "error": str(exc)})
    return results

@shared_task
def expire_second_chance_offers_task():
    now = timezone.now()
    expired_offers = SecondChanceOffer.objects.filter(
        status=SecondChanceOffer.Status.PENDING,
        expires_at__lte=now,
    ).select_related("auction")

    expired_count = 0
    for offer in expired_offers:
        offer.status = SecondChanceOffer.Status.EXPIRED
        offer.save(update_fields=["status", "updated_at"])

        auction = offer.auction
        if auction.status != Auction.Status.ACTIVE:
            auction.status = Auction.Status.UNSOLD
            auction.save(update_fields=["status", "updated_at"])
        expired_count += 1

    return expired_count

@shared_task
def notify_ending_soon_auctions_task():
    """
    Scans for ACTIVE auctions ending within 15 minutes that haven't been notified yet.
    Dispatches targeted WebSocket messages to all watching users.
    """
    now = timezone.now()
    threshold = now + timedelta(minutes=15)

    # Find active auctions ending soon that haven't been flagged
    ending_soon_auctions = Auction.objects.filter(
        status=Auction.Status.ACTIVE,
        ends_at__gt=now,
        ends_at__lte=threshold,
        ending_soon_notified=False,
    ).prefetch_related("watchers__user")

    channel_layer = get_channel_layer()
    notifications_sent = 0

    for auction in ending_soon_auctions:
        watchers = list(auction.watchers.select_related("user"))
        minutes_remaining = max(1, int((auction.ends_at - now).total_seconds() / 60))

        if channel_layer and watchers:
            for watch in watchers:
                async_to_sync(channel_layer.group_send)(
                    f"user_{watch.user.id}",
                    {
                        "type": "ending_soon_alert",
                        "data": {
                            "auction_id": str(auction.id),
                            "auction_title": auction.title,
                            "current_price": str(auction.current_price),
                            "minutes_remaining": minutes_remaining,
                            "ends_at": auction.ends_at.isoformat(),
                            "message": (
                                f"⏳ '{auction.title}' is ending in ~{minutes_remaining} minutes! "
                                f"Current bid: ${auction.current_price}."
                            ),
                        },
                    },
                )
                notifications_sent += 1

        auction.ending_soon_notified = True
        auction.save(update_fields=["ending_soon_notified", "updated_at"])

    return notifications_sent