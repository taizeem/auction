from decimal import Decimal, InvalidOperation
from django.utils import timezone
from rest_framework import generics, permissions, filters
from .models import Auction
from .serializers import AuctionSerializer
from .pagination import StandardResultsSetPagination


class AuctionListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/auctions/
         Query parameters supported:
         - ?page=1
         - ?page_size=10
         - ?search=vintage
         - ?ordering=current_price (or -current_price, ends_at, -created_at)
         - ?min_price=50.00
         - ?max_price=500.00
    POST /api/auctions/ -> Create new auction (Authenticated users only)
    """
    serializer_class = AuctionSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    
    # Matches case-insensitively across both title and description
    search_fields = ["title", "description"]
    
    # Whitelisted fields for ?ordering=
    ordering_fields = ["current_price", "ends_at", "created_at"]
    ordering = ["ends_at"]  # Default ordering: expiring soonest first

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

        # Explicit price range filters
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
    GET /api/auctions/<id>/ -> Retrieve full details for a single auction.
    """
    queryset = Auction.objects.select_related("highest_bidder").all()
    serializer_class = AuctionSerializer
    permission_classes = [permissions.AllowAny]