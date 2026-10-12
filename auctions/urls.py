from django.urls import path
from .views import (
    AuctionListCreateView,
    AuctionDetailView,
    AuctionBidHistoryView,
    UserProfileView,
    AuctionProxyBidView,
)

urlpatterns = [
    path("", AuctionListCreateView.as_view(), name="auction_list_create"),
    path("profile/", UserProfileView.as_view(), name="user_profile"),
    path("<uuid:pk>/", AuctionDetailView.as_view(), name="auction_detail"),
    path("<uuid:pk>/bids/", AuctionBidHistoryView.as_view(), name="auction_bid_history"),
    path("<uuid:pk>/proxy-bid/", AuctionProxyBidView.as_view(), name="auction_proxy_bid"),
]