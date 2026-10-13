from datetime import timedelta
from celery import shared_task
from django.utils import timezone

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