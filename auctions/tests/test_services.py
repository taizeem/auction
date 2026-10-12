from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone
from auctions.models import Auction, Bid, ProxyBid
from auctions.services import execute_bid, AuctionDomainException, execute_proxy_bid

User = get_user_model()


class ConcurrentBiddingServiceTests(TransactionTestCase):
    def setUp(self):
        self.auction = Auction.objects.create(
            title="Vintage Rolex Submariner",
            starting_price=Decimal("1000.00"),
            current_price=Decimal("1000.00"),
            ends_at=timezone.now() + timedelta(hours=1),
            status=Auction.Status.ACTIVE,
        )
        self.bidders = [
            User.objects.create_user(
                username=f"bidder_{i}",
                password="TestPassword123!",
                email=f"bidder_{i}@test.com",
            )
            for i in range(10)
        ]

    def tearDown(self):
        # Guarantee all thread connections are closed when tests finish
        connections.close_all()

    def test_concurrent_bidding_prevents_race_conditions(self):
        bid_amounts = [Decimal(f"{1100 + (i * 100)}.00") for i in range(10)]
        successful_bids = []
        failed_bids = []

        def worker(bidder, amount):
            try:
                result = execute_bid(
                    auction_id=str(self.auction.id),
                    user=bidder,
                    amount=amount,
                )
                return True, result
            except AuctionDomainException as exc:
                return False, str(exc)
            finally:
                # Crucial on PostgreSQL: close each thread-local connection
                connections.close_all()

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [
                executor.submit(worker, self.bidders[i], bid_amounts[i])
                for i in range(10)
            ]
            for future in as_completed(futures):
                success, data = future.result()
                if success:
                    successful_bids.append(data)
                else:
                    failed_bids.append(data)

        self.auction.refresh_from_db()
        highest_recorded_bid = Bid.objects.filter(auction=self.auction).order_by("-amount").first()
        self.assertEqual(self.auction.current_price, highest_recorded_bid.amount)
        self.assertEqual(self.auction.highest_bidder, highest_recorded_bid.bidder)
        self.assertGreater(len(successful_bids), 0)

    def test_bidding_rejected_on_expired_auction(self):
        self.auction.ends_at = timezone.now() - timedelta(minutes=5)
        self.auction.save()

        with self.assertRaises(AuctionDomainException) as ctx:
            execute_bid(
                auction_id=str(self.auction.id),
                user=self.bidders[0],
                amount=Decimal("2000.00"),
            )

        self.assertIn("expired", str(ctx.exception))




class AutoExtendServiceTests(TransactionTestCase):
    def setUp(self):
        self.user_a = User.objects.create_user(username="bidder_a", password="pw")
        self.user_b = User.objects.create_user(username="bidder_b", password="pw")

    def test_bid_outside_threshold_does_not_extend_deadline(self):
        # Auction ending in 10 minutes (threshold is 2 minutes)
        original_end = timezone.now() + timedelta(minutes=10)
        auction = Auction.objects.create(
            title="Standard Item",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=original_end,
            status=Auction.Status.ACTIVE,
        )

        result = execute_bid(str(auction.id), self.user_a, Decimal("110.00"))

        auction.refresh_from_db()
        self.assertFalse(result["extended"])
        # Timestamp remains unchanged
        self.assertEqual(auction.ends_at, original_end)

    def test_bid_inside_threshold_extends_deadline(self):
        # Auction ending in 45 seconds (inside the 120s threshold)
        initial_end = timezone.now() + timedelta(seconds=45)
        auction = Auction.objects.create(
            title="Sniped Item",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=initial_end,
            status=Auction.Status.ACTIVE,
        )

        result = execute_bid(str(auction.id), self.user_a, Decimal("150.00"))

        auction.refresh_from_db()
        self.assertTrue(result["extended"])
        # The new ends_at must now be roughly 120 seconds in the future
        remaining_seconds = (auction.ends_at - timezone.now()).total_seconds()
        self.assertGreater(remaining_seconds, 100)
        self.assertGreater(auction.ends_at, initial_end)

class ProxyBiddingEngineTests(TransactionTestCase):
    def setUp(self):
        self.auction = Auction.objects.create(
            title="Vintage Omega Speedmaster",
            starting_price=Decimal("100.00"),
            current_price=Decimal("100.00"),
            ends_at=timezone.now() + timedelta(hours=2),
            status=Auction.Status.ACTIVE,
        )
        self.alice = User.objects.create_user(username="alice", password="pw")
        self.bob = User.objects.create_user(username="bob", password="pw")
        self.charlie = User.objects.create_user(username="charlie", password="pw")

    def tearDown(self):
        connections.close_all()

    def test_first_bidder_starts_at_starting_price(self):
        # Alice enters a $200 max budget. Price should stay at starting $100.
        res = execute_proxy_bid(str(self.auction.id), self.alice, Decimal("200.00"))
        
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.current_price, Decimal("100.00"))
        self.assertEqual(self.auction.highest_bidder, self.alice)
        self.assertTrue(res["is_caller_leading"])

    def test_lower_competing_proxy_bids_up_winner_without_overtaking(self):
        # 1. Alice sets max $200 (current_price = $100)
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("200.00"))

        # 2. Bob bids $150. Increment at $150 is $5.00.
        # Alice should remain leader at $150 + $5 = $155.00.
        res = execute_proxy_bid(str(self.auction.id), self.bob, Decimal("150.00"))

        self.auction.refresh_from_db()
        self.assertEqual(self.auction.current_price, Decimal("155.00"))
        self.assertEqual(self.auction.highest_bidder, self.alice)
        self.assertFalse(res["is_caller_leading"])

    def test_higher_competing_proxy_overtakes_and_calculates_correct_step(self):
        # 1. Alice sets max $200 (current_price = $100)
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("200.00"))

        # 2. Bob bids $300. Increment at Alice's $200 is $5.00.
        # Bob becomes leader at $200 + $5 = $205.00.
        res = execute_proxy_bid(str(self.auction.id), self.bob, Decimal("300.00"))

        self.auction.refresh_from_db()
        self.assertEqual(self.auction.current_price, Decimal("205.00"))
        self.assertEqual(self.auction.highest_bidder, self.bob)
        self.assertTrue(res["is_caller_leading"])

    def test_tied_proxy_bids_awarded_to_first_timestamp(self):
        # 1. Alice bids $250
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("250.00"))

        # 2. Bob also bids $250.
        res = execute_proxy_bid(str(self.auction.id), self.bob, Decimal("250.00"))

        self.auction.refresh_from_db()
        # Alice submitted first, so she stays winner at exactly $250.00
        self.assertEqual(self.auction.current_price, Decimal("250.00"))
        self.assertEqual(self.auction.highest_bidder, self.alice)
        self.assertFalse(res["is_caller_leading"])

    def test_leading_bidder_can_increase_budget_without_raising_price(self):
        # Alice bids $200, price is $100
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("200.00"))
        
        # Alice raises budget to $400
        execute_proxy_bid(str(self.auction.id), self.alice, Decimal("400.00"))

        self.auction.refresh_from_db()
        self.assertEqual(self.auction.current_price, Decimal("100.00"))
        
        user_proxy = ProxyBid.objects.get(auction=self.auction, bidder=self.alice)
        self.assertEqual(user_proxy.max_amount, Decimal("400.00"))