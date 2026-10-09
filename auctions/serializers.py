from decimal import Decimal
from django.utils import timezone
from rest_framework import serializers
from .models import Auction


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
        # Automatically initialize current_price to the starting_price on creation
        validated_data["current_price"] = validated_data["starting_price"]
        return super().create(validated_data)