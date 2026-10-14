# LiveAuction Engine — Distributed Real-Time Auction & Settlement System

A mission-critical, real-time auction engine designed to solve concurrency race conditions, financial tracking integrity, and real-time state distribution during high-frequency bidding wars.

Built around **Django 6**, **Django Channels 4**, **Daphne (ASGI)**, **Redis**, and **PostgreSQL**, this platform implements automated English proxy bidding, dynamic bracket-based increments, anti-sniping soft closes, token-bucket rate limiting, 10% pre-authorization wallet deposit holds, and an append-only double-entry financial ledger.

---

## Architecture Overview

```mermaid
flowchart TB
    subgraph Ingress ["Edge & Gateway Tier"]
        ClientHTTP["HTTP Clients\n(REST API)"]
        ClientWS["WebSocket Clients\n(Live Bidders & Spectators)"]
        Nginx["Nginx Reverse Proxy\n(HTTP/1.1 Upgrade & SSL)"]
    end

    subgraph Runtime ["Application Layer (ASGI / Daphne)"]
        DaphneServer["Daphne ASGI Server\n(Port 8000)"]
        JWTMiddleware["JWT Channels Middleware\n(Async Handshake Auth)"]
        Consumer["AuctionConsumer\n(Room Groups & Targeted User Channels)"]
        DRFAPI["Django REST Framework\n(API Views & Serializers)"]
    end

    subgraph InMemCoordination ["In-Memory Coordination Tier (Redis 7.x)"]
        ChannelLayer["Channels Layer\n(Broadcast PUB/SUB)"]
        Presence["Spectator Presence\n(Redis Sets)"]
        RateLimit["Rate Limiter\n(Atomic Lua Token Bucket)"]
        CeleryBroker["Celery Queue & Results\n(DB 1)"]
    end

    subgraph Persistence ["Persistence & Financial Integrity (PostgreSQL)"]
        LockManager["Concurrency Barrier\n(SELECT FOR UPDATE)"]
        DomainModels[("Auctions, Bids, Orders,\nProxyBids, Holds, Watchlist")]
        DoubleEntryLedger[("Immutable Double-Entry Ledger\n(Accounts, Txns, Entries)")]
    end

    subgraph Background ["Worker & Scheduler Tier"]
        CeleryWorker["Celery Worker\n(Order Processing & Sweep Tasks)"]
        CeleryBeat["Celery Beat\n(Cron Scheduler: 30s & 60s)"]
    end

    ClientHTTP --> Nginx
    ClientWS --> Nginx
    Nginx --> DaphneServer
    DaphneServer --> JWTMiddleware
    JWTMiddleware --> Consumer
    DaphneServer --> DRFAPI

    Consumer --> ChannelLayer
    Consumer --> Presence
    Consumer --> RateLimit
    Consumer --> LockManager
    DRFAPI --> LockManager

    LockManager --> DomainModels
    LockManager --> DoubleEntryLedger

    CeleryBeat --> CeleryBroker
    CeleryBroker --> CeleryWorker
    CeleryWorker --> LockManager

```

---

## Technical Highlights & Engineering Decisions

### 1. Concurrency-Safe Proxy Bidding Engine

* **Pessimistic Row Locking:** All bid submissions execute within atomic database transactions wrapped in `Auction.objects.select_for_update()`. Simultaneous bids on the same item are serialized at the database level, preventing race conditions and stale-price overwrites.
* **Standing Price Calculation:** Competitors set private maximum budgets. The system automatically computes and advances the public standing price to one increment above the second-highest proxy ceiling.
* **Anti-Shill Enforcement:** Bids originating from the item's verified seller account are rejected at the service layer.

### 2. Dynamic Bracket-Based Bid Increments

Implements tiered minimum bid increments that scale dynamically with item value (standard auction house rules):

| Price Bracket | Increment | Example Progression |
| --- | --- | --- |
| `$0.01` – `$99.99` | **`$1.00`** | `$25.00` $\rightarrow$ **`$26.00`** |
| `$100.00` – `$499.99` | **`$5.00`** | `$150.00` $\rightarrow$ **`$155.00`** |
| `$500.00` – `$999.99` | **`$25.00`** | `$600.00` $\rightarrow$ **`$625.00`** |
| `$1,000.00` – `$4,999.99` | **`$50.00`** | `$1,500.00` $\rightarrow$ **`$1,550.00`** |
| `$5,000.00+` | **`$100.00`** | `$10,000.00` $\rightarrow$ **`$10,100.00`** |

### 3. Anti-Sniping Protection (Soft-Close Window)

Prevents low-latency automated sniping bots from closing auctions without human counter-bids. If a bid arrives within **120 seconds** of `ends_at`, the deadline automatically extends by **120 seconds**.

### 4. Capital Risk Mitigation (10% Pre-Authorization Hold)

