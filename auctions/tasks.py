from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer
from django.utils import timezone
from .models import Auction, Order
from .services import close_auction, process_defaulted_order_and_second_chance


@shared_task
def check_and_close_expired_auctions():
    """
    Periodic task executed by Celery Beat.
    Identifies expired auctions, updates their status, and broadcasts closure events to Redis.
    """
    now = timezone.now()
    expired_auctions = Auction.objects.filter(
        status=Auction.Status.ACTIVE,
        ends_at__lte=now,
    ).values_list("id", flat=True)

    channel_layer = get_channel_layer()

    closed_count = 0
    for auction_id in expired_auctions:
        result = close_auction(str(auction_id))
        if not result["already_closed"]:
            closed_count += 1
            # Dispatch closure payload through Redis to all connected room subscribers
            async_to_sync(channel_layer.group_send)(
                f"auction_{auction_id}",
                {
                    "type": "broadcast_closure",
                    "payload": result,
                },
            )

    return f"Closed {closed_count} expired auctions at {now.isoformat()}"


@shared_task
def expire_overdue_orders():
    """
    Scans for expired orders and triggers second-chance offers.
    """
    now = timezone.now()
    overdue_orders = list(
        Order.objects.filter(
            status=Order.Status.PENDING_PAYMENT,
            payment_due_at__lte=now,
        ).values_list("id", flat=True)
    )

    processed = 0
    for order_id in overdue_orders:
        process_defaulted_order_and_second_chance(str(order_id))
        processed += 1

    return f"Processed {processed} overdue order(s) for second-chance offers."