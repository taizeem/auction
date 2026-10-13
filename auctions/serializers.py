from decimal import Decimal
from django.utils import timezone
from rest_framework import serializers
from .models import Auction, Bid, ProxyBid, Order, SecondChanceOffer, UserWallet, DepositHold


def mask_username(username: str | None) -> str:
    if not username:
        return "***"
    clean = username.strip()
    if len(clean) <= 2:
        return f"{clean[0]}***"
    return f"{clean[0]}***{clean[-1]}"


class AuctionSerializer(serializers.ModelSerializer):
    seller_username = serializers.CharField(
        source="seller.username", read_only=True
    )
    highest_bidder_username = serializers.CharField(
        source="highest_bidder.username", read_only=True
    )
    is_expired = serializers.BooleanField(read_only=True)
    has_reserve = serializers.BooleanField(read_only=True)
    is_reserve_met = serializers.BooleanField(read_only=True)

    class Meta:
        model = Auction
        fields = [
            "id",
            "seller",
            "seller_username",
            "title",
            "description",
            "starting_price",
            "current_price",
            "reserve_price",
            "buy_it_now_price",
            "has_reserve",
            "is_reserve_met",
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
            "seller",
            "current_price",
            "highest_bidder",
            "status",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            "reserve_price": {"write_only": True},
        }

    def validate_starting_price(self, value: Decimal) -> Decimal:
        if value <= Decimal("0.00"):
            raise serializers.ValidationError("Starting price must be strictly greater than 0.00.")
        return value

    def validate_ends_at(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError("Auction end timestamp must be set in the future.")
        return value

    def validate(self, attrs):
        # 1. Edit restrictions when updating an existing listing
        if self.instance is not None:
            has_bids = self.instance.bids.exists()
            if has_bids:
                locked_fields = ["starting_price", "reserve_price", "buy_it_now_price", "ends_at"]
                for field in locked_fields:
                    if field in attrs and attrs[field] != getattr(self.instance, field):
                        raise serializers.ValidationError({
                            field: f"Cannot modify '{field}' once bids have been placed."
                        })

        # 2. General price boundary validations
        starting_price = attrs.get("starting_price") or (
            self.instance.starting_price if self.instance else None
        )
        reserve_price = attrs.get("reserve_price") or (
            self.instance.reserve_price if self.instance else None
        )
        buy_it_now_price = attrs.get("buy_it_now_price") or (
            self.instance.buy_it_now_price if self.instance else None
        )

        if reserve_price and starting_price and reserve_price < starting_price:
            raise serializers.ValidationError({
                "reserve_price": "Reserve price must be greater than or equal to starting price."
            })

        if buy_it_now_price and starting_price and buy_it_now_price <= starting_price:
            raise serializers.ValidationError({
                "buy_it_now_price": "Buy It Now price must strictly exceed the starting price."
            })

        if buy_it_now_price and reserve_price and buy_it_now_price < reserve_price:
            raise serializers.ValidationError({
                "buy_it_now_price": "Buy It Now price cannot be lower than the reserve price."
            })

        return attrs

    def create(self, validated_data):
        validated_data["current_price"] = validated_data["starting_price"]
        return super().create(validated_data)

class BidHistorySerializer(serializers.ModelSerializer):
    bidder_username = serializers.SerializerMethodField()

    class Meta:
        model = Bid
        fields = [
            "id",
            "bidder_username",
            "amount",
            "created_at",
        ]
        read_only_fields = fields

    def get_bidder_username(self, obj) -> str:
        request = self.context.get("request")

        if request and request.user.is_authenticated:
            if obj.bidder_id == request.user.id:
                return f"{obj.bidder.username} (You)"
            if request.user.is_staff:
                return f"{obj.bidder.username} [Staff]"

        return mask_username(obj.bidder.username)


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


class ProxyBidSerializer(serializers.ModelSerializer):
    auction_id = serializers.UUIDField(source="auction.id", read_only=True)
    auction_title = serializers.CharField(source="auction.title", read_only=True)
    current_price = serializers.DecimalField(
        source="auction.current_price", max_digits=12, decimal_places=2, read_only=True
    )
    is_highest_bidder = serializers.SerializerMethodField()

    class Meta:
        model = ProxyBid
        fields = [
            "id",
            "auction_id",
            "auction_title",
            "max_amount",
            "current_price",
            "is_highest_bidder",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_is_highest_bidder(self, obj) -> bool:
        return obj.auction.highest_bidder_id == obj.bidder_id


class SetProxyBidSerializer(serializers.Serializer):
    max_amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
        required=True,
    )


class OrderSerializer(serializers.ModelSerializer):
    auction_id = serializers.UUIDField(source="auction.id", read_only=True)
    auction_title = serializers.CharField(source="auction.title", read_only=True)
    buyer_username = serializers.CharField(source="buyer.username", read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "auction_id",
            "auction_title",
            "buyer_username",
            "amount",
            "status",
            "payment_due_at",
            "paid_at",
            "transaction_id",
            "created_at",
        ]
        read_only_fields = fields


class PaymentSubmitSerializer(serializers.Serializer):
    transaction_id = serializers.CharField(
        max_length=255, required=False, default="txn_simulated_success"
    )

class SecondChanceOfferSerializer(serializers.ModelSerializer):
    auction_id = serializers.UUIDField(source="auction.id", read_only=True)
    auction_title = serializers.CharField(source="auction.title", read_only=True)

    class Meta:
        model = SecondChanceOffer
        fields = [
            "id",
            "auction_id",
            "auction_title",
            "amount",
            "status",
            "expires_at",
            "created_at",
        ]
        read_only_fields = fields


class SecondChanceActionSerializer(serializers.Serializer):
    accept = serializers.BooleanField(required=True)


class UserWalletSerializer(serializers.ModelSerializer):
    available_balance = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = UserWallet
        fields = ["balance", "held_balance", "available_balance", "updated_at"]
        read_only_fields = ["held_balance", "available_balance", "updated_at"]


class WalletDepositFundSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("1.00"))
