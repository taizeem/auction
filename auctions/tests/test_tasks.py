from decimal import Decimal
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from auctions.models import Auction
from auctions.tasks import check_and_close_expired_auctions

User = get_user_model()


class CeleryTaskTests(TestCase):
    def test_check_and_close_expired_auctions(self):
        user = User.objects.create_user(
            username="winner_user",
            password="Password123!",
            email="winner@test.com",
        )
        # Create an auction past its expiration time
        auction = Auction.objects.create(
            title="Rare Coin",
            starting_price=Decimal("100.00"),
            current_price=Decimal("250.00"),
            highest_bidder=user,
            ends_at=timezone.now() - timedelta(minutes=1),
            status=Auction.Status.ACTIVE,
        )

        # Run the task synchronously
        result_message = check_and_close_expired_auctions()
        self.assertIn("Closed 1 expired auctions", result_message)

        auction.refresh_from_db()
        self.assertEqual(auction.status, Auction.Status.CLOSED)