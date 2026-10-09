from django.utils import timezone
from rest_framework import generics, permissions
from .models import Auction
from .serializers import AuctionSerializer


class AuctionListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/auctions/      -> List all active, unexpired auctions.
    POST /api/auctions/      -> Create a new auction item (requires JWT authentication).
    """
    serializer_class = AuctionSerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [permissions.IsAuthenticated()]
        return [permissions.AllowAny()]

    def get_queryset(self):
        # Only return active auctions whose countdown has not expired
        return (
            Auction.objects.filter(
                status=Auction.Status.ACTIVE,
                ends_at__gt=timezone.now(),
            )
            .select_related("highest_bidder")
            .order_by("ends_at")
        )


class AuctionDetailView(generics.RetrieveAPIView):
    """
    GET  /api/auctions/<id>/ -> Retrieve full details for a specific auction.
    """
    queryset = Auction.objects.select_related("highest_bidder").all()
    serializer_class = AuctionSerializer
    permission_classes = [permissions.AllowAny]