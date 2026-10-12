from decimal import Decimal, InvalidOperation
from django.utils import timezone
from rest_framework import generics, permissions, filters
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.generics import get_object_or_404
from .models import Auction, Bid
from .serializers import (
    AuctionSerializer,
    BidHistorySerializer,
    UserProfileSerializer,
)
from .pagination import StandardResultsSetPagination


class AuctionListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/auctions/      -> List all active auctions (paginated, searchable, filterable).
    POST /api/auctions/      -> Create a new auction item (authenticated users only).
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
    GET  /api/auctions/<uuid:pk>/ -> Retrieve full details for a single auction item.
    """
    queryset = Auction.objects.select_related("highest_bidder").all()
    serializer_class = AuctionSerializer
    permission_classes = [permissions.AllowAny]


class AuctionBidHistoryView(generics.ListAPIView):
    """
    GET  /api/auctions/<uuid:pk>/bids/ -> List all historical bids for an auction, newest first.
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
    GET  /api/auctions/profile/ -> Returns profile details, won auctions, and active bids.
    Requires SimpleJWT Authentication.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        now = timezone.now()
        user = request.user

        # 1. Closed auctions won by the authenticated user
        won_auctions = (
            Auction.objects.filter(
                highest_bidder=user,
                status=Auction.Status.CLOSED,
            )
            .order_by("-ends_at")
        )

        # 2. Bids placed by the user on currently active, unexpired auctions
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
        return Response(serializer.data)