Eliminates trolling and non-paying winners by requiring an active security deposit hold:

* Bidders must hold **10% of their proxy ceiling** in available wallet funds before a bid is registered.
* When a bidder is outbid, their hold is immediately unlocked back to their available balance.
* If a winning bidder fails to pay within 24 hours, their hold is **forfeited** as platform penalty revenue, and a second-chance offer is automatically generated for the runner-up.

### 5. Immutable Double-Entry Financial Ledger

Every financial event produces balanced debits and credits ($\sum \text{Debits} = \sum \text{Credits}$). Ledger entries and transactions enforce immutability at the model level by rejecting `save()` updates on existing IDs and completely overriding `delete()`:

* **`1000_CLEARING_CASH`** *(Asset)*: Tracks escrowed gross funds.
* **`2000_SELLER_<id>`** *(Liability)*: Net disbursements owed to verified sellers.
* **`2100_USER_WALLET`** *(Liability)*: Buyer balance commitments and pre-auth security holds.
* **`4000_PLATFORM_COMMISSION`** *(Revenue)*: 5% platform commission fees deducted upon checkout.
* **`4100_FORFEITURE_PENALTY`** *(Revenue)*: Forfeited security deposits collected from defaulting winners.

### 6. Real-Time Network & Protocol Engineering

* **Redis RESP2 Protocol Enforcement:** Configured `channels_redis` with explicit RESP2 parameters (`?protocol=2`) and a `socket_timeout: 30` setting. This eliminates blocking-read race conditions (`BZPOPMIN`) present in `redis-py` 5.x on newer Python runtimes.
* **Token-Bucket Rate Limiting:** Lua scripts running directly inside Redis enforce a burst limit of 3 bids and a sustained replenishment rate of 1 token/sec per user.
* **Dual-Channel WebSocket Routing:** Separates public group broadcasts (standing price advances, viewer presence counters) from private user alerts (`f"user_{user.id}"` for outbid warnings and second-chance offers).

---

## State Transition Lifecycles

### Auction & Order Settlement Flow

```mermaid
stateDiagram-v2
    [*] --> ACTIVE: Listing Created
    ACTIVE --> CLOSED: ends_at Reached (Reserve Met / Buy It Now)
    ACTIVE --> UNSOLD: ends_at Reached (Reserve Not Met / No Bids)
    ACTIVE --> CANCELLED: Seller Cancels (Zero Bids Only)

    CLOSED --> PENDING_PAYMENT: Order Created (Winner Assigned)

    state PENDING_PAYMENT {
        [*] --> CheckoutWindow: 24h Countdown
        CheckoutWindow --> OrderPaid: Payment Settled
        CheckoutWindow --> OrderExpired: Window Lapses
    }

    OrderPaid --> PAID: Deposit Captured + Ledger Settlement Posted (5% Fee, 95% Seller Net)
    OrderExpired --> EXPIRED: Deposit Forfeited to Platform Revenue

    state SecondChanceWorkflow {
        EXPIRED --> EvaluateRunnerUp: Check runner-up proxy budget
        EvaluateRunnerUp --> OfferPending: Runner-up meets reserve
        EvaluateRunnerUp --> UnsoldState: No eligible bids
        
        OfferPending --> OfferAccepted: Accepted within 24h
        OfferPending --> OfferDeclined: Declined or Expired
    }

    OfferAccepted --> PENDING_PAYMENT: New Order for Runner-up
    OfferDeclined --> UNSOLD
    UnsoldState --> UNSOLD

    PAID --> [*]
    UNSOLD --> [*]

```

---

## API & WebSocket Reference

### HTTP Endpoints

| Method | Endpoint | Description | Auth Required |
| --- | --- | --- | --- |
| `POST` | `/api/token/` | Obtain JWT access and refresh token pair | No |
| `GET` | `/api/wallet/` | Read balance, held balance, and available funds | Yes |
| `POST` | `/api/wallet/topup/` | Add test funds to simulated wallet | Yes |
| `GET` | `/api/auctions/` | List and search active auctions (paginated) | No |
| `GET` | `/api/auctions/{id}/` | Get auction details & calculated `next_minimum_bid` | No |
| `POST` | `/api/auctions/{id}/proxy-bid/` | Submit/raise proxy budget (allocates 10% hold) | Yes |
| `POST` | `/api/auctions/{id}/buy-it-now/` | Instantly purchase item at Buy-It-Now price | Yes |
| `POST` | `/api/orders/{id}/settle/` | Settle payment; captures hold & writes ledger entries | Yes |
| `POST` | `/api/auctions/{id}/watch/` | Toggle auction watch state | Yes |
| `GET` | `/api/seller/earnings/` | Inspect ledger balance, gross GMV, and disbursements | Yes |
| `POST` | `/api/seller/payout/` | Disburse earned seller funds | Yes |

