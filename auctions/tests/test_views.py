import uuid
from decimal import Decimal
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from auctions.models import Auction, Bid

User = get_user_model()


class AuctionHTTPAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="seller_user",
            password="SecurePassword123!",
            email="seller@example.com",
        )
        self.list_create_url = reverse("auction_list_create")

        # Item 1: Keyboard
        self.keyboard = Auction.objects.create(
            title="Vintage Mechanical Keyboard",
            description="Cherry MX Blue switches with coiled cable.",
            starting_price=Decimal("120.00"),
            current_price=Decimal("120.00"),
            status=Auction.Status.ACTIVE,
            ends_at=timezone.now() + timedelta(days=2),
        )

        # Item 2: Rolex
        self.rolex = Auction.objects.create(
            title="Vintage Rolex Submariner",
            description="Classic 1980s diver watch in mint condition.",
            starting_price=Decimal("5000.00"),
            current_price=Decimal("5000.00"),
            status=Auction.Status.ACTIVE,
            ends_at=timezone.now() + timedelta(days=5),
        )

        # Item 3: Expired monitor
        self.expired_auction = Auction.objects.create(
            title="Old Monitor",
            description="CRT display.",
            starting_price=Decimal("30.00"),
            current_price=Decimal("30.00"),
            status=Auction.Status.ACTIVE,
            ends_at=timezone.now() - timedelta(hours=1),
        )

    def test_list_auctions_pagination_structure(self):
        response = self.client.get(self.list_create_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify envelope structure
        self.assertIn("count", response.data)
        self.assertIn("next", response.data)
        self.assertIn("previous", response.data)
        self.assertIn("results", response.data)

        # Active items only (2 active, 1 expired)
        self.assertEqual(response.data["count"], 2)
        returned_ids = [item["id"] for item in response.data["results"]]
        self.assertIn(str(self.keyboard.id), returned_ids)
        self.assertIn(str(self.rolex.id), returned_ids)
        self.assertNotIn(str(self.expired_auction.id), returned_ids)

    def test_search_filter_by_title_and_description(self):
        # Search by description keyword "switches"
        response = self.client.get(self.list_create_url, {"search": "switches"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(self.keyboard.id))

        # Search by title keyword "Rolex"
        response = self.client.get(self.list_create_url, {"search": "Rolex"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(self.rolex.id))

    def test_ordering_filter_by_current_price_descending(self):
        response = self.client.get(self.list_create_url, {"ordering": "-current_price"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(results[0]["id"], str(self.rolex.id))
        self.assertEqual(results[1]["id"], str(self.keyboard.id))

    def test_price_range_filtering(self):
        # Filter for items between $50 and $200
        response = self.client.get(self.list_create_url, {"min_price": "50.00", "max_price": "200.00"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(self.keyboard.id))

    def test_custom_page_size(self):
        response = self.client.get(self.list_create_url, {"page_size": 1})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertIsNotNone(response.data["next"])

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