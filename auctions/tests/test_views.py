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
        self.bidder_one = User.objects.create_user(
            username="bidder_one",
            password="SecurePassword123!",
            email="bidder1@example.com",
        )
        self.bidder_two = User.objects.create_user(
            username="bidder_two",
            password="SecurePassword123!",
            email="bidder2@example.com",
        )
        self.list_create_url = reverse("auction_list_create")

        # Test item
        self.auction = Auction.objects.create(
            title="Vintage Mechanical Keyboard",
            description="Cherry MX Blue switches with coiled cable.",
            starting_price=Decimal("120.00"),
            current_price=Decimal("150.00"),
            status=Auction.Status.ACTIVE,
            ends_at=timezone.now() + timedelta(days=2),
        )

        # Pre-seed bid records
        self.bid_1 = Bid.objects.create(
            auction=self.auction,
            bidder=self.bidder_one,
            amount=Decimal("130.00"),
        )
        self.bid_2 = Bid.objects.create(
            auction=self.auction,
            bidder=self.bidder_two,
            amount=Decimal("150.00"),
        )

    def test_list_bid_history_returns_bids_newest_first(self):
        url = reverse("auction_bid_history", kwargs={"pk": self.auction.id})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)

        results = response.data["results"]
        # Newest bid ($150) must appear first
        self.assertEqual(results[0]["id"], str(self.bid_2.id))
        self.assertEqual(results[0]["amount"], "150.00")
        self.assertEqual(results[0]["bidder_username"], "bidder_two")

        # First bid ($130) appears second
        self.assertEqual(results[1]["id"], str(self.bid_1.id))
        self.assertEqual(results[1]["amount"], "130.00")
        self.assertEqual(results[1]["bidder_username"], "bidder_one")

    def test_list_bid_history_supports_pagination(self):
        url = reverse("auction_bid_history", kwargs={"pk": self.auction.id})
        response = self.client.get(url, {"page_size": 1})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["id"], str(self.bid_2.id))
        self.assertIsNotNone(response.data["next"])

    def test_list_bid_history_non_existent_auction_returns_404(self):
        non_existent_id = uuid.uuid4()
        url = reverse("auction_bid_history", kwargs={"pk": non_existent_id})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)