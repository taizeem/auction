from django.urls import path
from .views import AuctionListCreateView, AuctionDetailView, AuctionBidHistoryView

urlpatterns = [
    path("", AuctionListCreateView.as_view(), name="auction_list_create"),
    path("<uuid:pk>/", AuctionDetailView.as_view(), name="auction_detail"),
    path("<uuid:pk>/bids/", AuctionBidHistoryView.as_view(), name="auction_bid_history"),
]