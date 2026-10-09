from django.contrib import admin
from .models import Auction, Bid


class BidInline(admin.TabularInline):
    model = Bid
    extra = 0
    readonly_fields = ("bidder", "amount", "created_at")


@admin.register(Auction)
class AuctionAdmin(admin.ModelAdmin):
    list_display = ("title", "current_price", "highest_bidder", "status", "ends_at")
    list_filter = ("status", "ends_at")
    search_fields = ("title", "highest_bidder__username")
    inlines = [BidInline]


@admin.register(Bid)
class BidAdmin(admin.ModelAdmin):
    list_display = ("auction", "bidder", "amount", "created_at")
    list_filter = ("created_at",)