from datetime import timedelta
from decimal import Decimal
import uuid
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from auctions.models import Auction, UserWallet
from auctions.services import execute_proxy_bid

User = get_user_model()


class AuctionHTTPAPITests(APITestCase):

    def setUp(self):
        self.bidder_one = User.objects.create_user(username="bidder_one", password="pw")
        self.bidder_two = User.objects.create_user(username="bidder_two", password="pw")
        self.user = self.bidder_one
        self.other_user = self.bidder_two

        for u in (self.bidder_one, self.bidder_two):
            UserWallet.objects.create(user=u, balance=Decimal("10000.00"))

        self.auction = Auction.objects.create(
            title="Vintage Watch",
            description="Classic collectible",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            reserve_price=Decimal("200.00"),
            buy_it_now_price=Decimal("500.00"),
            ends_at=timezone.now() + timedelta(days=2),
            status=Auction.Status.ACTIVE,
        )

    def test_list_bid_history_returns_bids_newest_first_and_masked(self):
        execute_proxy_bid(str(self.auction.id), self.bidder_one, Decimal("150.00"))
        execute_proxy_bid(str(self.auction.id), self.bidder_two, Decimal("200.00"))

        url = reverse("auction_bid_history", kwargs={"pk": self.auction.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"] if "results" in response.data else response.data
        self.assertGreaterEqual(len(results), 2)
        self.assertEqual(results[0]["bidder_username"], "b***o")

    def test_list_bid_history_supports_pagination(self):
        execute_proxy_bid(str(self.auction.id), self.bidder_one, Decimal("150.00"))
        execute_proxy_bid(str(self.auction.id), self.bidder_two, Decimal("200.00"))

        url = reverse("auction_bid_history", kwargs={"pk": self.auction.id}) + "?page_size=1"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)

    def test_list_bid_history_non_existent_auction_returns_404(self):
        url = reverse("auction_bid_history", kwargs={"pk": uuid.uuid4()})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_bid_history_displays_you_tag_for_own_bids(self):
        execute_proxy_bid(str(self.auction.id), self.bidder_one, Decimal("150.00"))
        execute_proxy_bid(str(self.auction.id), self.bidder_two, Decimal("200.00"))

        self.client.force_authenticate(user=self.bidder_two)
        url = reverse("auction_bid_history", kwargs={"pk": self.auction.id})
        response = self.client.get(url)
        results = response.data["results"] if "results" in response.data else response.data
        self.assertEqual(results[0]["bidder_username"], "bidder_two (You)")

    def test_post_proxy_bid_sets_and_retrieves_budget(self):
        self.client.force_authenticate(user=self.bidder_one)
        url = reverse("auction_proxy_bid", kwargs={"pk": self.auction.id})

        post_res = self.client.post(url, {"max_amount": "200.00"}, format="json")
        self.assertEqual(post_res.status_code, status.HTTP_200_OK)

        get_res = self.client.get(url)
        self.assertEqual(get_res.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(str(get_res.data["max_amount"])), Decimal("200.00"))

    def test_post_proxy_bid_rejects_budget_lower_than_required_increment(self):
        self.client.force_authenticate(user=self.bidder_one)
        url = reverse("auction_proxy_bid", kwargs={"pk": self.auction.id})

        post_res = self.client.post(url, {"max_amount": "50.00"}, format="json")
        self.assertEqual(post_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("must be at least", post_res.data["detail"])

    def test_get_proxy_bid_returns_404_when_none_exists(self):
        self.client.force_authenticate(user=self.bidder_one)
        url = reverse("auction_proxy_bid", kwargs={"pk": self.auction.id})
        res = self.client.get(url)
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_buy_it_now_endpoint_authenticated_success(self):
        self.client.force_authenticate(user=self.bidder_one)
        url = reverse("auction_buy_it_now", kwargs={"pk": self.auction.id})

        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.status, Auction.Status.CLOSED)

    def test_buy_it_now_endpoint_unauthenticated_rejected(self):
        url = reverse("auction_buy_it_now", kwargs={"pk": self.auction.id})
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unauthenticated_cannot_access_or_set_proxy_bid(self):
        url = reverse("auction_proxy_bid", kwargs={"pk": self.auction.id})
        self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(
            self.client.post(url, {"max_amount": "200.00"}).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_unauthenticated_user_cannot_access_profile(self):
        url = reverse("user_profile")
        self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_user_profile_returns_won_auctions_and_active_bids(self):
        execute_proxy_bid(str(self.auction.id), self.bidder_two, Decimal("150.00"))
        self.client.force_authenticate(user=self.bidder_two)
        url = reverse("user_profile")
        res = self.client.get(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("active_bids", res.data)


class SellerRestrictionsTests(APITestCase):

    def setUp(self):
        self.seller = User.objects.create_user(username="item_seller", password="pw")
        self.buyer = User.objects.create_user(username="honest_buyer", password="pw")
        self.other_user = User.objects.create_user(username="stranger", password="pw")

        for u in (self.seller, self.buyer, self.other_user):
            UserWallet.objects.create(user=u, balance=Decimal("10000.00"))

        self.auction = Auction.objects.create(
            seller=self.seller,
            title="Vintage Turntable",
            description="Mint condition audio equipment.",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(days=3),
            status=Auction.Status.ACTIVE,
        )

    def test_seller_cannot_bid_on_own_auction_anti_shill(self):
        self.client.force_authenticate(user=self.seller)
        url = reverse("auction_proxy_bid", kwargs={"pk": self.auction.id})
        res = self.client.post(url, {"max_amount": "200.00"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("own auctions", res.data["detail"])

    def test_seller_can_cancel_auction_with_zero_bids(self):
        self.client.force_authenticate(user=self.seller)
        url = reverse("auction_cancel", kwargs={"pk": self.auction.id})
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.status, Auction.Status.CANCELLED)

    def test_non_seller_cannot_cancel_auction(self):
        self.client.force_authenticate(user=self.other_user)
        url = reverse("auction_cancel", kwargs={"pk": self.auction.id})
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("do not have permission", res.data["detail"])

    def test_cancellation_rejected_after_bids_are_placed(self):
        execute_proxy_bid(str(self.auction.id), self.buyer, Decimal("150.00"))
        self.client.force_authenticate(user=self.seller)
        url = reverse("auction_cancel", kwargs={"pk": self.auction.id})
        res = self.client.post(url)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("after bids have already been placed", res.data["detail"])

    def test_seller_can_edit_description_even_after_bids_exist(self):
        execute_proxy_bid(str(self.auction.id), self.buyer, Decimal("150.00"))
        self.client.force_authenticate(user=self.seller)
        url = reverse("auction_detail", kwargs={"pk": self.auction.id})
        patch_data = {"description": "Updated: Includes high-grade needle cartridge."}
        res = self.client.patch(url, patch_data, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["description"], patch_data["description"])

    def test_seller_cannot_edit_price_terms_after_bids_exist(self):
        execute_proxy_bid(str(self.auction.id), self.buyer, Decimal("150.00"))
        self.client.force_authenticate(user=self.seller)
        url = reverse("auction_detail", kwargs={"pk": self.auction.id})
        patch_data = {"starting_price": "250.00"}
        res = self.client.patch(url, patch_data, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("starting_price", res.data)