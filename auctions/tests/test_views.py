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
    def test_unauthenticated_user_cannot_access_profile(self):
        url = reverse("user_profile")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_user_profile_returns_won_auctions_and_active_bids(self):
        # Create a dedicated user for clean isolation from setUp()
        profile_user = User.objects.create_user(
            username="profile_user",
            password="SecurePassword123!",
            email="profile_user@example.com",
        )

        # 1. Auction won by profile_user (CLOSED)
        won_item = Auction.objects.create(
            title="Won Vintage Camera",
            starting_price=Decimal("200.00"),
            current_price=Decimal("350.00"),
            highest_bidder=profile_user,
            status=Auction.Status.CLOSED,
            ends_at=timezone.now() - timedelta(days=1),
        )

        # 2. Auction won by another user (should NOT appear in profile_user's won list)
        Auction.objects.create(
            title="Other User Item",
            starting_price=Decimal("100.00"),
            current_price=Decimal("200.00"),
            highest_bidder=self.bidder_two,
            status=Auction.Status.CLOSED,
            ends_at=timezone.now() - timedelta(days=1),
        )

        # 3. Active auction where profile_user is CURRENTLY WINNING
        active_winning = Auction.objects.create(
            title="Active Guitar Auction",
            starting_price=Decimal("500.00"),
            current_price=Decimal("600.00"),
            highest_bidder=profile_user,
            status=Auction.Status.ACTIVE,
            ends_at=timezone.now() + timedelta(days=2),
        )
        winning_bid = Bid.objects.create(
            auction=active_winning,
            bidder=profile_user,
            amount=Decimal("600.00"),
        )

        # 4. Active auction where profile_user was OUTBID by bidder_two
        active_outbid = Auction.objects.create(
            title="Active Painting Auction",
            starting_price=Decimal("300.00"),
            current_price=Decimal("450.00"),
            highest_bidder=self.bidder_two,
            status=Auction.Status.ACTIVE,
            ends_at=timezone.now() + timedelta(days=3),
        )
        outbid_bid = Bid.objects.create(
            auction=active_outbid,
            bidder=profile_user,
            amount=Decimal("400.00"),
        )
        Bid.objects.create(
            auction=active_outbid,
            bidder=self.bidder_two,
            amount=Decimal("450.00"),
        )

        # Authenticate as the isolated profile_user
        self.client.force_authenticate(user=profile_user)
        url = reverse("user_profile")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify user details and counts
        self.assertEqual(response.data["user"]["username"], "profile_user")
        self.assertEqual(response.data["won_auctions_count"], 1)
        self.assertEqual(response.data["active_bids_count"], 2)

        # Verify won auctions
        won_list = response.data["won_auctions"]
        self.assertEqual(len(won_list), 1)
        self.assertEqual(won_list[0]["id"], str(won_item.id))
        self.assertEqual(won_list[0]["final_price"], "350.00")

        # Verify active bids and is_highest_bidder flags
        active_bids = response.data["active_bids"]
        self.assertEqual(len(active_bids), 2)

        # Leading bid verification
        leading_record = next(b for b in active_bids if b["id"] == str(winning_bid.id))
        self.assertTrue(leading_record["is_highest_bidder"])
        self.assertEqual(leading_record["amount"], "600.00")
        self.assertEqual(leading_record["current_price"], "600.00")

        # Outbid verification
        outbid_record = next(b for b in active_bids if b["id"] == str(outbid_bid.id))
        self.assertFalse(outbid_record["is_highest_bidder"])
        self.assertEqual(outbid_record["amount"], "400.00")
        self.assertEqual(outbid_record["current_price"], "450.00")