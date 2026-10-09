from django.urls import path
from .views import AuctionListCreateView, AuctionDetailView

urlpatterns = [
    path("", AuctionListCreateView.as_view(), name="auction_list_create"),
    path("<uuid:pk>/", AuctionDetailView.as_view(), name="auction_detail"),
]