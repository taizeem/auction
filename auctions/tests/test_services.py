from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone
from auctions.models import Auction, Bid
from auctions.services import execute_bid, AuctionDomainException

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