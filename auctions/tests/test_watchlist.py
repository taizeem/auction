from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from auctions.models import Auction, Watchlist, UserWallet
from auctions.tasks import notify_ending_soon_auctions_task

User = get_user_model()


class WatchlistAPITests(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="watch_user", password="pw")
        self.auction = Auction.objects.create(
            title="Vintage Omega Speedmaster",
            starting_price=Decimal("500.00"),
            current_price=Decimal("500.00"),
            ends_at=timezone.now() + timedelta(hours=3),
            status=Auction.Status.ACTIVE,
        )

    def test_toggle_watch_adds_and_removes(self):
        self.client.force_authenticate(user=self.user)
        url = reverse("auction_watch_toggle", kwargs={"pk": self.auction.id})

        # 1. First POST adds to watchlist
        res1 = self.client.post(url)
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        self.assertTrue(res1.data["is_watching"])
        self.assertEqual(Watchlist.objects.filter(user=self.user, auction=self.auction).count(), 1)

        # 2. Second POST removes from watchlist
        res2 = self.client.post(url)
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertFalse(res2.data["is_watching"])
        self.assertEqual(Watchlist.objects.filter(user=self.user, auction=self.auction).count(), 0)

    def test_watchlist_list_returns_user_watched_auctions(self):
        self.client.force_authenticate(user=self.user)
        Watchlist.objects.create(user=self.user, auction=self.auction)

        url = reverse("watchlist_list")
        res = self.client.get(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data["results"] if "results" in res.data else res.data
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["auction"]["title"], self.auction.title)

    def test_unauthenticated_cannot_watch(self):
        url = reverse("auction_watch_toggle", kwargs={"pk": self.auction.id})
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)


class EndingSoonTaskTests(TransactionTestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="task_watcher", password="pw")
        UserWallet.objects.create(user=self.user, balance=Decimal("1000.00"))

    def test_notify_ending_soon_sends_alert_and_sets_flag(self):
        # Auction ending in 10 minutes (within 15m window)
        auction = Auction.objects.create(
            title="Sealed Pokemon Booster",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(minutes=10),
            status=Auction.Status.ACTIVE,
            ending_soon_notified=False,
        )
        Watchlist.objects.create(user=self.user, auction=auction)

        sent_count = notify_ending_soon_auctions_task()
        self.assertEqual(sent_count, 1)

        auction.refresh_from_db()
        self.assertTrue(auction.ending_soon_notified)

        # Second execution skips because already notified
        second_sent = notify_ending_soon_auctions_task()
        self.assertEqual(second_sent, 0)

    def test_task_ignores_auctions_ending_in_more_than_fifteen_minutes(self):
        auction = Auction.objects.create(
            title="Far Away Auction",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=2),
            status=Auction.Status.ACTIVE,
            ending_soon_notified=False,
        )
        Watchlist.objects.create(user=self.user, auction=auction)

        sent_count = notify_ending_soon_auctions_task()
        self.assertEqual(sent_count, 0)

        auction.refresh_from_db()
        self.assertFalse(auction.ending_soon_notified)