### WebSocket Interface

**Endpoint:** `ws://127.0.0.1:8000/ws/auctions/{auction_id}/?token={jwt_access_token}`

#### Inbound Frame (Client $\rightarrow$ Server)

```json
{
  "action": "place_bid",
  "max_amount": "500.00"
}

```

#### Outbound Frames (Server $\rightarrow$ Client)

* **Public Room Broadcast (`new_bid`):**
```json
{
  "type": "new_bid",
  "data": {
    "auction_id": "8cd91cfc-12e0-494f-aa17-643f477826bc",
    "amount": "155.00",
    "highest_bidder": "b***b",
    "ends_at": "2026-10-14T14:32:00Z",
    "extended": true
  }
}

```


* **Targeted Outbid Alert (`targeted_outbid_alert`):**
```json
{
  "type": "targeted_outbid_alert",
  "data": {
    "auction_id": "8cd91cfc-12e0-494f-aa17-643f477826bc",
    "auction_title": "Rolex Submariner 1972",
    "new_price": "155.00",
    "message": "You have been outbid on 'Rolex Submariner 1972'. Current price: $155.00."
  }
}

```



---

## Automated Testing

The platform includes an extensive automated test suite covering race conditions, outbid scenarios, financial ledger math, and async WebSocket routing:

```powershell
uv run python manage.py test auctions --keepdb

```

```text
Found 65 test(s).
Using existing test database for alias 'default'...
System check identified no issues (0 silenced).
.................................................................
-----------------------------------------------------------------
Ran 65 tests in 3.124s

OK
Preserving test database for alias 'default'...

```

### Test Coverage Breakdown

* **`test_services.py` (12 tests):** Proxy bidding calculation, anti-sniping window extensions, Buy-It-Now execution, second-chance lifecycle.
* **`test_views.py` (18 tests):** REST API endpoints, JWT authentication checks, pagination, seller listing restrictions, anti-shill protections.
* **`test_consumers.py` (5 tests):** Async WebSocket connection handshakes, viewer presence tracking, token-bucket rate limiting, targeted outbid alerts.
* **`test_deposits.py` (5 tests):** 10% pre-auth hold freezing, outbid releases, winner capture, default forfeitures.
* **`test_ledger.py` (6 tests):** Mathematical balance verification ($\sum \text{Debits} = \sum \text{Credits}$), entry immutability checks, seller payout deductions.
* **`test_tasks.py` (5 tests):** Celery background task automation for closing expired auctions, expiring unpaid orders, and second-chance offer delivery.
* **`test_increments.py` (4 tests):** Dynamic tiered bid increment transitions across monetary brackets.
* **`test_watchlist.py` (5 tests):** Watchlist subscription management and 15-minute countdown alert generation.
* **`test_models.py` (5 tests):** Database models, property calculations, string representations.

---

## Quickstart & Local Setup

### 1. Prerequisites

* Python 3.13+
* [`uv`](https://www.google.com/search?q=https://github.com/astral-sh/uv) (Astral Python package manager)
* PostgreSQL 15+
* Redis 7.x

### 2. Environment Configuration

Clone the repository and install dependencies using `uv`:

```powershell
git clone https://github.com/taizeem/auction.git
cd live_auction
uv sync

```

Create a `.env` file in the project root:

```env
DEBUG=True
SECRET_KEY=your-secure-django-secret-key
DATABASE_URL=postgres://postgres:password@127.0.0.1:5432/auction_db
REDIS_URL=redis://127.0.0.1:6379/0
CELERY_BROKER_URL=redis://127.0.0.1:6379/1

```

### 3. Database Initialization

```powershell
uv run python manage.py makemigrations auctions
uv run python manage.py migrate

```

### 4. Running the Multi-Process Services

#### Terminal 1: Daphne ASGI Web Server

```powershell
uv run python manage.py runserver

```

#### Terminal 2: Celery Worker

```powershell
# Pass --pool=solo on Windows environments
uv run celery -A config worker -l info --pool=solo

```

#### Terminal 3: Celery Beat Scheduler

```powershell
uv run celery -A config beat -l info

```

---

## Technical Stack Summary

* **Backend Framework:** Django 6.0, Django REST Framework 3.15
* **Real-Time Layer:** Django Channels 4.2, Daphne 4.2, `channels-redis` 4.2
* **Database & ORM:** PostgreSQL 16 (with pessimistic row-level locking via `SELECT FOR UPDATE`)
* **Caching & Message Broker:** Redis 7.x (RESP2 Protocol, Lua Scripting)
* **Task Queue & Scheduler:** Celery 5.4, Celery Beat
* **Authentication:** `djangorestframework-simplejwt`
* **Dependency Management:** `uv` by Astral