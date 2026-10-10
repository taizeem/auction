from decimal import Decimal, InvalidOperation
from django.utils import timezone
from rest_framework import generics, permissions, filters
from rest_framework.generics import get_object_or_404
from .models import Auction, Bid
from .serializers import AuctionSerializer, BidHistorySerializer
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