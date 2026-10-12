from decimal import Decimal
from django.utils import timezone
from rest_framework import serializers
from .models import Auction, Bid


class AuctionSerializer(serializers.ModelSerializer):
    highest_bidder_username = serializers.CharField(
        source="highest_bidder.username", read_only=True
    )
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = Auction
        fields = [
            "id",
            "title",
            "description",
            "starting_price",
            "current_price",
            "highest_bidder",
            "highest_bidder_username",
            "status",
            "ends_at",
            "is_expired",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "current_price",
            "highest_bidder",
            "status",
            "created_at",
            "updated_at",
        ]

    def validate_starting_price(self, value: Decimal) -> Decimal:
        if value <= Decimal("0.00"):
            raise serializers.ValidationError("Starting price must be strictly greater than 0.00.")
        return value

    def validate_ends_at(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError("Auction end timestamp must be set in the future.")
        return value

    def create(self, validated_data):
        validated_data["current_price"] = validated_data["starting_price"]
        return super().create(validated_data)


class BidHistorySerializer(serializers.ModelSerializer):
    bidder_username = serializers.CharField(source="bidder.username", read_only=True)

    class Meta:
        model = Bid
        fields = [
            "id",
            "bidder_username",
            "amount",
            "created_at",
        ]
        read_only_fields = fields


class WonAuctionSerializer(serializers.ModelSerializer):
    final_price = serializers.DecimalField(
        source="current_price", max_digits=12, decimal_places=2, read_only=True
    )

    class Meta:
        model = Auction
        fields = [
            "id",
            "title",
            "description",
            "final_price",
            "ends_at",
            "created_at",
        ]
        read_only_fields = fields


class ActiveBidSerializer(serializers.ModelSerializer):
    auction_id = serializers.UUIDField(source="auction.id", read_only=True)
    auction_title = serializers.CharField(source="auction.title", read_only=True)
    current_price = serializers.DecimalField(
        source="auction.current_price", max_digits=12, decimal_places=2, read_only=True
    )
    auction_ends_at = serializers.DateTimeField(source="auction.ends_at", read_only=True)
    is_highest_bidder = serializers.SerializerMethodField()

    class Meta:
        model = Bid
        fields = [
            "id",
            "auction_id",
            "auction_title",
            "amount",
            "current_price",
            "is_highest_bidder",
            "auction_ends_at",
            "created_at",
        ]
        read_only_fields = fields

    def get_is_highest_bidder(self, obj) -> bool:
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.auction.highest_bidder_id == request.user.id
        return False


class UserSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField()


class UserProfileSerializer(serializers.Serializer):
    user = UserSummarySerializer()
    won_auctions_count = serializers.IntegerField()
    active_bids_count = serializers.IntegerField()
    won_auctions = WonAuctionSerializer(many=True)
    active_bids = ActiveBidSerializer(many=True)