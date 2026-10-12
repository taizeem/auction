from decimal import Decimal, InvalidOperation
from django.utils import timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from rest_framework import generics, permissions, filters, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.generics import get_object_or_404
from .models import Auction, Bid, ProxyBid, Order
from .services import execute_proxy_bid, AuctionDomainException, execute_buy_it_now, settle_order_payment
from .serializers import (
    AuctionSerializer,
    BidHistorySerializer,
    UserProfileSerializer,
    ProxyBidSerializer,
    SetProxyBidSerializer,
    mask_username,
    OrderSerializer,
    PaymentSubmitSerializer,
)
from .pagination import StandardResultsSetPagination


class AuctionListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/auctions/
         Query filters: ?search=..., ?ordering=..., ?min_price=..., ?max_price=..., ?page=..., ?page_size=...
    POST /api/auctions/ -> Create listing (Authenticated users only)
    """
    serializer_class = AuctionSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title", "description"]
    ordering_fields = ["current_price", "ends_at", "created_at"]
    ordering = ["ends_at"]

    def get_permissions(self):
        if self.request.method == "POST":
            return [permissions.IsAuthenticated()]
        return [permissions.AllowAny()]

    def get_queryset(self):
        queryset = (
            Auction.objects.filter(
                status=Auction.Status.ACTIVE,
                ends_at__gt=timezone.now(),
            )
            .select_related("highest_bidder")
        )

        min_price = self.request.query_params.get("min_price")
        max_price = self.request.query_params.get("max_price")

        if min_price is not None:
            try:
                queryset = queryset.filter(current_price__gte=Decimal(min_price))
            except (InvalidOperation, ValueError):
                pass

        if max_price is not None:
            try:
                queryset = queryset.filter(current_price__lte=Decimal(max_price))
            except (InvalidOperation, ValueError):
                pass

        return queryset


class AuctionDetailView(generics.RetrieveAPIView):
    """
    GET /api/auctions/<uuid:pk>/ -> Retrieve full details for a single item.
    """
    queryset = Auction.objects.select_related("highest_bidder").all()
    serializer_class = AuctionSerializer
    permission_classes = [permissions.AllowAny]


class AuctionBidHistoryView(generics.ListAPIView):
    """
    GET /api/auctions/<uuid:pk>/bids/ -> Paginated bid logs (newest first, usernames masked).
    """
    serializer_class = BidHistorySerializer
    pagination_class = StandardResultsSetPagination
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        auction_id = self.kwargs.get("pk")
        get_object_or_404(Auction, id=auction_id)
        return (
            Bid.objects.filter(auction_id=auction_id)
            .select_related("bidder")
            .order_by("-created_at")
        )


class UserProfileView(APIView):
    """
    GET /api/auctions/profile/ -> Won auctions and ongoing active bids for the caller.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        now = timezone.now()
        user = request.user

        # 1. Closed items won by caller
        won_auctions = (
            Auction.objects.filter(
                highest_bidder=user,
                status=Auction.Status.CLOSED,
            )
            .order_by("-ends_at")
        )

        # 2. Caller's bids on currently active listings
        active_bids = (
            Bid.objects.filter(
                bidder=user,
                auction__status=Auction.Status.ACTIVE,
                auction__ends_at__gt=now,
            )
            .select_related("auction", "auction__highest_bidder")
            .order_by("-created_at")
        )

        serializer = UserProfileSerializer(
            {
                "user": user,
                "won_auctions_count": won_auctions.count(),
                "active_bids_count": active_bids.count(),
                "won_auctions": won_auctions,
                "active_bids": active_bids,
            },
            context={"request": request},
        )
        return Response(serializer.data, status=status.HTTP_200_OK)


class AuctionProxyBidView(APIView):
    """
    GET  /api/auctions/<uuid:pk>/proxy-bid/ -> View caller's private maximum budget.
    POST /api/auctions/<uuid:pk>/proxy-bid/ -> Set or raise private maximum budget.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        auction = get_object_or_404(Auction, id=pk)
        proxy_bid = (
            ProxyBid.objects.filter(auction=auction, bidder=request.user)
            .select_related("auction")
            .first()
        )
        if not proxy_bid:
            return Response(
                {"detail": "No proxy budget set for this auction."},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = ProxyBidSerializer(proxy_bid)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, pk):
        auction = get_object_or_404(Auction, id=pk)
        input_serializer = SetProxyBidSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        max_amount = input_serializer.validated_data["max_amount"]

        try:
            bid_result = execute_proxy_bid(
                auction_id=str(auction.id),
                user=request.user,
                max_amount=max_amount,
            )
        except AuctionDomainException as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        channel_layer = get_channel_layer()
        if channel_layer:
            # 1. Public room broadcast
            async_to_sync(channel_layer.group_send)(
                f"auction_{auction.id}",
                {
                    "type": "broadcast_bid",
                    "bid_data": {
                        "auction_id": bid_result["auction_id"],
                        "amount": bid_result["amount"],
                        "highest_bidder": mask_username(bid_result["highest_bidder"]),
                        "bid_id": bid_result["bid_id"],
                        "ends_at": bid_result["ends_at"],
                        "extended": bid_result["extended"],
                        "timestamp": bid_result["timestamp"],
                    },
                },
            )

            # 2. Targeted outbid alert
            displaced_id = bid_result.get("displaced_user_id")
            if displaced_id:
                async_to_sync(channel_layer.group_send)(
                    f"user_{displaced_id}",
                    {
                        "type": "outbid_notification",
                        "data": {
                            "auction_id": bid_result["auction_id"],
                            "auction_title": bid_result["auction_title"],
                            "new_price": bid_result["amount"],
                            "message": f"You have been outbid on '{bid_result['auction_title']}'. Current price is now ${bid_result['amount']}.",
                        },
                    },
                )

        proxy_bid = ProxyBid.objects.get(auction=auction, bidder=request.user)
        return Response(
            {
                "proxy_bid": ProxyBidSerializer(proxy_bid).data,
                "is_leading": bid_result["is_caller_leading"],
                "current_price": bid_result["amount"],
                "extended": bid_result["extended"],
            },
            status=status.HTTP_200_OK,
        )

class AuctionBuyItNowView(APIView):
    """
    POST /api/auctions/<uuid:pk>/buy-it-now/
    Atomically purchases an active auction item at its buy_it_now_price.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        try:
            result = execute_buy_it_now(auction_id=str(pk), user=request.user)
        except AuctionDomainException as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Broadcast immediate auction closure over WebSockets
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"auction_{pk}",
                {
                    "type": "broadcast_auction_closed",
                    "data": {
                        "auction_id": result["auction_id"],
                        "status": result["status"],
                        "winner": result["winner"],
                        "final_price": result["final_price"],
                        "reason": "buy_it_now",
                    },
                },
            )

        return Response(result, status=status.HTTP_200_OK)


class OrderDetailView(generics.RetrieveAPIView):
    """
    GET /api/orders/<uuid:pk>/ -> Retrieve order details (buyer or staff only).
    """
    serializer_class = OrderSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Order.objects.filter(buyer=self.request.user).select_related("auction", "buyer")


class OrderPayView(APIView):
    """
    POST /api/orders/<uuid:pk>/pay/ -> Submits payment settlement for a pending order.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        serializer = PaymentSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        txn_id = serializer.validated_data.get("transaction_id") or "txn_mock_success"

        try:
            result = settle_order_payment(
                order_id=str(pk),
                user=request.user,
                transaction_id=txn_id,
            )
        except AuctionDomainException as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)