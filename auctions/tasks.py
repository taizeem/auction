from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer
from django.utils import timezone
from .models import Auction
from .services import close_auction


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