from decimal import Decimal
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from auctions.models import Auction

User = get_user_model()


class AuctionHTTPAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="seller_user",
            password="SecurePassword123!",
            email="seller@example.com",
        )
        self.list_create_url = reverse("auction_list_create")

        # Create active auction
        self.active_auction = Auction.objects.create(
            title="Vintage Mechanical Keyboard",
            starting_price=Decimal("120.00"),
            current_price=Decimal("120.00"),
            status=Auction.Status.ACTIVE,
            ends_at=timezone.now() + timedelta(days=2),
        )

        # Create already-expired auction
        self.expired_auction = Auction.objects.create(
            title="Old Monitor",
            starting_price=Decimal("30.00"),
            current_price=Decimal("30.00"),
            status=Auction.Status.ACTIVE,
            ends_at=timezone.now() - timedelta(hours=1),
        )

    def test_list_auctions_returns_only_active_unexpired_items(self):
        response = self.client.get(self.list_create_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        returned_ids = [item["id"] for item in response.data]
        self.assertIn(str(self.active_auction.id), returned_ids)
        self.assertNotIn(str(self.expired_auction.id), returned_ids)

    def test_unauthenticated_user_cannot_create_auction(self):
        payload = {
            "title": "Unauthorized Item",
            "starting_price": "50.00",
            "ends_at": (timezone.now() + timedelta(days=1)).isoformat(),
        }
        response = self.client.post(self.list_create_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_user_can_create_auction(self):
        self.client.force_authenticate(user=self.user)
        payload = {
            "title": "Retro Gaming Console",
            "description": "Mint condition with original controllers.",
            "starting_price": "250.00",
            "ends_at": (timezone.now() + timedelta(days=3)).isoformat(),
        }
        response = self.client.post(self.list_create_url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["title"], "Retro Gaming Console")
        self.assertEqual(response.data["current_price"], "250.00")
        self.assertEqual(response.data["status"], "ACTIVE")

    def test_create_auction_rejects_past_end_time(self):
        self.client.force_authenticate(user=self.user)
        payload = {
            "title": "Invalid Timestamp Auction",
            "starting_price": "100.00",
            "ends_at": (timezone.now() - timedelta(minutes=10)).isoformat(),
        }
        response = self.client.post(self.list_create_url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ends_at", response.data)