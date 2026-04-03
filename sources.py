"""
apex/sources.py — Data source definitions
==========================================
Every data source is a class that inherits from BaseSource.
Each source runs as its own async task in the event loop.
When it has something new, it puts a dict onto the shared queue.

The dict always has at minimum:
    source  : str   — matches the "SOURCE" column in your taxonomy
    text    : str   — human-readable description of the item
    ts      : str   — ISO timestamp

Sources can add extra fields (e.g. actual_mmb for EIA) that the
classifier knows how to read.

TO ADD A NEW SOURCE: copy OpecRssSource, change the URL and parse logic.
"""

import asyncio
import datetime
import logging
import json
import os
from abc import ABC, abstractmethod

import aiohttp
import feedparser

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# BASE CLASS — all sources inherit from this
# ═══════════════════════════════════════════════════════

class BaseSource(ABC):
    """
    Abstract base for every data source.
    Subclasses implement poll() and set name + interval_seconds.

    DEDUP RULES (fixing the core data quality problem):
    - _already_seen(key) returns True if the EXACT key was seen before.
    - Keys NEVER expire. Same data = same key = never re-emitted.
    - This is correct: the RL model needs ONE signal per value change,
      not 80 copies of "funding rate = -50.8%" every minute.
    - For RSS feeds: key = article URL/ID (naturally unique per item)
    - For numeric data: key = rounded value (emits on meaningful change)
    - Price feed (KrakenPrice): uses special logic — emits on price change
    """
    name: str = "Unknown"
    interval_seconds: float = 60.0

    # REMOVED: always_emit and seen_ttl — these caused the noise flooding.
    # Dedup is now permanent per key. Sources that need to re-emit
    # should construct keys that include the changing value.

    _shared_session: 'aiohttp.ClientSession | None' = None

    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
        self._seen: set[str] = set()
        self._poll_count = 0
        self._newest_pub_ts: float = 0  # track newest publication timestamp per source

    @classmethod
    def set_shared_session(cls, session: 'aiohttp.ClientSession') -> None:
        cls._shared_session = session

    def get_session(self) -> 'aiohttp.ClientSession':
        if self._shared_session is not None:
            return self._shared_session
        return aiohttp.ClientSession()

    def _now(self) -> str:
        return datetime.datetime.utcnow().isoformat()

    def _already_seen(self, key: str) -> bool:
        """
        Permanent dedup. Once a key is seen, it's never emitted again.
        This is correct for: RSS articles, filing IDs, unique events.

        For numeric data that changes: include the VALUE in the key
        so a new value = new key = new emission. Example:
          key = f"kraken-BTC-{price:.0f}"  → emits on $1 change
          key = f"funding-BTC-{rate:.4f}"  → emits on 0.01% change
        """
        if key in self._seen:
            return True
        self._seen.add(key)
        # Trim to prevent unbounded growth — keep last 2000
        if len(self._seen) > 2000:
            # Remove oldest (sets are unordered, but this prevents OOM)
            excess = len(self._seen) - 1500
            for _ in range(excess):
                self._seen.pop()
        return False

    def _is_fresh_rss(self, entry) -> bool:
        """Check if an RSS entry is newer than the last item we processed.
        Prevents archive re-ingestion that floods the stream with old data."""
        import time as _time
        import calendar

        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        if pub:
            try:
                entry_ts = calendar.timegm(pub)
                # Skip anything older than 48 hours
                if entry_ts < _time.time() - 172800:
                    return False
                # Skip if we've already seen newer items
                if entry_ts <= self._newest_pub_ts:
                    return False
                self._newest_pub_ts = max(self._newest_pub_ts, entry_ts)
                return True
            except (ValueError, OverflowError, TypeError):
                pass

        # No pub date — allow through but still deduplicate by key
        return True

    async def emit(self, item: dict) -> None:
        """Put a new item on the shared queue. Non-blocking."""
        item["ts"] = item.get("ts", self._now())
        item["source"] = self.name
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(item)
            except asyncio.QueueEmpty:
                pass

    @abstractmethod
    async def poll(self) -> None:
        ...

    async def run(self) -> None:
        log.info("[%s] source started (interval: %ss)", self.name, self.interval_seconds)
        import random
        await asyncio.sleep(random.uniform(0, min(3, self.interval_seconds)))
        while True:
            self._poll_count += 1
            try:
                await self.poll()
            except Exception as exc:
                log.warning("[%s] poll error: %s", self.name, exc)
            await asyncio.sleep(self.interval_seconds)


# ═══════════════════════════════════════════════════════
# EIA PETROLEUM SOURCE
# Endpoint: api.eia.gov/v2/petroleum
# Scheduled: Wednesday 10:30 AM ET
# Classifier: keyword path → E001 (draw) or E002 (build)
# ═══════════════════════════════════════════════════════

class EiaPetroleumSource(BaseSource):
    """
    Polls the EIA API for weekly crude inventory data.
    The EIA API returns structured JSON — no NLP needed.
    The classifier receives actual_mmb and consensus_mmb as numbers.

    FREE API. Get a key at: https://www.eia.gov/opendata/
    Set your key in the EIA_API_KEY environment variable.

    NOTE: EIA publishes Wednesdays at 10:30 AM ET.
    Polling every 60s is fine — we deduplicate by report date.
    """
    name = "EIA Petroleum"
    interval_seconds = 60.0

    # Weekly crude inventory series ID
    SERIES_ID = "PET.WCRSTUS1.W"
    API_URL = "https://api.eia.gov/v2/seriesid/{series}?api_key={key}&length=2"

    def __init__(self, queue: asyncio.Queue, api_key: str):
        super().__init__(queue)
        self.api_key = api_key

    async def poll(self) -> None:
        import os
        key = self.api_key or os.environ.get("EIA_API_KEY", "")
        if not key:
            log.warning("[EIA] No API key set. Skipping poll. Set EIA_API_KEY env var.")
            return

        url = self.API_URL.format(series=self.SERIES_ID, key=key)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.warning("[EIA] HTTP %s", resp.status)
                    return
                data = await resp.json()

        # EIA returns the two most recent periods
        try:
            rows = data["response"]["data"]
            latest = rows[0]
            prior  = rows[1]
        except (KeyError, IndexError):
            log.warning("[EIA] Unexpected response structure")
            return

        period = latest.get("period", "unknown")
        if self._already_seen(period):
            return   # Already sent this week's report

        actual_mmb    = float(latest.get("value", 0)) / 1000   # Convert to million barrels
        prior_mmb     = float(prior.get("value", 0)) / 1000
        # EIA doesn't publish consensus — you need a Bloomberg/Reuters feed for that.
        # For Phase 0 we use week-over-week change as a proxy.
        # When you have a consensus feed, swap prior_mmb for the actual estimate.
        consensus_mmb = prior_mmb * 0.98   # Rough proxy: market expects ~2% seasonal draw

        delta = actual_mmb - consensus_mmb

        await self.emit({
            "text":          f"EIA weekly crude inventory: {actual_mmb:.1f}M bbl "
                             f"(prior {prior_mmb:.1f}M, implied consensus {consensus_mmb:.1f}M, delta {delta:+.1f}M)",
            "actual_mmb":    actual_mmb,
            "consensus_mmb": consensus_mmb,
            "period":        period,
            "extra_json":    json.dumps({"period": period, "actual": actual_mmb,
                                         "consensus": consensus_mmb}),
        })


# ═══════════════════════════════════════════════════════
# OPEC RSS SOURCE
# Feed: opec.org/opec_web/en/press_room/204.htm
# Scheduled: Event-driven (no fixed schedule)
# Classifier: keyword path → E037, E038, E039, E040
# ═══════════════════════════════════════════════════════

class OpecRssSource(BaseSource):
    """
    Polls the OPEC official press room RSS feed.
    Sends every new item to the classifier — it decides if it's tradeable.

    FREE. No key required.
    """
    name = "OPEC RSS"
    interval_seconds = 86400.0   # Blocked — daily poll to avoid wasted requests

    RSS_URL = "https://www.opec.org/opec_web/en/press_room/204.htm"

    async def poll(self) -> None:
        # feedparser is synchronous — run it in a thread so it doesn't block the loop
        loop = asyncio.get_event_loop()
        feed = await loop.run_in_executor(None, feedparser.parse, self.RSS_URL)

        for entry in feed.entries:
            if not self._is_fresh_rss(entry):
                continue
            uid = entry.get("id") or entry.get("link") or entry.get("title", "")
            if self._already_seen(uid):
                continue

            title   = entry.get("title", "")
            summary = entry.get("summary", "")
            text    = f"{title}. {summary}".strip()

            await self.emit({
                "text":       text,
                "title":      title,
                "link":       entry.get("link", ""),
                "extra_json": json.dumps({"uid": uid, "title": title}),
            })


# ═══════════════════════════════════════════════════════
# FED RSS SOURCE
# Feed: federalreserve.gov/feeds/feeds.htm (multiple feeds)
# Scheduled: Event-driven
# Classifier: keyword path → E003, E004, E024–E028
# ═══════════════════════════════════════════════════════

class FedRssSource(BaseSource):
    """
    Polls the Federal Reserve press release RSS feed.
    FREE. No key required.
    """
    name = "Fed RSS"
    interval_seconds = 15.0   # Fed announcements are the highest-magnitude events

    RSS_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"

    async def poll(self) -> None:
        loop = asyncio.get_event_loop()
        feed = await loop.run_in_executor(None, feedparser.parse, self.RSS_URL)

        for entry in feed.entries:
            if not self._is_fresh_rss(entry):
                continue
            uid = entry.get("id") or entry.get("link", "")
            if self._already_seen(uid):
                continue

            title   = entry.get("title", "")
            summary = entry.get("summary", "")
            text    = f"{title}. {summary}".strip()

            await self.emit({
                "text":       text,
                "title":      title,
                "link":       entry.get("link", ""),
                "extra_json": json.dumps({"uid": uid}),
            })



# ═══════════════════════════════════════════════════════
# FEDERAL REGISTER RSS (EPA, Financial Regulation, Crypto Rules)
# Feed: Federal Register RSS feeds
# Scheduled: Event-driven (varies by agency)
# Classifier: keyword path → E074–E076
# ═══════════════════════════════════════════════════════

class FederalRegisterSource(BaseSource):
    """
    Polls the Federal Register for emergency rules, final rules affecting:
      - EPA (oil/gas drilling restrictions)
      - Financial Regulation (banking capital requirements)
      - Crypto (SEC/FinCEN guidance, proposed regulations)
    
    FREE. No API key required.
    Feed: https://www.federalregister.gov/api/v1/
    """
    name = "Federal Register"
    interval_seconds = 60.0  # Poll every 60 seconds
    
    API_URL = "https://www.federalregister.gov/api/v1/documents"
    
    async def poll(self) -> None:
        """
        Query Federal Register API for recent documents matching specific agencies/keywords.
        Look for:
          - Immediate Effective Rule (effective immediately)
          - Emergency Rule designations
          - Keywords: "oil", "gas", "drilling", "crypto", "digital asset", "banking", "capital"
        """
        params = {
            "order": "-publication_date",
            "per_page": 20,
            "agencies[]": ["Environmental Protection Agency", "Federal Reserve System", "SEC"],
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        log.warning("[Federal Register] HTTP %s", resp.status)
                        return
                    data = await resp.json()
            except Exception as e:
                log.warning("[Federal Register] Error: %s", e)
                return
        
        for doc in data.get("results", []):
            doc_id = doc.get("document_number", "")
            if self._already_seen(doc_id):
                continue
            
            title = doc.get("title", "")
            agency = doc.get("agency_names", ["Unknown"])[0]
            effective_on = doc.get("effective_on", "")
            
            # Only flag items marked as "Immediate Effective" or "Emergency"
            action = doc.get("action", "")
            is_emergency = "immediate effective" in action.lower() or "emergency" in action.lower()
            is_final = "final rule" in action.lower()
            
            # Only emit if final rule or emergency designation
            if is_final or is_emergency:
                await self.emit({
                    "text": f"Federal Register - {agency}: {title} (Effective: {effective_on})",
                    "title": title,
                    "agency": agency,
                    "document_number": doc_id,
                    "effective_on": effective_on,
                    "is_emergency": is_emergency,
                    "link": f"https://www.federalregister.gov/documents/{doc['publication_date']}/{doc_id}",
                    "extra_json": json.dumps({"doc_id": doc_id, "agency": agency, "is_emergency": is_emergency}),
                })


# ═══════════════════════════════════════════════════════
# SEC PRESS RELEASES
# Feed: SEC official press releases
# Scheduled: Event-driven
# Classifier: keyword path → E080–E082
# ═══════════════════════════════════════════════════════

class SecPressReleaseSource(BaseSource):
    """
    Polls SEC press releases for:
      - Trading suspensions (E080)
      - Enforcement actions against crypto exchanges (E081)
      - New spot crypto ETF approvals (E082)
    
    FREE. No API key required.
    Feed: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany
    Alternative: RSS-like feed via SEC news
    """
    name = "SEC Enforcement"
    interval_seconds = 60.0
    
    SEC_NEWS_URL = "https://www.sec.gov/rss/news.xml"
    
    async def poll(self) -> None:
        loop = asyncio.get_event_loop()
        feed = await loop.run_in_executor(None, feedparser.parse, self.SEC_NEWS_URL)
        
        for entry in feed.entries:
            if not self._is_fresh_rss(entry):
                continue
            uid = entry.get("id") or entry.get("link", "")
            if self._already_seen(uid):
                continue
            
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            text = f"{title}. {summary}".strip()
            
            # Only emit if it mentions trading suspension, crypto enforcement, or ETF approval
            keywords = ["suspension", "trading halt", "enforcement", "crypto", "btc", "bitcoin", "etf", "approved"]
            if any(kw in text.lower() for kw in keywords):
                await self.emit({
                    "text": text,
                    "title": title,
                    "link": entry.get("link", ""),
                    "extra_json": json.dumps({"uid": uid}),
                })



# ═══════════════════════════════════════════════════════
# WHALE ALERT (Large Crypto Transfers)
# API: Whale Alert official API
# Scheduled: Real-time
# Classifier: keyword path → E061–E063
# ═══════════════════════════════════════════════════════

class WhaleAlertSource(BaseSource):
    """
    Monitors large crypto transfers (BTC, ETH, stablecoins) via Whale Alert API.
    
    Whale Alert tracks on-chain transactions >$500K across all major blockchains.
    FREE tier: limited to 1 API call/minute (~100 alerts/day)
    PAID tier: unlimited calls, real-time webhooks
    
    Get API key at: https://whale-alert.io/
    """
    name = "Whale Alert"
    interval_seconds = 60.0
    
    API_URL = "https://api.whale-alert.io/v1/transactions"
    
    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("WHALE_ALERT_KEY", "")
    
    async def poll(self) -> None:
        import os
        if not self.api_key:
            log.warning("[Whale Alert] No API key. Get one at https://whale-alert.io/")
            return
        
        params = {
            "api_key": self.api_key,
            "min_value": 500000,  # Only transactions >$500K
            "limit": 100,
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        log.warning("[Whale Alert] HTTP %s", resp.status)
                        return
                    data = await resp.json()
            except Exception as e:
                log.warning("[Whale Alert] Error: %s", e)
                return
        
        for tx in data.get("result", []):
            tx_id = tx.get("hash", "")
            if self._already_seen(tx_id):
                continue
            
            symbol = tx.get("symbol", "").upper()
            value = tx.get("value_usd", 0)
            from_addr = tx.get("from", {}).get("address", "")
            to_addr = tx.get("to", {}).get("address", "")
            from_label = tx.get("from", {}).get("owner_type", "unknown")
            to_label = tx.get("to", {}).get("owner_type", "unknown")
            
            # Detect exchange deposits (selling signal) vs withdrawals (accumulation)
            direction = "TO"
            if "exchange" in to_label.lower():
                direction = "TO_EXCHANGE (SELL SIGNAL)"
            elif "exchange" in from_label.lower():
                direction = "FROM_EXCHANGE (ACCUMULATION)"
            
            await self.emit({
                "text": f"Whale Alert: {symbol} {direction} - ${value:,.0f} "
                        f"from {from_label} to {to_label}",
                "symbol": symbol,
                "value_usd": value,
                "direction": direction,
                "tx_hash": tx_id,
                "from_label": from_label,
                "to_label": to_label,
                "extra_json": json.dumps({
                    "tx_hash": tx_id,
                    "symbol": symbol,
                    "direction": direction,
                    "value": value,
                }),
            })


# ═══════════════════════════════════════════════════════
# COINGLASS (Funding Rates, Open Interest, Liquidations)
# API: Coinglass official API
# Scheduled: Real-time (streaming preferred)
# Classifier: keyword path → E064–E067
# ═══════════════════════════════════════════════════════

class CoinglassSource(BaseSource):
    """
    Monitors perpetual futures metrics: funding rates, open interest, liquidations.
    
    Coinglass aggregates data from: Binance, Bybit, OKX, Deribit, etc.
    FREE tier: limited history + delayed data
    PAID tier: real-time, webhooks
    
    Get API key at: https://www.coinglass.com/api
    """
    name = "Coinglass"
    interval_seconds = 60.0
    
    API_URL = "https://api.coinglass.com/api/v1"
    
    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("COINGLASS_KEY", "")
        self._last_oi = {}      # Track previous OI to detect spikes
        self._last_funding = {} # Track previous funding rate
    
    async def poll(self) -> None:
        import os
        if not self.api_key:
            log.warning("[Coinglass] No API key. Free tier available at https://www.coinglass.com/api")
            return
        
        # Get funding rate data
        funding_url = f"{self.API_URL}/funding_usd_history"
        oi_url = f"{self.API_URL}/total_oi"
        
        async with aiohttp.ClientSession() as session:
            # Funding rates
            try:
                async with session.get(
                    funding_url,
                    params={"symbol": "BTC", "timeType": "4h"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("success"):
                            funding_data = data.get("data", {})
                            # Check for extreme funding rates (E065, E066)
                            for exchange, rate in funding_data.items():
                                current_rate = float(rate) if rate else 0
                                event_key = f"funding_{exchange}"
                                
                                # Extreme positive funding (longs over-leveraged) → E065
                                if current_rate > 0.001:  # >0.1% per 8hr
                                    if not self._already_seen(f"{event_key}_high_{current_rate}"):
                                        await self.emit({
                                            "text": f"Coinglass: BTC funding rate EXTREME POSITIVE on {exchange} - {current_rate*100:.3f}%/8hr (longs over-leveraged)",
                                            "exchange": exchange,
                                            "funding_rate": current_rate,
                                            "event": "E065",
                                            "extra_json": json.dumps({
                                                "exchange": exchange,
                                                "funding_rate": current_rate,
                                                "type": "extreme_positive"
                                            }),
                                        })
                                
                                # Extreme negative funding (shorts over-leveraged) → E066
                                elif current_rate < -0.0005:  # <-0.05% per 8hr
                                    if not self._already_seen(f"{event_key}_low_{current_rate}"):
                                        await self.emit({
                                            "text": f"Coinglass: BTC funding rate EXTREME NEGATIVE on {exchange} - {current_rate*100:.3f}%/8hr (shorts over-leveraged)",
                                            "exchange": exchange,
                                            "funding_rate": current_rate,
                                            "event": "E066",
                                            "extra_json": json.dumps({
                                                "exchange": exchange,
                                                "funding_rate": current_rate,
                                                "type": "extreme_negative"
                                            }),
                                        })
            except Exception as e:
                log.warning("[Coinglass] Funding rate error: %s", e)
            
            # Open Interest
            try:
                async with session.get(
                    oi_url,
                    params={"symbol": "BTC"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("success"):
                            oi_data = data.get("data", {})
                            total_oi = float(oi_data.get("totalOI", 0))
                            
                            # Track OI changes (E067: mass deleveraging)
                            last_oi = self._last_oi.get("BTC", total_oi)
                            oi_change = (last_oi - total_oi) / last_oi if last_oi > 0 else 0
                            
                            if oi_change > 0.15 and not self._already_seen("oi_drop_" + str(int(total_oi))):
                                await self.emit({
                                    "text": f"Coinglass: BTC open interest dropped {oi_change*100:.1f}% - mass deleveraging cascade",
                                    "oi_current": total_oi,
                                    "oi_prior": last_oi,
                                    "oi_change_pct": oi_change * 100,
                                    "event": "E067",
                                    "extra_json": json.dumps({
                                        "oi_current": total_oi,
                                        "oi_change_pct": oi_change * 100,
                                        "type": "mass_deleveraging"
                                    }),
                                })
                            
                            self._last_oi["BTC"] = total_oi
            except Exception as e:
                log.warning("[Coinglass] OI error: %s", e)


# ═══════════════════════════════════════════════════════
# ECB PRESS RELEASES
# Feed: ECB official press releases
# Scheduled: Event-driven
# Classifier: keyword path → E029–E031
# ═══════════════════════════════════════════════════════

class EcbRssSource(BaseSource):
    """
    Monitors ECB press releases for rate decisions, monetary policy changes.
    
    FREE. No API key required.
    """
    name = "ECB RSS"
    interval_seconds = 15.0
    
    RSS_URL = "https://www.ecb.europa.eu/rss/press.html"
    
    async def poll(self) -> None:
        loop = asyncio.get_event_loop()
        feed = await loop.run_in_executor(None, feedparser.parse, self.RSS_URL)
        
        for entry in feed.entries:
            if not self._is_fresh_rss(entry):
                continue
            uid = entry.get("id") or entry.get("link", "")
            if self._already_seen(uid):
                continue
            
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            text = f"{title}. {summary}".strip()
            
            await self.emit({
                "text": text,
                "title": title,
                "link": entry.get("link", ""),
                "extra_json": json.dumps({"uid": uid}),
            })


# ═══════════════════════════════════════════════════════
# KRAKEN PERPETUALS FUNDING RATE SOURCE
# Endpoint: futures.kraken.com/derivatives/api/v3/tickers
# Real-time: Continuous updates every 5 seconds
# Replaces: Binance (451 geo-blocked) and Bybit (403 geo-blocked) on Railway
# Classifier: numeric path → E064–E067 (extreme funding rates, OI)
# ═══════════════════════════════════════════════════════

class KrakenFundingRateSource(BaseSource):
    """
    Polls Kraken perpetuals funding rates in real-time.
    Covers BTC and ETH perps (PF_XBTUSD, PF_ETHUSD).
    Replaces Binance/Bybit which are geo-blocked from Railway US servers.

    FREE API. No key required.
    """
    name = "Kraken Funding"
    interval_seconds = 5.0

    # Kraken perp symbol → human label
    SYMBOLS = {
        "PF_XBTUSD": "BTC",
        "PF_ETHUSD": "ETH",
    }

    def __init__(self, queue: asyncio.Queue):
        super().__init__(queue)
        self._last_funding_rate = {}

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                url = "https://futures.kraken.com/derivatives/api/v3/tickers"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return

                    data = await resp.json()
                    tickers = {t["symbol"]: t for t in data.get("tickers", [])}

                    for symbol, label in self.SYMBOLS.items():
                        ticker = tickers.get(symbol)
                        if not ticker:
                            continue

                        funding_rate = float(ticker.get("fundingRate", 0))
                        rate_key = f"{symbol}_{funding_rate:.8f}"
                        if self._already_seen(rate_key):
                            continue

                        if funding_rate > 0.0005:
                            direction = "extreme_positive"
                            text = f"Kraken {label}: Extreme positive funding {funding_rate*100:.3f}% (longs over-leveraged)"
                        elif funding_rate < -0.0005:
                            direction = "extreme_negative"
                            text = f"Kraken {label}: Extreme negative funding {funding_rate*100:.3f}% (shorts over-leveraged)"
                        else:
                            direction = "normal"
                            text = f"Kraken {label}: Funding rate {funding_rate*100:.4f}%"

                        await self.emit({
                            "text": text,
                            "symbol": symbol,
                            "funding_rate": funding_rate,
                            "direction": direction,
                            "extra_json": json.dumps({
                                "symbol": symbol,
                                "funding_rate": funding_rate,
                                "type": direction
                            })
                        })

        except Exception as e:
            log.warning("[Kraken Funding] Error: %s", e)


# ═══════════════════════════════════════════════════════
# OKX PERPETUALS FUNDING RATE SOURCE
# Real-time: 5 second polling
# ═══════════════════════════════════════════════════════

class OkxFundingRateSource(BaseSource):
    """OKX perpetuals funding rates"""
    name = "OKX Funding"
    interval_seconds = 5.0
    
    def __init__(self, queue: asyncio.Queue):
        super().__init__(queue)
        self._last_funding_rate = {}
    
    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
                
                for symbol in symbols:
                    url = f"https://www.okx.com/api/v5/public/funding-rate?instId={symbol}"
                    
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status != 200:
                            continue
                        
                        data = await resp.json()
                        if not data.get("data"):
                            continue
                        
                        latest = data["data"][0]
                        funding_rate = float(latest.get("fundingRate", 0))
                        
                        rate_key = f"{symbol}_{funding_rate:.6f}"
                        if self._already_seen(rate_key):
                            continue
                        
                        await self.emit({
                            "text": f"OKX {symbol}: Funding {funding_rate*100:.4f}%",
                            "symbol": symbol,
                            "funding_rate": funding_rate,
                            "extra_json": json.dumps({"symbol": symbol, "funding_rate": funding_rate})
                        })
        except Exception as e:
            log.warning("[OKX Funding] Error: %s", e)


# ═══════════════════════════════════════════════════════
# BLOCKCHAIN.COM WHALE TRANSFERS
# Real-time: Monitor large BTC transfers
# ═══════════════════════════════════════════════════════

class BlockchainComSource(BaseSource):
    """Monitor large BTC transfers in real-time"""
    name = "Blockchain.com"
    interval_seconds = 10.0
    
    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                url = "https://blockchain.info/unconfirmed-transactions?format=json"
                
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return
                    
                    data = await resp.json()
                    for tx in data.get("txs", [])[:10]:  # Check top 10
                        tx_id = tx.get("hash", "")
                        
                        if self._already_seen(tx_id):
                            continue
                        
                        value_btc = sum(o.get("value", 0) for o in tx.get("out", [])) / 1e8

                        if value_btc > 1:  # >1 BTC transfers
                            await self.emit({
                                "text": f"Blockchain: Large BTC transfer {value_btc:.2f} BTC (~${value_btc*40000:.0f})",
                                "value_btc": value_btc,
                                "tx_hash": tx_id,
                                "extra_json": json.dumps({"value_btc": value_btc, "tx": tx_id})
                            })
        except Exception as e:
            log.warning("[Blockchain.com] Error: %s", e)


# ═══════════════════════════════════════════════════════
# ETHERSCAN ON-CHAIN DATA SOURCE
# API: https://api.etherscan.io/
# Scheduled: Every 10 seconds
# Classifier: keyword path → E061–E063 (whale transfers), E072–E073 (stablecoin minting)
# ═══════════════════════════════════════════════════════

class EtherscanSource(BaseSource):
    """
    Monitors Ethereum on-chain data via Etherscan API.
    Tracks:
      - Large ETH transfers (whale transfers)
      - USDC/USDT minting activity (stablecoin supply changes)
    
    FREE API key available at https://etherscan.io/apis
    Set your key in the ETHERSCAN_API_KEY environment variable.
    
    Events:
      - E061–E063: Whale ETH transfers (>100 ETH)
      - E072–E073: Stablecoin minting (USDC/USDT >1M)
    """
    name = "Etherscan"
    interval_seconds = 10.0

    ETHERSCAN_API_URL = "https://api.etherscan.io/api"

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("ETHERSCAN_API_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return  # silent — no key configured

        try:
            async with aiohttp.ClientSession() as session:
                # Gas tracker — always returns data, good heartbeat
                params = {"module": "gastracker", "action": "gasoracle", "apikey": self.api_key}
                async with session.get(self.ETHERSCAN_API_URL, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                if data.get("status") == "1":
                    result = data.get("result", {})
                    fast = result.get("FastGasPrice", "")
                    safe = result.get("SafeGasPrice", "")
                    base = result.get("suggestBaseFee", "")

                    key = f"etherscan-gas-{fast}-{safe}"
                    if not self._already_seen(key):
                        await self.emit({
                            "text": f"Etherscan: ETH gas — fast {fast} gwei, safe {safe} gwei, base {base} gwei",
                            "fast_gwei": fast,
                            "safe_gwei": safe,
                            "base_fee": base,
                            "extra_json": json.dumps({"fast": fast, "safe": safe, "base": base}),
                        })

                # ETH supply stats
                params2 = {"module": "stats", "action": "ethsupply2", "apikey": self.api_key}
                async with session.get(self.ETHERSCAN_API_URL, params=params2, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return
                    data2 = await resp.json()

                if data2.get("status") == "1":
                    result = data2.get("result", {})
                    supply = float(result.get("EthSupply", 0)) / 1e18
                    staking = float(result.get("Eth2Staking", 0)) / 1e18
                    burnt = float(result.get("BurntFees", 0)) / 1e18

                    key2 = f"etherscan-supply-{int(supply)}"
                    if not self._already_seen(key2):
                        await self.emit({
                            "text": f"Etherscan: ETH supply {supply:,.0f}, staked {staking:,.0f}, burnt {burnt:,.0f}",
                            "eth_supply": supply,
                            "eth_staked": staking,
                            "eth_burnt": burnt,
                            "extra_json": json.dumps({"supply": supply, "staked": staking, "burnt": burnt}),
                        })

        except Exception as e:
            log.warning("[Etherscan] Error: %s", e)


# ═══════════════════════════════════════════════════════
# COINGECKO ON-CHAIN METRICS SOURCE
# API: https://api.coingecko.com/api/v3/
# Scheduled: Every 5 minutes
# Classifier: keyword path → E087–E090 (miner activity, exchange reserves)
# ═══════════════════════════════════════════════════════

class CoinGeckoSource(BaseSource):
    """
    Monitors on-chain metrics via CoinGecko API.
    FREE. No API key required (uses public endpoint).
    
    Tracks:
      - Exchange reserve changes (inflow/outflow)
      - Miner activity (BTC/ETH miner revenue)
      - Network transaction volume
    
    Events:
      - E087: Exchange inflow surge (>$100M daily)
      - E088: Exchange outflow surge (>$100M daily)
      - E089: Miner revenue spike (>50% increase)
      - E090: Network transaction volume surge
    """
    name = "CoinGecko"
    interval_seconds = 60.0  # Every 5 minutes

    COINGECKO_API_URL = "https://api.coingecko.com/api/v3"

    def __init__(self, queue: asyncio.Queue):
        super().__init__(queue)
        self.last_btc_miner_revenue = None
        self.last_eth_miner_revenue = None

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                # Get global data
                url = f"{self.COINGECKO_API_URL}/global"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        log.warning("[CoinGecko] HTTP %s", resp.status)
                        return
                    global_data = await resp.json()
        except Exception as exc:
            log.warning("[CoinGecko] poll error: %s", exc)
            return

        try:
            data = global_data.get("data", {})
            
            # Monitor market cap changes (proxy for volume/activity)
            btc_mcap = float(data.get("total_market_cap", {}).get("btc", 0))
            eth_mcap = float(data.get("total_market_cap", {}).get("eth", 0))

            if btc_mcap > 0:
                key = f"coingecko-mcap-btc-{btc_mcap:.0f}"
                if not self._already_seen(key):
                    # Check for significant market cap changes (>5% in 5 min = ~60% daily)
                    await self.emit({
                        "text": f"CoinGecko: BTC market cap at {btc_mcap:.0f} BTC",
                        "btc_market_cap": btc_mcap,
                        "extra_json": json.dumps({"btc_mcap": btc_mcap, "source": "coingecko"})
                    })

            if eth_mcap > 0:
                key = f"coingecko-mcap-eth-{eth_mcap:.0f}"
                if not self._already_seen(key):
                    await self.emit({
                        "text": f"CoinGecko: ETH market cap at {eth_mcap:.0f} ETH",
                        "eth_market_cap": eth_mcap,
                        "extra_json": json.dumps({"eth_mcap": eth_mcap, "source": "coingecko"})
                    })

            # Monitor dominance (BTC & ETH share of total market)
            btc_dominance = float(data.get("btc_market_cap_percentage", 0))
            eth_dominance = float(data.get("eth_market_cap_percentage", 0))

            if btc_dominance > 0:
                key = f"coingecko-dominance-btc-{btc_dominance:.2f}"
                if not self._already_seen(key):
                    await self.emit({
                        "text": f"CoinGecko: BTC dominance {btc_dominance:.2f}%",
                        "btc_dominance": btc_dominance,
                        "extra_json": json.dumps({"btc_dominance": btc_dominance})
                    })

        except Exception as e:
            log.warning("[CoinGecko] parse error: %s", e)


# ═══════════════════════════════════════════════════════
# BLS ECONOMIC DATA (REAL)
# Endpoint: api.bls.gov/publicAPI/v2/timeseries/data/
# Series: CUUR0000SA0 (CPI-U), CES0000000001 (Total NFP),
#         WPUFD49104 (PPI Final Demand), CES0500000003 (Avg Hourly Earnings)
# FREE with key (500 req/day). Without key: 25 req/day.
# Classifier: keyword path → E032–E036
# ═══════════════════════════════════════════════════════

class BlsSource(BaseSource):
    """
    Polls BLS API v2 for latest economic data releases.
    Emits when new data point appears for CPI, NFP, PPI, or wages.
    FREE. Key optional but recommended (25 → 500 req/day).
    """
    name = "BLS"
    interval_seconds = 600.0  # every 30 min (data releases monthly)

    # BLS series IDs
    SERIES = {
        "CUUR0000SA0":    ("CPI-U (all urban consumers)",    "CPI"),
        "CES0000000001":  ("Total Nonfarm Payrolls (NFP)",   "NFP"),
        "WPUFD49104":     ("PPI Final Demand",               "PPI"),
        "CES0500000003":  ("Average Hourly Earnings",        "WAGE"),
    }

    API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("BLS_API_KEY", "")
        self._last_values = {}  # series_id -> (year, period, value)

    async def poll(self) -> None:
        try:
            payload = {
                "seriesid": list(self.SERIES.keys()),
                "latest": True,
            }
            if self.api_key:
                payload["registrationkey"] = self.api_key

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.API_URL,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        log.warning("[BLS] HTTP %s", resp.status)
                        return

                    data = await resp.json()

                results = data.get("Results", {}).get("series", [])
                for series in results:
                    sid = series.get("seriesID", "")
                    points = series.get("data", [])
                    if not points or sid not in self.SERIES:
                        continue

                    latest = points[0]  # most recent
                    year = latest.get("year", "")
                    period = latest.get("period", "")
                    value = latest.get("value", "")

                    key = f"{sid}-{year}-{period}-{value}"
                    if self._already_seen(key):
                        continue

                    label, short = self.SERIES[sid]
                    prev = self._last_values.get(sid)
                    change_text = ""
                    if prev:
                        try:
                            delta = float(value) - float(prev[2])
                            change_text = f" (change: {delta:+.1f})"
                        except ValueError:
                            pass
                    self._last_values[sid] = (year, period, value)

                    await self.emit({
                        "text": f"BLS {short}: {label} = {value} ({year} {period}){change_text}",
                        "series_id": sid,
                        "series_name": label,
                        "value": value,
                        "year": year,
                        "period": period,
                        "extra_json": json.dumps({
                            "series_id": sid,
                            "value": value,
                            "year": year,
                            "period": period,
                        }),
                    })

        except Exception as e:
            log.warning("[BLS] Error: %s", e)


# ═══════════════════════════════════════════════════════
# FDA MEDWATCH SOURCE (REAL)
# Feed: FDA Safety Recalls RSS + openFDA drug enforcement API
# FREE. No API key required.
# Classifier: keyword path → E046–E050
# ═══════════════════════════════════════════════════════

class FdaMedwatchSource(BaseSource):
    """
    Polls FDA recall/safety RSS feed and openFDA enforcement API.
    Emits real drug recalls, withdrawals, and safety alerts.
    FREE. No API key required.
    """
    name = "FDA MedWatch"
    interval_seconds = 300.0  # every 15 min

    RECALL_RSS = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/recalls/rss.xml"
    ENFORCEMENT_API = "https://api.fda.gov/drug/enforcement.json?sort=report_date:desc&limit=5"

    async def poll(self) -> None:
        # ── RSS feed for recalls/safety alerts ──
        try:
            loop = asyncio.get_event_loop()
            feed = await loop.run_in_executor(None, feedparser.parse, self.RECALL_RSS)

            for entry in feed.entries[:10]:
                if not self._is_fresh_rss(entry):
                    continue
                uid = entry.get("id") or entry.get("link", "")
                if self._already_seen(uid):
                    continue

                title = entry.get("title", "")
                summary = entry.get("summary", "")
                text = f"{title}. {summary}".strip()

                await self.emit({
                    "text": f"FDA Recall: {text[:200]}",
                    "title": title,
                    "link": entry.get("link", ""),
                    "extra_json": json.dumps({"uid": uid, "source": "fda_rss"}),
                })
        except Exception as e:
            log.warning("[FDA MedWatch] RSS error: %s", e)

        # ── openFDA enforcement API for drug enforcement actions ──
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.ENFORCEMENT_API,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return

                    data = await resp.json()
                    for result in data.get("results", []):
                        recall_id = result.get("recall_number", "")
                        if not recall_id or self._already_seen(recall_id):
                            continue

                        reason = result.get("reason_for_recall", "")
                        product = result.get("product_description", "")[:100]
                        classification = result.get("classification", "")
                        status = result.get("status", "")

                        await self.emit({
                            "text": f"FDA Enforcement: {classification} — {reason[:150]}",
                            "recall_number": recall_id,
                            "product": product,
                            "classification": classification,
                            "status": status,
                            "extra_json": json.dumps({
                                "recall_number": recall_id,
                                "classification": classification,
                                "status": status,
                                "source": "openfda",
                            }),
                        })
        except Exception as e:
            log.warning("[FDA MedWatch] API error: %s", e)


# ═══════════════════════════════════════════════════════
# SEC ENFORCEMENT SOURCE (REAL)
# Feeds: SEC Litigation Releases RSS + Press Releases RSS
# FREE. No API key required.
# Classifier: keyword path → E051–E055
# ═══════════════════════════════════════════════════════

class SecEnforcementSource(BaseSource):
    """
    Polls SEC litigation releases and press releases RSS.
    Emits real enforcement actions, trading halts, charges.
    FREE. No API key required.
    """
    name = "SEC Enforcement"
    interval_seconds = 300.0  # every 20 min

    FEEDS = {
        "litigation": "https://www.sec.gov/rss/litigation/litreleases.xml",
        "press":      "https://www.sec.gov/news/pressreleases.rss",
    }

    async def poll(self) -> None:
        loop = asyncio.get_event_loop()

        for feed_name, url in self.FEEDS.items():
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, url)

                for entry in feed.entries[:10]:
                    if not self._is_fresh_rss(entry):
                        continue
                    uid = entry.get("id") or entry.get("link", "")
                    if self._already_seen(uid):
                        continue

                    title = entry.get("title", "")
                    summary = entry.get("summary", "")
                    text = f"{title}. {summary}".strip()

                    await self.emit({
                        "text": f"SEC {feed_name}: {text[:200]}",
                        "title": title,
                        "link": entry.get("link", ""),
                        "feed": feed_name,
                        "extra_json": json.dumps({
                            "uid": uid,
                            "feed": feed_name,
                        }),
                    })
            except Exception as e:
                log.warning("[SEC Enforcement] %s feed error: %s", feed_name, e)


# ═══════════════════════════════════════════════════════
# NOAA SPACE WEATHER SOURCE (REAL)
# Endpoint: services.swpc.noaa.gov/json/planetary_k_index_1m.json
# FREE, no API key. Returns latest Kp index readings.
# Kp >= 5 -> G1 storm, Kp >= 7 -> G3, Kp >= 8 -> G4, Kp >= 9 -> G5
# Classifier: keyword path -> E085-E086
# ═══════════════════════════════════════════════════════

class NoaaSpaceWeatherSource(BaseSource):
    """
    Polls NOAA SWPC for real Kp-index data (geomagnetic activity).
    Emits events when Kp reaches storm thresholds.
    FREE. No API key required.
    """
    name = "NOAA Space Weather"
    interval_seconds = 300.0  # every 15 min

    KP_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"

    THRESHOLDS = [
        (9, "G5 Extreme"),
        (8, "G4 Severe"),
        (7, "G3 Strong"),
        (6, "G2 Moderate"),
        (5, "G1 Minor"),
    ]

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.KP_URL,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return

                    data = await resp.json()
                    if not data:
                        return

                    latest = data[-1]
                    kp = float(latest.get("kp_index", 0))
                    ts = latest.get("time_tag", "")

                    key = f"kp-{ts}-{kp}"
                    if self._already_seen(key):
                        return

                    level = None
                    for threshold, label in self.THRESHOLDS:
                        if kp >= threshold:
                            level = label
                            break

                    if level:
                        await self.emit({
                            "text": f"NOAA: Kp={kp:.1f} - {level} geomagnetic storm",
                            "kp_index": kp,
                            "storm_level": level,
                            "extra_json": json.dumps({
                                "kp_index": kp,
                                "storm_level": level,
                                "time_tag": ts,
                            }),
                        })
                    else:
                        await self.emit({
                            "text": f"NOAA: Kp={kp:.1f} - quiet geomagnetic conditions",
                            "kp_index": kp,
                            "storm_level": "quiet",
                            "extra_json": json.dumps({
                                "kp_index": kp,
                                "time_tag": ts,
                            }),
                        })

        except Exception as e:
            log.warning("[NOAA Space Weather] Error: %s", e)


# ═══════════════════════════════════════════════════════
# SEC EDGAR FULL-TEXT FILINGS (REAL)
# Endpoint: efts.sec.gov/LATEST/search-index?dateRange=custom
# FREE, no API key. Returns recent 8-K, 10-K, 10-Q filings.
# Classifier: keyword path → E048–E053
# ═══════════════════════════════════════════════════════

class EdgarFilingSource(BaseSource):
    """
    Polls SEC EDGAR full-text search for recent 8-K and 10-K filings.
    Uses the free EFTS search endpoint — no key needed.
    """
    name = "EDGAR Filings"
    interval_seconds = 60.0  # every 2 min

    SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
    FULL_TEXT_URL = "https://efts.sec.gov/LATEST/search-index"

    # We use the newer EDGAR full-text search API
    EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

    async def poll(self) -> None:
        try:
            # EDGAR full-text search for recent 8-K filings
            params = {
                "q": "\"8-K\"",
                "dateRange": "custom",
                "startdt": (datetime.datetime.utcnow() - datetime.timedelta(hours=2)).strftime("%Y-%m-%d"),
                "enddt": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
                "forms": "8-K",
            }
            headers = {
                "User-Agent": "APEX Signal Trader research@apex.local",
                "Accept": "application/json",
            }

            async with aiohttp.ClientSession() as session:
                # Use the EDGAR full-text search API
                url = "https://efts.sec.gov/LATEST/search-index"
                async with session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        # Fallback: use the EDGAR company search RSS
                        await self._poll_rss(session, headers)
                        return

                    data = await resp.json()
                    for hit in data.get("hits", {}).get("hits", [])[:15]:
                        source = hit.get("_source", {})
                        filing_id = hit.get("_id", "")

                        if self._already_seen(filing_id):
                            continue

                        company = source.get("display_names", ["Unknown"])[0] if source.get("display_names") else "Unknown"
                        form = source.get("form_type", "8-K")
                        filed = source.get("file_date", "")
                        desc = source.get("display_description", "")[:200]

                        await self.emit({
                            "text": f"EDGAR {form}: {company} — {desc}",
                            "company": company,
                            "form_type": form,
                            "file_date": filed,
                            "filing_id": filing_id,
                            "extra_json": json.dumps({
                                "filing_id": filing_id,
                                "company": company,
                                "form_type": form,
                                "file_date": filed,
                            }),
                        })

        except Exception as e:
            log.warning("[EDGAR Filings] Error: %s", e)

    async def _poll_rss(self, session, headers):
        """Fallback: poll EDGAR company filings RSS for recent 8-Ks."""
        try:
            rss_url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=20&search_text=&start=0&output=atom"
            async with session.get(rss_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return

                text = await resp.text()
                # Parse Atom feed
                import xml.etree.ElementTree as ET
                root = ET.fromstring(text)
                ns = {"atom": "http://www.w3.org/2005/Atom"}

                for entry in root.findall("atom:entry", ns)[:15]:
                    title_el = entry.find("atom:title", ns)
                    link_el = entry.find("atom:link", ns)
                    summary_el = entry.find("atom:summary", ns)
                    uid_el = entry.find("atom:id", ns)

                    uid = uid_el.text if uid_el is not None else ""
                    if not uid or self._already_seen(uid):
                        continue

                    title = title_el.text if title_el is not None else ""
                    summary = summary_el.text if summary_el is not None else ""
                    link = link_el.get("href", "") if link_el is not None else ""

                    await self.emit({
                        "text": f"EDGAR 8-K: {title}",
                        "title": title,
                        "link": link,
                        "extra_json": json.dumps({"uid": uid, "source": "edgar_rss"}),
                    })

        except Exception as e:
            log.warning("[EDGAR Filings] RSS fallback error: %s", e)


# ═══════════════════════════════════════════════════════
# KRAKEN SPOT PRICE FEED (REAL)
# Endpoint: api.kraken.com/0/public/Ticker
# FREE, no API key. BTC + ETH spot prices every 10 seconds.
# Gives the dashboard something live to show.
# ═══════════════════════════════════════════════════════

class KrakenPriceSource(BaseSource):
    """
    Polls Kraken spot prices for BTC and ETH every 5 seconds.
    Always emits — this is the system heartbeat and RL price feed.
    FREE. No API key required.
    """
    name = "Kraken Price"
    interval_seconds = 5.0  # never dedup — always emit

    TICKER_URL = "https://api.kraken.com/0/public/Ticker"
    PAIRS = {
        "XXBTZUSD": "BTC",
        "XETHZUSD": "ETH",
    }

    def __init__(self, queue: asyncio.Queue):
        super().__init__(queue)
        self._last_prices = {}

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                params = {"pair": ",".join(self.PAIRS.keys())}
                async with session.get(
                    self.TICKER_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        return

                    data = await resp.json()
                    result = data.get("result", {})

                    for pair_id, label in self.PAIRS.items():
                        ticker = result.get(pair_id)
                        if not ticker:
                            continue

                        # c = last trade close, o = today open
                        price = float(ticker["c"][0])
                        open_price = float(ticker["o"])
                        high = float(ticker["h"][1])   # 24h high
                        low = float(ticker["l"][1])    # 24h low
                        volume = float(ticker["v"][1]) # 24h volume

                        # Only emit on meaningful price change ($10 BTC, $2 ETH)
                        last = self._last_prices.get(label)
                        min_change = 10.0 if label == "BTC" else 2.0
                        if last is not None and abs(price - last) < min_change:
                            continue
                        self._last_prices[label] = price

                        change_pct = ((price - open_price) / open_price) * 100 if open_price else 0
                        direction = "up" if change_pct >= 0 else "down"

                        await self.emit({
                            "text": f"{label}/USD ${price:,.2f} ({change_pct:+.2f}%) H:{high:,.0f} L:{low:,.0f}",
                            "symbol": label,
                            "price": price,
                            "change_pct": change_pct,
                            "high_24h": high,
                            "low_24h": low,
                            "volume_24h": volume,
                            "direction": direction,
                            "extra_json": json.dumps({
                                "symbol": label,
                                "price": price,
                                "change_pct": round(change_pct, 2),
                                "direction": direction,
                            }),
                        })

        except Exception as e:
            log.warning("[Kraken Price] Error: %s", e)


# ═══════════════════════════════════════════════════════
# USGS EARTHQUAKE FEED (REAL)
# Endpoint: earthquake.usgs.gov GeoJSON
# FREE, no key. Significant earthquakes within minutes of detection.
# ═══════════════════════════════════════════════════════

class UsgsEarthquakeSource(BaseSource):
    """
    Polls USGS significant earthquake feed. M5.5+ near oil regions
    or undersea cable corridors = supply-side shock signal.
    FREE. No API key required.
    """
    name = "USGS Earthquake"
    interval_seconds = 30.0

    FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_hour.geojson"
    # Fallback to larger window if hourly is empty
    FEED_DAY_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_day.geojson"

    # Oil-producing regions (lat/lon bounding boxes)
    OIL_REGIONS = {
        "MENA": {"lat": (15, 42), "lon": (25, 65)},
        "Venezuela": {"lat": (0, 13), "lon": (-75, -59)},
        "Gulf of Mexico": {"lat": (18, 31), "lon": (-98, -80)},
        "Nigeria": {"lat": (2, 14), "lon": (2, 15)},
    }

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                # Try significant_hour first, fall back to 4.5_day
                for url in [self.FEED_URL, self.FEED_DAY_URL]:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        features = data.get("features", [])
                        if features:
                            break

                for quake in features[:10]:
                    props = quake.get("properties", {})
                    geom = quake.get("geometry", {})
                    coords = geom.get("coordinates", [0, 0, 0])

                    qid = quake.get("id", "")
                    if not qid or self._already_seen(qid):
                        continue

                    mag = float(props.get("mag", 0))
                    place = props.get("place", "Unknown")
                    lon, lat = coords[0], coords[1]

                    # Check proximity to oil regions
                    region_hit = None
                    for region, bounds in self.OIL_REGIONS.items():
                        if bounds["lat"][0] <= lat <= bounds["lat"][1] and bounds["lon"][0] <= lon <= bounds["lon"][1]:
                            region_hit = region
                            break

                    alert = props.get("alert", "")  # green/yellow/orange/red
                    tsunami = props.get("tsunami", 0)

                    text = f"USGS: M{mag:.1f} earthquake — {place}"
                    if region_hit:
                        text += f" [OIL REGION: {region_hit}]"
                    if tsunami:
                        text += " [TSUNAMI WARNING]"

                    await self.emit({
                        "text": text,
                        "magnitude": mag,
                        "place": place,
                        "lat": lat,
                        "lon": lon,
                        "oil_region": region_hit,
                        "alert_level": alert,
                        "tsunami": tsunami,
                        "extra_json": json.dumps({
                            "quake_id": qid,
                            "magnitude": mag,
                            "place": place,
                            "oil_region": region_hit,
                            "alert": alert,
                        }),
                    })

        except Exception as e:
            log.warning("[USGS Earthquake] Error: %s", e)


# ═══════════════════════════════════════════════════════
# TREASURY AUCTION API (REAL)
# Endpoint: api.fiscaldata.treasury.gov
# FREE, no key. Auction results (bid-to-cover, high yield, tail).
# ═══════════════════════════════════════════════════════

class TreasuryAuctionSource(BaseSource):
    """
    Polls Treasury auction results from FiscalData API.
    Weak demand (low bid-to-cover, large tail) moves rates and equities.
    FREE. No API key required.
    """
    name = "Treasury Auction"
    interval_seconds = 120.0  # every 5 min

    API_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"

    async def poll(self) -> None:
        try:
            today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
            week_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")

            params = {
                "filter": f"auction_date:gte:{week_ago}",
                "sort": "-auction_date",
                "page[size]": "10",
                "fields": "security_type,security_term,auction_date,high_yield,bid_to_cover_ratio,total_accepted,total_tendered",
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                for record in data.get("data", []):
                    auction_date = record.get("auction_date", "")
                    sec_type = record.get("security_type", "")
                    sec_term = record.get("security_term", "")
                    high_yield = record.get("high_yield", "")
                    btc_ratio = record.get("bid_to_cover_ratio", "")

                    # Suppress null/empty yield rows — not a signal
                    if not high_yield or high_yield in ("null", "None", ""):
                        continue

                    key = f"auction-{auction_date}-{sec_type}-{sec_term}"
                    if self._already_seen(key):
                        continue

                    text = f"Treasury: {sec_type} {sec_term} auction — yield {high_yield}%"
                    if btc_ratio:
                        text += f", bid-to-cover {btc_ratio}"

                    await self.emit({
                        "text": text,
                        "security_type": sec_type,
                        "security_term": sec_term,
                        "auction_date": auction_date,
                        "high_yield": high_yield,
                        "bid_to_cover_ratio": btc_ratio,
                        "extra_json": json.dumps(record),
                    })

        except Exception as e:
            log.warning("[Treasury Auction] Error: %s", e)


# ═══════════════════════════════════════════════════════
# CFTC COMMITMENT OF TRADERS (REAL)
# Endpoint: publicreporting.cftc.gov Socrata API
# FREE, no key. Weekly COT positioning data.
# ═══════════════════════════════════════════════════════

class CftcCotSource(BaseSource):
    """
    Polls CFTC disaggregated COT report (Socrata API).
    Extreme managed-money positioning historically precedes reversals.
    FREE. No API key required.
    """
    name = "CFTC COT"
    interval_seconds = 1800.0  # hourly (data is weekly, Fri 3:30pm)

    COT_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

    # Markets we care about
    TRACKED_MARKETS = {
        "CRUDE OIL, LIGHT SWEET": "MCL",
        "GOLD": "MGC",
        "E-MINI S&P 500": "MES",
        "EURO FX": "M6E",
        "BITCOIN": "BTC",
    }

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "$order": "report_date_as_yyyy_mm_dd DESC",
                    "$limit": "20",
                }

                async with session.get(self.COT_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                for record in data:
                    market = record.get("market_and_exchange_names", "")
                    report_date = record.get("report_date_as_yyyy_mm_dd", "")
                    # Clean market name — CFTC adds exchange suffix
                    market_clean = market.split(" - ")[0].strip()

                    key = f"cot-{market_clean}-{report_date}"
                    if self._already_seen(key):
                        continue

                    asset = None
                    for tracked, label in self.TRACKED_MARKETS.items():
                        if tracked in market_clean.upper():
                            asset = label
                            break

                    # Managed money net position
                    mm_long = int(record.get("m_money_positions_long_all", 0) or 0)
                    mm_short = int(record.get("m_money_positions_short_all", 0) or 0)
                    mm_net = mm_long - mm_short

                    # Commercial hedgers
                    comm_long = int(record.get("prod_merc_positions_long_all", 0) or 0)
                    comm_short = int(record.get("prod_merc_positions_short_all", 0) or 0)
                    comm_net = comm_long - comm_short

                    direction = "net-long" if mm_net > 0 else "net-short"

                    await self.emit({
                        "text": f"CFTC COT: {market_clean} — Managed Money {direction} {abs(mm_net):,} contracts (report {report_date})",
                        "market": market_clean,
                        "asset": asset,
                        "managed_money_net": mm_net,
                        "commercial_net": comm_net,
                        "report_date": report_date,
                        "extra_json": json.dumps({
                            "market": market_clean,
                            "mm_net": mm_net,
                            "comm_net": comm_net,
                            "report_date": report_date,
                        }),
                    })

        except Exception as e:
            log.warning("[CFTC COT] Error: %s", e)


# ═══════════════════════════════════════════════════════
# NWS SEVERE WEATHER ALERTS (REAL)
# Endpoint: api.weather.gov/alerts/active
# FREE, no key. Active tornado, blizzard, extreme cold alerts.
# ═══════════════════════════════════════════════════════

class NwsSevereWeatherSource(BaseSource):
    """
    Polls NWS active weather alerts. Extreme cold → nat gas spike,
    Gulf storms → crude disruption, tornado clusters → grain impact.
    FREE. No API key required.
    """
    name = "NWS Weather"
    interval_seconds = 120.0  # every 5 min

    ALERTS_URL = "https://api.weather.gov/alerts/active"

    # Severity levels we care about
    SEVERE_EVENTS = {
        "Tornado Warning", "Tornado Watch",
        "Extreme Cold Warning", "Extreme Cold Watch",
        "Blizzard Warning", "Ice Storm Warning",
        "Hurricane Warning", "Hurricane Watch",
        "Tropical Storm Warning",
        "Storm Surge Warning",
        "Excessive Heat Warning",
        "Derecho",
    }

    async def poll(self) -> None:
        try:
            headers = {"User-Agent": "APEX Signal Trader research@apex.local", "Accept": "application/geo+json"}
            async with aiohttp.ClientSession() as session:
                params = {"status": "actual", "severity": "Extreme,Severe"}
                async with session.get(self.ALERTS_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                # Deduplicate by event TYPE, not by individual county alert ID.
                # Same storm = same event type = one signal, not 20 per-county copies.
                seen_event_types = set()
                for feature in data.get("features", []):
                    props = feature.get("properties", {})
                    event = props.get("event", "")
                    severity = props.get("severity", "")
                    headline = props.get("headline", "")

                    # One alert per event type per poll (not per county)
                    event_key = f"nws-{event}-{severity}"
                    if event_key in seen_event_types:
                        continue
                    seen_event_types.add(event_key)

                    if self._already_seen(event_key):
                        continue

                    area = props.get("areaDesc", "")[:100]
                    await self.emit({
                        "text": f"NWS {severity}: {event} — {headline[:150]}",
                        "event_type": event,
                        "severity": severity,
                        "area": area,
                        "extra_json": json.dumps({
                            "event": event, "severity": severity, "area": area,
                        }),
                    })

        except Exception as e:
            log.warning("[NWS Weather] Error: %s", e)


# ═══════════════════════════════════════════════════════
# NHC TROPICAL WEATHER RSS (REAL)
# Feed: nhc.noaa.gov — Atlantic + Eastern Pacific
# FREE, no key. Seasonal Jun–Nov.
# ═══════════════════════════════════════════════════════

class NhcTropicalSource(BaseSource):
    """
    Polls NHC Atlantic tropical weather feed.
    Gulf storms directly affect ~17% of US crude production.
    FREE. No API key required. Seasonal Jun–Nov.
    """
    name = "NHC Tropical"
    interval_seconds = 300.0  # every 15 min

    FEEDS = {
        "atlantic": "https://www.nhc.noaa.gov/index-at.xml",
        "outlook": "https://www.nhc.noaa.gov/gtwo.xml",
    }

    async def poll(self) -> None:
        loop = asyncio.get_event_loop()
        for feed_name, url in self.FEEDS.items():
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, url)
                for entry in feed.entries[:10]:
                    if not self._is_fresh_rss(entry):
                        continue
                    uid = entry.get("id") or entry.get("link", "")
                    if self._already_seen(uid):
                        continue

                    title = entry.get("title", "")
                    summary = entry.get("summary", "")[:200]

                    await self.emit({
                        "text": f"NHC: {title}",
                        "title": title,
                        "summary": summary,
                        "feed": feed_name,
                        "link": entry.get("link", ""),
                        "extra_json": json.dumps({"uid": uid, "feed": feed_name}),
                    })
            except Exception as e:
                log.warning("[NHC Tropical] %s error: %s", feed_name, e)


# ═══════════════════════════════════════════════════════
# FERC ENERGY RSS (REAL)
# Feed: ferc.gov newsroom RSS
# FREE, no key. Pipeline curtailments, LNG terminal orders.
# ═══════════════════════════════════════════════════════

class FercEnergySource(BaseSource):
    """
    Polls FERC newsroom RSS for energy regulatory actions.
    Emergency curtailment orders → NG futures. LNG terminal rulings → MCL.
    FREE. No API key required.
    """
    name = "FERC Energy"
    interval_seconds = 86400.0  # Blocked — daily poll to avoid wasted requests

    RSS_URL = "https://www.ferc.gov/rss/newsroom/ferc-news.rss"

    async def poll(self) -> None:
        try:
            loop = asyncio.get_event_loop()
            feed = await loop.run_in_executor(
                None,
                lambda: feedparser.parse(self.RSS_URL, request_headers={"User-Agent": "Mozilla/5.0 (compatible; APEX Signal Trader)"}),
            )
            for entry in feed.entries[:10]:
                if not self._is_fresh_rss(entry):
                    continue
                uid = entry.get("id") or entry.get("link", "")
                if self._already_seen(uid):
                    continue

                title = entry.get("title", "")
                summary = entry.get("summary", "")[:200]
                text = f"{title}. {summary}".strip()

                await self.emit({
                    "text": f"FERC: {text[:200]}",
                    "title": title,
                    "link": entry.get("link", ""),
                    "extra_json": json.dumps({"uid": uid}),
                })
        except Exception as e:
            log.warning("[FERC Energy] Error: %s", e)


# ═══════════════════════════════════════════════════════
# SEC FORM 4 INSIDER TRADES (REAL)
# Endpoint: efts.sec.gov — sub-second updates vs 10-min RSS
# FREE, no key. Cluster filings = highest-precision insider signal.
# ═══════════════════════════════════════════════════════

class SecForm4Source(BaseSource):
    """
    Polls SEC EFTS for Form 4 insider transaction filings.
    Cluster buys (CEO + CFO within 48h) = high conviction signal.
    FREE. No API key required.
    """
    name = "SEC Form 4"
    interval_seconds = 60.0  # every 2 min

    EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

    async def poll(self) -> None:
        try:
            now = datetime.datetime.utcnow()
            params = {
                "q": "\"Statement of Changes in Beneficial Ownership\"",
                "forms": "4",
                "dateRange": "custom",
                "startdt": (now - datetime.timedelta(hours=4)).strftime("%Y-%m-%d"),
                "enddt": now.strftime("%Y-%m-%d"),
            }
            headers = {
                "User-Agent": "APEX Signal Trader research@apex.local",
                "Accept": "application/json",
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(self.EFTS_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        # Fallback to RSS
                        await self._poll_rss(session, headers)
                        return

                    data = await resp.json()
                    for hit in data.get("hits", {}).get("hits", [])[:15]:
                        source = hit.get("_source", {})
                        fid = hit.get("_id", "")
                        if self._already_seen(fid):
                            continue

                        names = source.get("display_names", [])
                        company = names[0] if names else "Unknown"
                        filed = source.get("file_date", "")
                        desc = source.get("display_description", "")[:200]

                        await self.emit({
                            "text": f"SEC Form 4: {company} — {desc}",
                            "company": company,
                            "file_date": filed,
                            "extra_json": json.dumps({
                                "filing_id": fid,
                                "company": company,
                                "file_date": filed,
                            }),
                        })

        except Exception as e:
            log.warning("[SEC Form 4] Error: %s", e)

    async def _poll_rss(self, session, headers):
        """Fallback: current Form 4 filings via RSS."""
        try:
            url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&dateb=&owner=only&count=20&search_text=&start=0&output=atom"
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return
                text = await resp.text()

            import xml.etree.ElementTree as ET
            root = ET.fromstring(text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("atom:entry", ns)[:15]:
                title_el = entry.find("atom:title", ns)
                uid_el = entry.find("atom:id", ns)
                uid = uid_el.text if uid_el is not None else ""
                if not uid or self._already_seen(uid):
                    continue
                title = title_el.text if title_el is not None else ""
                link_el = entry.find("atom:link", ns)
                link = link_el.get("href", "") if link_el is not None else ""

                await self.emit({
                    "text": f"SEC Form 4: {title}",
                    "title": title,
                    "link": link,
                    "extra_json": json.dumps({"uid": uid, "source": "form4_rss"}),
                })
        except Exception as e:
            log.warning("[SEC Form 4] RSS fallback error: %s", e)


# ═══════════════════════════════════════════════════════
# POLYMARKET PREDICTION MARKET (REAL)
# Endpoint: gamma-api.polymarket.com
# FREE, no key. Implied probability shifts on macro events.
# ═══════════════════════════════════════════════════════

class PolymarketSource(BaseSource):
    """
    Polls Polymarket for active prediction markets on Fed, CPI,
    geopolitical events. 10-point probability shifts = institutional
    repositioning signal.
    FREE. No API key required for reads.
    """
    name = "Polymarket"
    interval_seconds = 30.0  # every 1 min

    API_URL = "https://gamma-api.polymarket.com/markets"

    # Keywords for markets we care about
    TRACKED_KEYWORDS = [
        "fed", "rate", "cpi", "inflation", "recession",
        "trump", "tariff", "war", "oil", "bitcoin", "btc",
        "election", "default", "shutdown", "debt ceiling",
    ]

    def __init__(self, queue: asyncio.Queue):
        super().__init__(queue)
        self._last_prices = {}  # slug -> last price

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                params = {"active": "true", "closed": "false", "limit": "50", "order": "volume", "ascending": "false"}
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    markets = await resp.json()

                for market in markets:
                    slug = market.get("conditionId", "") or market.get("id", "")
                    question = market.get("question", "")
                    if not question:
                        continue

                    # Filter to relevant markets
                    q_lower = question.lower()
                    relevant = any(kw in q_lower for kw in self.TRACKED_KEYWORDS)
                    if not relevant:
                        continue

                    # Get current probability
                    outcome_prices = market.get("outcomePrices", "")
                    if isinstance(outcome_prices, str):
                        try:
                            outcome_prices = json.loads(outcome_prices)
                        except (json.JSONDecodeError, TypeError):
                            continue

                    if not outcome_prices or not isinstance(outcome_prices, list):
                        continue

                    yes_price = float(outcome_prices[0]) if outcome_prices else 0
                    prob_pct = yes_price * 100

                    # Detect significant moves
                    last = self._last_prices.get(slug)
                    self._last_prices[slug] = prob_pct

                    if last is not None:
                        delta = prob_pct - last
                        if abs(delta) < 2.0:  # ignore small moves
                            continue

                        direction = "UP" if delta > 0 else "DOWN"
                        text = f"Polymarket: \"{question[:80]}\" {direction} {abs(delta):.1f}pts → {prob_pct:.0f}%"

                        await self.emit({
                            "text": text,
                            "question": question[:120],
                            "probability": prob_pct,
                            "delta": delta,
                            "direction": direction,
                            "extra_json": json.dumps({
                                "slug": slug,
                                "question": question[:120],
                                "probability": round(prob_pct, 1),
                                "delta": round(delta, 1),
                            }),
                        })
                    else:
                        # First observation — emit current state
                        key = f"poly-init-{slug}"
                        if not self._already_seen(key):
                            await self.emit({
                                "text": f"Polymarket: \"{question[:80]}\" at {prob_pct:.0f}%",
                                "question": question[:120],
                                "probability": prob_pct,
                                "extra_json": json.dumps({
                                    "slug": slug,
                                    "question": question[:120],
                                    "probability": round(prob_pct, 1),
                                }),
                            })

        except Exception as e:
            log.warning("[Polymarket] Error: %s", e)


# ═══════════════════════════════════════════════════════
# FRED API (St. Louis Fed) — REAL
# Endpoint: api.stlouisfed.org/fred/series/observations
# FREE with key. Treasury yields, SOFR, reverse repo, Fed balance sheet.
# ═══════════════════════════════════════════════════════

class FredSource(BaseSource):
    """
    Polls FRED for daily Treasury yields, SOFR, and RRP data.
    Yield spikes, RRP drops, balance sheet changes = macro signals.
    FREE with registered key (120 req/min).
    """
    name = "FRED"
    interval_seconds = 900.0  # every 30 min

    API_URL = "https://api.stlouisfed.org/fred/series/observations"

    SERIES = {
        "DGS10":    "10Y Treasury Yield",
        "DGS2":     "2Y Treasury Yield",
        "SOFR":     "SOFR Rate",
        "RRPONTSYD": "Reverse Repo (RRP)",
        "WALCL":    "Fed Balance Sheet",
    }

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("FRED_API_KEY", "")
        self._last_values = {}

    async def poll(self) -> None:
        if not self.api_key:
            return  # silently skip without key

        try:
            async with aiohttp.ClientSession() as session:
                for series_id, label in self.SERIES.items():
                    params = {
                        "series_id": series_id,
                        "api_key": self.api_key,
                        "file_type": "json",
                        "sort_order": "desc",
                        "limit": "1",
                    }
                    async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()

                    observations = data.get("observations", [])
                    if not observations:
                        continue

                    obs = observations[0]
                    value = obs.get("value", ".")
                    date = obs.get("date", "")

                    if value == ".":  # missing data
                        continue

                    key = f"fred-{series_id}-{date}-{value}"
                    if self._already_seen(key):
                        continue

                    # Detect change from last known
                    prev = self._last_values.get(series_id)
                    change_text = ""
                    if prev:
                        try:
                            delta = float(value) - float(prev)
                            change_text = f" ({delta:+.3f})"
                        except ValueError:
                            pass
                    self._last_values[series_id] = value

                    await self.emit({
                        "text": f"FRED: {label} = {value}{change_text} ({date})",
                        "series_id": series_id,
                        "series_name": label,
                        "value": value,
                        "date": date,
                        "extra_json": json.dumps({
                            "series_id": series_id,
                            "value": value,
                            "date": date,
                        }),
                    })

        except Exception as e:
            log.warning("[FRED] Error: %s", e)




# ═══════════════════════════════════════════════════════
# MISO REAL-TIME GRID DATA
# Zero auth. 5-min LMPs, fuel mix, wind actuals.
# ═══════════════════════════════════════════════════════

class MisoGridSource(BaseSource):
    """MISO grid: 5-min LMPs + fuel mix. Zero auth."""
    name = "MISO Grid"
    interval_seconds = 120.0

    FUEL_URLS = [
        "https://api.misoenergy.org/MISORTWDDataBroker/DataBrokerServices.asmx?messageType=getfuelmix&returnType=json",
        "https://www.misoenergy.org/api/MISORTWDDataBroker/getfuelmix",
    ]

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                data = None
                for fuel_url in self.FUEL_URLS:
                    try:
                        async with session.get(fuel_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                            # Check for error response
                            if isinstance(data, dict) and "error" in data:
                                log.warning("[MISO Grid] API returned error: %s — trying next endpoint", data["error"])
                                data = None
                                continue
                            break
                    except Exception:
                        continue

                if data is None:
                    log.warning("[MISO Grid] All fuel mix endpoints failed — MISO API may be down")
                    return

                fuel_mix = []
                if isinstance(data, dict):
                    # MISO returns nested structure
                    categories = data.get("Fuel", data.get("FuelMix", {}))
                    if isinstance(categories, dict):
                        fuel_mix = categories.get("Type", [])
                    elif isinstance(categories, list):
                        fuel_mix = categories
                elif isinstance(data, list):
                    fuel_mix = data

                for fuel in (fuel_mix if isinstance(fuel_mix, list) else []):
                    if isinstance(fuel, dict):
                        cat = fuel.get("CATEGORY", fuel.get("category", ""))
                        gen = fuel.get("ACT", fuel.get("act", 0))
                        key = f"miso-fuel-{cat}-{gen}"
                        if not self._already_seen(key):
                            await self.emit({
                                "text": f"MISO Fuel Mix: {cat} = {gen} MW",
                                "category": cat,
                                "generation_mw": gen,
                                "extra_json": json.dumps({"category": cat, "mw": gen}),
                            })
        except Exception as e:
            log.warning("[MISO Grid] Error: %s", e)


# ═══════════════════════════════════════════════════════
# NRC NUCLEAR REACTOR STATUS
# RSS feed. No auth. Reactor scrams, outages, emergencies.
# ═══════════════════════════════════════════════════════

class NrcReactorSource(BaseSource):
    """NRC event notifications + reactor status RSS. No auth."""
    name = "NRC Reactor"
    interval_seconds = 300.0

    EVENT_RSS = "https://www.nrc.gov/public-involve/rss?feed=event"
    NEWS_RSS = "https://www.nrc.gov/public-involve/rss?feed=news"

    async def poll(self) -> None:
        loop = asyncio.get_event_loop()
        for feed_name, url in [("events", self.EVENT_RSS), ("news", self.NEWS_RSS)]:
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, url)
                for entry in feed.entries[:10]:
                    if not self._is_fresh_rss(entry):
                        continue
                    uid = entry.get("id") or entry.get("link", "")
                    if self._already_seen(uid):
                        continue
                    title = entry.get("title", "")
                    summary = entry.get("summary", "")[:200]
                    await self.emit({
                        "text": f"NRC {feed_name}: {title}",
                        "title": title,
                        "summary": summary,
                        "link": entry.get("link", ""),
                        "extra_json": json.dumps({"uid": uid, "feed": feed_name}),
                    })
            except Exception as e:
                log.warning("[NRC Reactor] %s error: %s", feed_name, e)


# ═══════════════════════════════════════════════════════
# WHITE HOUSE PRESIDENTIAL ACTIONS RSS
# No auth. EOs appear hours before Federal Register.
# ═══════════════════════════════════════════════════════

class WhiteHouseSource(BaseSource):
    """White House presidential actions + briefings RSS. No auth."""
    name = "White House"
    interval_seconds = 60.0  # every 2 min

    FEEDS = {
        "actions": "https://www.whitehouse.gov/presidential-actions/feed/",
        "briefings": "https://www.whitehouse.gov/briefings-statements/feed/",
    }

    async def poll(self) -> None:
        loop = asyncio.get_event_loop()
        for feed_name, url in self.FEEDS.items():
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, url)
                for entry in feed.entries[:10]:
                    if not self._is_fresh_rss(entry):
                        continue
                    uid = entry.get("id") or entry.get("link", "")
                    if self._already_seen(uid):
                        continue
                    title = entry.get("title", "")
                    await self.emit({
                        "text": f"White House {feed_name}: {title}",
                        "title": title,
                        "link": entry.get("link", ""),
                        "extra_json": json.dumps({"uid": uid, "feed": feed_name}),
                    })
            except Exception as e:
                log.warning("[White House] %s error: %s", feed_name, e)


# ═══════════════════════════════════════════════════════
# FAA NAS STATUS API
# No auth. Ground stops, delays, airport closures.
# ═══════════════════════════════════════════════════════

class FaaNasSource(BaseSource):
    """FAA NAS airport status — ground stops, delays. No auth."""
    name = "FAA NAS"
    interval_seconds = 60.0

    API_URL = "https://nasstatus.faa.gov/api/airport-status-information"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    text = await resp.text()

                # FAA returns XML — parse it
                import xml.etree.ElementTree as ET
                try:
                    root = ET.fromstring(text)
                except ET.ParseError:
                    return

                # Extract ground stops and delays
                for delay_type in root.findall(".//Delay_type"):
                    name_el = delay_type.find("Name")
                    if name_el is None:
                        continue
                    delay_name = name_el.text or ""

                    # Get affected airports
                    for gs in delay_type.findall(".//Ground_Stop_List/Program") + delay_type.findall(".//Ground_Delay_List/Ground_Delay"):
                        arpt = gs.find("ARPT")
                        reason = gs.find("Reason")
                        airport = arpt.text if arpt is not None else ""
                        reason_text = reason.text if reason is not None else delay_name

                        key = f"faa-{airport}-{delay_name}"
                        if not airport or self._already_seen(key):
                            continue

                        await self.emit({
                            "text": f"FAA: {airport} — {delay_name}: {reason_text}",
                            "airport": airport,
                            "delay_type": delay_name,
                            "reason": reason_text,
                            "extra_json": json.dumps({"airport": airport, "type": delay_name, "reason": reason_text}),
                        })

                # If no specific delays found, emit update time
                update_el = root.find("Update_Time")
                if update_el is not None:
                    update_time = update_el.text or ""
                    key = f"faa-update-{update_time}"
                    if not self._already_seen(key):
                        # Check if any delays exist
                        delay_count = len(root.findall(".//Delay_type"))
                        if delay_count > 0:
                            await self.emit({
                                "text": f"FAA NAS: {delay_count} delay programs active ({update_time})",
                                "extra_json": json.dumps({"update": update_time, "delays": delay_count}),
                            })

                # Handle both API response formats (kept for legacy URL fallback)
                delays = data if isinstance(data, list) else data.get("delays", data.get("data", []))
                if isinstance(delays, dict):
                    delays = [delays]

                for delay in (delays if isinstance(delays, list) else []):
                    if not isinstance(delay, dict):
                        continue
                    airport = delay.get("arpt", delay.get("IATA", delay.get("airport", "")))
                    reason = delay.get("reason", delay.get("type", ""))
                    status = delay.get("status", "")

                    key = f"faa-{airport}-{reason}-{status}"
                    if not key.strip("-") or self._already_seen(key):
                        continue

                    text = f"FAA: {airport}"
                    if reason:
                        text += f" — {reason}"
                    if status:
                        text += f" ({status})"

                    await self.emit({
                        "text": text,
                        "airport": airport,
                        "reason": reason,
                        "extra_json": json.dumps(delay) if len(json.dumps(delay)) < 2000 else json.dumps({"airport": airport, "reason": reason}),
                    })
        except Exception as e:
            log.warning("[FAA NAS] Error: %s", e)


# ═══════════════════════════════════════════════════════
# CBP BORDER WAIT TIMES
# No auth. Commercial vehicle delays at US-Mexico ports.
# ═══════════════════════════════════════════════════════

class CbpBorderSource(BaseSource):
    """CBP border wait times — commercial delays at US-Mexico ports. No auth."""
    name = "CBP Border"
    interval_seconds = 300.0  # every 15 min

    API_URL = "https://bwt.cbp.gov/api/waittimes"

    # Major US-Mexico trade ports
    KEY_PORTS = {"Laredo", "El Paso", "Otay Mesa", "Nogales", "Hidalgo", "Brownsville", "Eagle Pass"}

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                for port in data if isinstance(data, list) else []:
                    name = port.get("port_name", port.get("port", ""))
                    if not any(kp.lower() in name.lower() for kp in self.KEY_PORTS):
                        continue

                    comm_delay = port.get("commercial_vehicle_lanes", {})
                    if isinstance(comm_delay, dict):
                        delay_min = comm_delay.get("delay_minutes", comm_delay.get("standard_lanes", {}).get("delay_minutes", 0))
                    else:
                        delay_min = 0

                    key = f"cbp-{name}-{delay_min}"
                    if self._already_seen(key):
                        continue

                    if delay_min and int(delay_min) > 0:
                        await self.emit({
                            "text": f"CBP: {name} — commercial delay {delay_min} min",
                            "port": name,
                            "delay_minutes": delay_min,
                            "extra_json": json.dumps({"port": name, "delay": delay_min}),
                        })
        except Exception as e:
            log.warning("[CBP Border] Error: %s", e)


# ═══════════════════════════════════════════════════════
# WHO DISEASE OUTBREAK NEWS
# OData JSON. No auth. Pandemic early warning.
# ═══════════════════════════════════════════════════════

class WhoOutbreakSource(BaseSource):
    """WHO Disease Outbreak News — pandemic early warning. No auth."""
    name = "WHO Outbreak"
    interval_seconds = 900.0  # every 30 min

    API_URL = "https://www.who.int/api/news/diseaseoutbreaknews"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                params = {"orderby": "PublicationDate desc", "top": "10"}
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                items = data.get("value", []) if isinstance(data, dict) else data
                for item in (items if isinstance(items, list) else []):
                    uid = item.get("Id", item.get("UrlName", ""))
                    if not uid or self._already_seen(str(uid)):
                        continue

                    title = item.get("Title", item.get("ItemDefaultUrl", ""))
                    pub_date = item.get("PublicationDate", "")
                    disease = item.get("DiseaseNames", "")

                    await self.emit({
                        "text": f"WHO: {title}",
                        "title": title,
                        "disease": disease,
                        "pub_date": pub_date,
                        "extra_json": json.dumps({"uid": uid, "disease": disease, "date": pub_date}),
                    })
        except Exception as e:
            log.warning("[WHO Outbreak] Error: %s", e)


# ═══════════════════════════════════════════════════════
# FINRA ATS DARK POOL VOLUME
# No auth. Weekly dark pool volume anomalies.
# ═══════════════════════════════════════════════════════

class FinraAtsSource(BaseSource):
    """FINRA ATS weekly dark pool volume. No auth."""
    name = "FINRA ATS"
    interval_seconds = 3600.0  # hourly (data is weekly)

    API_URL = "https://api.finra.org/data/group/otcmarket/name/weeklysummary"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Accept": "application/json"}
                async with session.get(self.API_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                items = data if isinstance(data, list) else data.get("data", [])
                for record in items[:20]:
                    if not isinstance(record, dict):
                        continue
                    symbol = record.get("symbol", record.get("issueSymbolIdentifier", ""))
                    volume = record.get("totalWeeklyShareQuantity", record.get("volume", 0))
                    trades = record.get("totalWeeklyTradeCount", record.get("trades", 0))
                    week = record.get("weekStartDate", "")

                    key = f"finra-{symbol}-{week}"
                    if not symbol or self._already_seen(key):
                        continue

                    await self.emit({
                        "text": f"FINRA ATS: {symbol} — {volume:,} shares, {trades:,} trades (week {week})",
                        "symbol": symbol,
                        "volume": volume,
                        "trades": trades,
                        "week": week,
                        "extra_json": json.dumps({"symbol": symbol, "volume": volume, "trades": trades}),
                    })
        except Exception as e:
            log.warning("[FINRA ATS] Error: %s", e)


# ═══════════════════════════════════════════════════════
# dYdX v4 PERPETUAL MARKETS
# No auth. DEX funding rates, OI, volume.
# ═══════════════════════════════════════════════════════

class DydxSource(BaseSource):
    """dYdX v4 perpetual markets — funding, OI, volume. No auth."""
    name = "dYdX"
    interval_seconds = 30.0

    MARKETS_URL = "https://indexer.dydx.trade/v4/perpetualMarkets"

    TRACKED = {"BTC-USD", "ETH-USD", "SOL-USD"}

    def __init__(self, queue: asyncio.Queue):
        super().__init__(queue)
        self._last_funding = {}

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.MARKETS_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                markets = data.get("markets", {})
                for ticker, info in markets.items():
                    if ticker not in self.TRACKED:
                        continue

                    funding = float(info.get("nextFundingRate", 0) or 0)
                    oi = float(info.get("openInterest", 0) or 0)
                    price = float(info.get("oraclePrice", 0) or 0)
                    vol_24h = float(info.get("volume24H", 0) or 0)

                    # Only emit on funding change
                    last = self._last_funding.get(ticker)
                    self._last_funding[ticker] = funding

                    key = f"dydx-{ticker}-{funding:.8f}"
                    if self._already_seen(key):
                        continue

                    funding_pct = funding * 100
                    await self.emit({
                        "text": f"dYdX {ticker}: funding {funding_pct:+.4f}%, OI ${oi:,.0f}, price ${price:,.2f}",
                        "ticker": ticker,
                        "funding_rate": funding,
                        "open_interest": oi,
                        "oracle_price": price,
                        "volume_24h": vol_24h,
                        "extra_json": json.dumps({
                            "ticker": ticker,
                            "funding": funding,
                            "oi": oi,
                            "price": price,
                        }),
                    })
        except Exception as e:
            log.warning("[dYdX] Error: %s", e)


# ═══════════════════════════════════════════════════════
# MEMPOOL.SPACE (Bitcoin mempool, fees, difficulty)
# No auth. Fee spikes = demand signal. Difficulty = miner economics.
# ═══════════════════════════════════════════════════════

class MempoolSource(BaseSource):
    """Bitcoin mempool fees + difficulty adjustment. No auth."""
    name = "Mempool"
    interval_seconds = 15.0

    FEES_URL = "https://mempool.space/api/v1/fees/recommended"
    DIFF_URL = "https://mempool.space/api/v1/difficulty-adjustment"
    HASHRATE_URL = "https://mempool.space/api/v1/mining/hashrate/1m"

    def __init__(self, queue: asyncio.Queue):
        super().__init__(queue)
        self._last_fast_fee = 0

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                # Fees
                async with session.get(self.FEES_URL, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        fees = await resp.json()
                        fast = fees.get("fastestFee", 0)
                        medium = fees.get("halfHourFee", 0)
                        slow = fees.get("hourFee", 0)

                        # Only emit on meaningful fee change
                        if abs(fast - self._last_fast_fee) >= 2:
                            self._last_fast_fee = fast
                            await self.emit({
                                "text": f"Mempool: BTC fees — fast {fast} sat/vB, medium {medium}, slow {slow}",
                                "fast_fee": fast,
                                "medium_fee": medium,
                                "slow_fee": slow,
                                "extra_json": json.dumps(fees),
                            })

                # Difficulty adjustment
                async with session.get(self.DIFF_URL, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        diff = await resp.json()
                        change_pct = diff.get("difficultyChange", 0)
                        remaining = diff.get("remainingBlocks", 0)
                        eta = diff.get("estimatedRetargetDate", 0)

                        key = f"mempool-diff-{remaining}"
                        if not self._already_seen(key) and remaining < 200:
                            await self.emit({
                                "text": f"Mempool: BTC difficulty retarget in {remaining} blocks ({change_pct:+.2f}%)",
                                "difficulty_change": change_pct,
                                "remaining_blocks": remaining,
                                "extra_json": json.dumps(diff),
                            })

        except Exception as e:
            log.warning("[Mempool] Error: %s", e)


# ═══════════════════════════════════════════════════════
# GDELT DOC 2.0 API
# No auth. Global event monitoring, 65 languages.
# ═══════════════════════════════════════════════════════

class GdeltDocSource(BaseSource):
    """GDELT DOC 2.0 — global event sentiment monitoring. No auth."""
    name = "GDELT"
    interval_seconds = 30.0  # short interval, but sleeps between queries to avoid 429

    API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

    QUERIES = [
        ("tariff OR sanctions OR trade war", "trade"),
        ("OPEC OR crude oil OR pipeline", "energy"),
        ("military OR missile OR invasion", "conflict"),
        ("fed rate OR inflation OR CPI", "macro"),
    ]

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                for qi, (query, category) in enumerate(self.QUERIES):
                    if qi > 0:
                        await asyncio.sleep(5)  # 5s between keyword queries to avoid 429
                    params = {
                        "query": query,
                        "mode": "artlist",
                        "maxrecords": "5",
                        "timespan": "60min",
                        "format": "json",
                        "sort": "datedesc",
                    }
                    try:
                        async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status != 200:
                                continue
                            text = await resp.text()
                            try:
                                data = json.loads(text)
                            except json.JSONDecodeError:
                                continue
                    except Exception:
                        continue

                    # GDELT returns articles at top level or nested
                    articles = data if isinstance(data, list) else data.get("articles", data.get("article", []))
                    for article in articles[:3]:
                        url = article.get("url", "")
                        if not url or self._already_seen(url):
                            continue
                        title = article.get("title", "")
                        try:
                            tone = float(article.get("tone", 0) or 0)
                        except (ValueError, TypeError):
                            tone = 0.0
                        domain = article.get("domain", "")
                        lang = article.get("language", "")

                        await self.emit({
                            "text": f"GDELT [{category}]: {title[:120]} (tone:{tone:.1f}, {domain})",
                            "title": title[:150],
                            "category": category,
                            "tone": tone,
                            "domain": domain,
                            "language": lang,
                            "extra_json": json.dumps({
                                "url": url,
                                "category": category,
                                "tone": tone,
                                "lang": lang,
                            }),
                        })
        except Exception as e:
            log.warning("[GDELT] Error: %s", e)


# ═══════════════════════════════════════════════════════
# BANK OF ENGLAND RSS + RATES
# No auth. MPC decisions, speeches, SONIA rate.
# ═══════════════════════════════════════════════════════

class BoeSource(BaseSource):
    """Bank of England news/speeches RSS + Bank Rate CSV. No auth."""
    name = "BoE"
    interval_seconds = 300.0

    NEWS_RSS = "https://www.bankofengland.co.uk/rss/news"
    SPEECHES_RSS = "https://www.bankofengland.co.uk/rss/speeches"

    async def poll(self) -> None:
        loop = asyncio.get_event_loop()
        for feed_name, url in [("news", self.NEWS_RSS), ("speeches", self.SPEECHES_RSS)]:
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, url)
                for entry in feed.entries[:10]:
                    if not self._is_fresh_rss(entry):
                        continue
                    uid = entry.get("id") or entry.get("link", "")
                    if self._already_seen(uid):
                        continue
                    title = entry.get("title", "")
                    await self.emit({
                        "text": f"BoE {feed_name}: {title}",
                        "title": title,
                        "link": entry.get("link", ""),
                        "extra_json": json.dumps({"uid": uid, "feed": feed_name}),
                    })
            except Exception as e:
                log.warning("[BoE] %s error: %s", feed_name, e)


# ═══════════════════════════════════════════════════════
# FEDERAL REGISTER PRE-PUBLICATION (UPGRADE)
# No auth. 15-hour window before official publication.
# ═══════════════════════════════════════════════════════

class FedRegPrePubSource(BaseSource):
    """Federal Register pre-publication documents — 15h early window. No auth."""
    name = "FedReg PrePub"
    interval_seconds = 300.0

    API_URL = "https://www.federalregister.gov/api/v1/public-inspection-documents/current.json"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                results = data.get("results", []) if isinstance(data, dict) else data
                for doc in (results if isinstance(results, list) else [])[:15]:
                    doc_num = doc.get("document_number", "")
                    if not doc_num or self._already_seen(doc_num):
                        continue

                    doc_type = doc.get("type", "")
                    title = doc.get("title", "")[:150]
                    agencies = ", ".join(a.get("name", "") for a in doc.get("agencies", [])[:3])

                    await self.emit({
                        "text": f"FedReg PrePub [{doc_type}]: {title} ({agencies})",
                        "document_number": doc_num,
                        "doc_type": doc_type,
                        "title": title,
                        "agencies": agencies,
                        "extra_json": json.dumps({
                            "doc_number": doc_num,
                            "type": doc_type,
                            "agencies": agencies,
                        }),
                    })
        except Exception as e:
            log.warning("[FedReg PrePub] Error: %s", e)


# ═══════════════════════════════════════════════════════
# CONGRESS.GOV API (Library of Congress)
# Bills, hearings, nominations. Free key required.
# ═══════════════════════════════════════════════════════

class CongressSource(BaseSource):
    """Congress.gov — recent bills, hearings, nominations. Free key."""
    name = "Congress"
    interval_seconds = 300.0

    BILLS_URL = "https://api.congress.gov/v3/bill"
    HEARINGS_URL = "https://api.congress.gov/v3/committee-meeting"

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("CONGRESS_API_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return

        try:
            async with aiohttp.ClientSession() as session:
                # Recent bills
                params = {
                    "api_key": self.api_key,
                    "limit": "20",
                    "sort": "updateDate+desc",
                    "format": "json",
                }
                async with session.get(self.BILLS_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for bill in data.get("bills", [])[:10]:
                            bill_num = bill.get("number", "")
                            bill_type = bill.get("type", "")
                            title = bill.get("title", "")[:150]
                            action = bill.get("latestAction", {}).get("text", "")[:100]
                            update = bill.get("updateDate", "")

                            key = f"congress-{bill_type}{bill_num}-{update}"
                            if self._already_seen(key):
                                continue

                            await self.emit({
                                "text": f"Congress: {bill_type}{bill_num} — {title} [{action}]",
                                "bill_number": f"{bill_type}{bill_num}",
                                "title": title,
                                "action": action,
                                "extra_json": json.dumps({
                                    "bill": f"{bill_type}{bill_num}",
                                    "title": title[:100],
                                    "action": action,
                                }),
                            })

                # Committee hearings
                params["limit"] = "10"
                async with session.get(self.HEARINGS_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for mtg in data.get("committeeMeetings", data.get("committee-meetings", []))[:5]:
                            if not isinstance(mtg, dict):
                                continue
                            title = mtg.get("title", "")[:150]
                            date = mtg.get("date", "")
                            chamber = mtg.get("chamber", "")

                            key = f"congress-hearing-{date}-{title[:40]}"
                            if self._already_seen(key):
                                continue

                            await self.emit({
                                "text": f"Congress hearing ({chamber}): {title}",
                                "title": title,
                                "date": date,
                                "chamber": chamber,
                                "extra_json": json.dumps({"title": title[:100], "date": date}),
                            })
        except Exception as e:
            log.warning("[Congress] Error: %s", e)


# ═══════════════════════════════════════════════════════
# NASA FIRMS FIRE DETECTION (satellite thermal anomalies)
# Free MAP_KEY. Fires near oil/gas infrastructure.
# ═══════════════════════════════════════════════════════

class NasaFirmsSource(BaseSource):
    """NASA FIRMS — satellite fire detection near energy infrastructure. Free key."""
    name = "NASA FIRMS"
    interval_seconds = 120.0  # every 5 min

    # Area query for key energy regions (US Gulf Coast + Permian Basin)
    # Format: west,south,east,north
    AREAS = {
        "Gulf Coast": "-98,26,-88,32",
        "Permian Basin": "-105,30,-100,34",
        "California": "-122,33,-115,38",
    }

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("NASA_FIRMS_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return

        try:
            async with aiohttp.ClientSession() as session:
                for region, coords in self.AREAS.items():
                    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{self.api_key}/VIIRS_NOAA20_NRT/{coords}/1"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status != 200:
                            continue
                        text = await resp.text()

                    lines = text.strip().split("\n")
                    if len(lines) < 2:
                        continue

                    headers = lines[0].split(",")
                    for line in lines[1:11]:  # max 10 fires per region
                        fields = line.split(",")
                        if len(fields) < len(headers):
                            continue

                        row = dict(zip(headers, fields))
                        lat = row.get("latitude", "")
                        lon = row.get("longitude", "")
                        bright = row.get("bright_ti4", row.get("brightness", ""))
                        conf = row.get("confidence", "")
                        acq_date = row.get("acq_date", "")
                        acq_time = row.get("acq_time", "")

                        key = f"firms-{lat}-{lon}-{acq_date}-{acq_time}"
                        if self._already_seen(key):
                            continue

                        # Only high confidence fires
                        if conf and conf.lower() in ("l", "low"):
                            continue

                        await self.emit({
                            "text": f"NASA FIRMS: Fire detected in {region} ({lat},{lon}) brightness={bright} conf={conf}",
                            "region": region,
                            "lat": lat,
                            "lon": lon,
                            "brightness": bright,
                            "confidence": conf,
                            "extra_json": json.dumps({
                                "region": region,
                                "lat": lat,
                                "lon": lon,
                                "brightness": bright,
                            }),
                        })
        except Exception as e:
            log.warning("[NASA FIRMS] Error: %s", e)


# ═══════════════════════════════════════════════════════
# GIE AGSI+ EUROPEAN GAS STORAGE
# Free key. Daily EU gas storage fill levels.
# ═══════════════════════════════════════════════════════

class GieAgsiSource(BaseSource):
    """GIE AGSI+ — European gas storage levels. Free key via x-key header."""
    name = "GIE AGSI"
    interval_seconds = 1800.0  # hourly (data is daily)

    API_URL = "https://agsi.gie.eu/api"

    COUNTRIES = {"EU": "EU aggregate", "DE": "Germany", "NL": "Netherlands", "IT": "Italy", "FR": "France"}

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("GIE_API_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return

        try:
            headers = {"x-key": self.api_key}
            async with aiohttp.ClientSession() as session:
                for code, label in self.COUNTRIES.items():
                    params = {"country": code}
                    async with session.get(self.API_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()

                    # Data is a list of daily records
                    records = data if isinstance(data, list) else data.get("data", [])
                    if not records:
                        continue

                    latest = records[0] if isinstance(records[0], dict) else {}
                    gas_day = latest.get("gasDayStart", latest.get("gasDayStartedOn", ""))
                    full_pct = latest.get("full", latest.get("fullPercent", 0))
                    injection = latest.get("injection", 0)
                    withdrawal = latest.get("withdrawal", 0)
                    trend = latest.get("trend", 0)

                    key = f"gie-{code}-{gas_day}"
                    if self._already_seen(key):
                        continue

                    flow = "injection" if float(injection or 0) > float(withdrawal or 0) else "withdrawal"

                    await self.emit({
                        "text": f"GIE AGSI: {label} gas storage {full_pct}% full ({flow}, trend {trend}) — {gas_day}",
                        "country": code,
                        "full_pct": full_pct,
                        "injection": injection,
                        "withdrawal": withdrawal,
                        "gas_day": gas_day,
                        "extra_json": json.dumps({
                            "country": code,
                            "full_pct": full_pct,
                            "flow": flow,
                            "gas_day": gas_day,
                        }),
                    })
        except Exception as e:
            log.warning("[GIE AGSI] Error: %s", e)


# ═══════════════════════════════════════════════════════
# OPENSKY NETWORK — LIVE AIRCRAFT TRACKING (ADS-B)
# Free registered account. Raw state vectors: position, velocity,
# heading, altitude, callsign, ICAO24 hex address.
# ═══════════════════════════════════════════════════════

class OpenSkySource(BaseSource):
    """
    OpenSky Network — raw ADS-B aircraft state vectors.
    Monitors key bounding boxes: Persian Gulf, DC area, major airports.
    Tracks military transponder prefixes and known VIP callsigns.
    Free registered account (4000 credits/day).
    """
    name = "OpenSky"
    interval_seconds = 15.0

    API_URL = "https://opensky-network.org/api/states/all"

    # Bounding boxes: lamin, lomin, lamax, lomax
    WATCH_AREAS = {
        "Persian Gulf": {"lamin": 24, "lomin": 48, "lamax": 30, "lomax": 57},
        "DC Area": {"lamin": 38.5, "lomin": -77.5, "lamax": 39.2, "lomax": -76.5},
        "Taiwan Strait": {"lamin": 22, "lomin": 117, "lamax": 26, "lomax": 121},
    }

    # ICAO24 hex prefixes for military aircraft (US=AE/AF, UK=43, NATO=various)
    MILITARY_PREFIXES = ("ae", "af", "43", "3e", "3f", "50", "70", "71")

    # Known high-interest callsigns (partial match)
    VIP_CALLSIGNS = {"SAM", "EXEC", "VENUS", "REACH", "EVAC", "IRON", "DUKE", "RCH"}

    def __init__(self, queue: asyncio.Queue, client_id: str = "", client_secret: str = ""):
        super().__init__(queue)
        self._last_military_count = {}
        self.client_id = client_id or os.environ.get("OPENSKY_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("OPENSKY_CLIENT_SECRET", "")

    async def poll(self) -> None:
        try:
            auth = None
            if self.client_id and self.client_secret:
                auth = aiohttp.BasicAuth(self.client_id, self.client_secret)
            async with aiohttp.ClientSession(auth=auth) as session:
                for area_name, bbox in self.WATCH_AREAS.items():
                    params = {**bbox}
                    try:
                        async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    states = data.get("states", [])
                    if not states:
                        continue

                    # Count military aircraft in zone
                    military_count = 0
                    vip_aircraft = []

                    for sv in states:
                        # State vector: [icao24, callsign, origin, time_pos, last_contact,
                        #                lon, lat, baro_alt, on_ground, velocity,
                        #                heading, vert_rate, sensors, geo_alt, squawk, spi, pos_source]
                        if len(sv) < 8:
                            continue

                        icao24 = (sv[0] or "").lower()
                        callsign = (sv[1] or "").strip()
                        lon = sv[5]
                        lat = sv[6]
                        alt = sv[7]  # baro altitude meters
                        on_ground = sv[8]
                        velocity = sv[9]  # m/s
                        squawk = sv[14] if len(sv) > 14 else ""

                        if on_ground:
                            continue

                        # Military detection
                        is_military = any(icao24.startswith(p) for p in self.MILITARY_PREFIXES)
                        if is_military:
                            military_count += 1

                        # VIP callsign detection
                        is_vip = any(v in callsign.upper() for v in self.VIP_CALLSIGNS)
                        if is_vip:
                            vip_aircraft.append({
                                "callsign": callsign,
                                "icao24": icao24,
                                "alt_ft": int((alt or 0) * 3.281),
                                "speed_kts": int((velocity or 0) * 1.944),
                            })

                    # Emit military aircraft count changes
                    last = self._last_military_count.get(area_name, 0)
                    self._last_military_count[area_name] = military_count

                    key = f"opensky-mil-{area_name}-{military_count}"
                    if military_count > 0 and not self._already_seen(key):
                        delta = military_count - last
                        delta_text = f" ({delta:+d})" if last > 0 else ""
                        await self.emit({
                            "text": f"OpenSky: {area_name} — {military_count} military aircraft airborne{delta_text}, {len(states)} total",
                            "area": area_name,
                            "military_count": military_count,
                            "total_aircraft": len(states),
                            "extra_json": json.dumps({
                                "area": area_name,
                                "military": military_count,
                                "total": len(states),
                            }),
                        })

                    # Emit VIP aircraft detections
                    for vip in vip_aircraft:
                        vip_key = f"opensky-vip-{vip['callsign']}-{area_name}"
                        if not self._already_seen(vip_key):
                            await self.emit({
                                "text": f"OpenSky: VIP aircraft {vip['callsign']} in {area_name} — {vip['alt_ft']}ft, {vip['speed_kts']}kts",
                                "area": area_name,
                                "callsign": vip["callsign"],
                                "icao24": vip["icao24"],
                                "extra_json": json.dumps(vip),
                            })

        except Exception as e:
            log.warning("[OpenSky] Error: %s", e)


# ═══════════════════════════════════════════════════════
# ERCOT PUBLIC API (Texas Grid)
# 5-min SCED LMPs, wind/solar production, system load.
# Requires subscription key (Ocp-Apim-Subscription-Key header).
# ═══════════════════════════════════════════════════════

class ErcotSource(BaseSource):
    """
    ERCOT Texas grid — 5-min LMPs, wind/solar production, system load.
    Price spikes from $25 to $5000/MWh cap during heat waves or wind drops.
    Wind ramp-downs force gas generation online → Henry Hub gas demand signal.
    Free registration required for subscription key.
    """
    name = "ERCOT"
    interval_seconds = 120.0  # every 5 min

    BASE_URL = "https://api.ercot.com/api/public-reports"

    # Key data products
    ENDPOINTS = {
        "lmp": "/np6-788-cd/lmp_node_zone_hub",      # 5-min SCED LMPs
        "wind": "/np4-732-cd/wpp_hrly_avrg_actl_fcast",  # wind production
        "solar": "/np4-745-cd/spp_hrly_actual_fcast_geo",  # solar production
    }

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("ERCOT_API_KEY", "")
        self._last_lmp = {}

    async def poll(self) -> None:
        if not self.api_key:
            return

        headers = {"Ocp-Apim-Subscription-Key": self.api_key}

        try:
            async with aiohttp.ClientSession() as session:
                for data_type, endpoint in self.ENDPOINTS.items():
                    url = f"{self.BASE_URL}{endpoint}"
                    try:
                        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    # ERCOT returns nested data structures
                    records = data.get("data", data.get("records", []))
                    if isinstance(records, dict):
                        records = records.get("records", records.get("data", []))
                    if not isinstance(records, list):
                        continue

                    for record in records[:5]:
                        if not isinstance(record, dict):
                            continue

                        if data_type == "lmp":
                            zone = record.get("SettlementPoint", record.get("settlement_point", ""))
                            lmp = record.get("LMP", record.get("lmp", 0))
                            interval = record.get("DeliveryInterval", record.get("interval", ""))

                            try:
                                lmp_val = float(lmp)
                            except (ValueError, TypeError):
                                continue

                            key = f"ercot-lmp-{zone}-{interval}"
                            if self._already_seen(key):
                                continue

                            # Detect price spikes
                            last = self._last_lmp.get(zone, lmp_val)
                            self._last_lmp[zone] = lmp_val
                            spike = ""
                            if lmp_val > 100:
                                spike = " [PRICE SPIKE]"
                            elif lmp_val > 50:
                                spike = " [ELEVATED]"

                            await self.emit({
                                "text": f"ERCOT LMP: {zone} = ${lmp_val:.2f}/MWh{spike}",
                                "zone": zone,
                                "lmp": lmp_val,
                                "extra_json": json.dumps({"zone": zone, "lmp": lmp_val, "interval": interval}),
                            })

                        elif data_type in ("wind", "solar"):
                            gen_type = data_type.upper()
                            actual = record.get("ActualMW", record.get("actual", record.get("ACTUAL", 0)))
                            forecast = record.get("CopHSL", record.get("forecast", record.get("FORECAST", 0)))
                            hour = record.get("HourEnding", record.get("hour", ""))

                            try:
                                actual_val = float(actual or 0)
                                forecast_val = float(forecast or 0)
                            except (ValueError, TypeError):
                                continue

                            key = f"ercot-{data_type}-{hour}-{actual_val:.0f}"
                            if self._already_seen(key):
                                continue

                            delta = actual_val - forecast_val
                            delta_pct = (delta / forecast_val * 100) if forecast_val else 0

                            text = f"ERCOT {gen_type}: {actual_val:,.0f} MW actual"
                            if forecast_val:
                                text += f" vs {forecast_val:,.0f} MW forecast ({delta_pct:+.1f}%)"

                            await self.emit({
                                "text": text,
                                "gen_type": gen_type,
                                "actual_mw": actual_val,
                                "forecast_mw": forecast_val,
                                "delta_pct": delta_pct,
                                "extra_json": json.dumps({
                                    "type": gen_type,
                                    "actual": actual_val,
                                    "forecast": forecast_val,
                                    "delta_pct": round(delta_pct, 1),
                                }),
                            })

        except Exception as e:
            log.warning("[ERCOT] Error: %s", e)




# ═══════════════════════════════════════════════════════
# OFAC SANCTIONS LIST (Treasury)
# No auth. XML diff every 5 min for new designations.
# ═══════════════════════════════════════════════════════

class OfacSanctionsSource(BaseSource):
    """OFAC SDN list — sanctions as trading signal. No auth."""
    name = "OFAC Sanctions"
    interval_seconds = 120.0

    SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.csv"

    def __init__(self, queue: asyncio.Queue):
        super().__init__(queue)
        self._last_line_count = 0

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.SDN_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return
                    text = await resp.text()

            lines = text.strip().split("\n")
            count = len(lines)

            if self._last_line_count > 0 and count != self._last_line_count:
                delta = count - self._last_line_count
                # New entries added
                if delta > 0:
                    for line in lines[-min(delta, 5):]:
                        fields = line.split(",")
                        if len(fields) < 3:
                            continue
                        entity = fields[1].strip('"') if len(fields) > 1 else ""
                        sdn_type = fields[2].strip('"') if len(fields) > 2 else ""
                        key = f"ofac-{entity[:50]}"
                        if self._already_seen(key):
                            continue
                        await self.emit({
                            "text": f"OFAC: NEW SANCTION — {entity} ({sdn_type}) [+{delta} entries]",
                            "entity": entity,
                            "sdn_type": sdn_type,
                            "delta": delta,
                            "extra_json": json.dumps({"entity": entity, "type": sdn_type, "delta": delta}),
                        })
            elif self._last_line_count == 0:
                key = f"ofac-baseline-{count}"
                if not self._already_seen(key):
                    await self.emit({
                        "text": f"OFAC: SDN baseline loaded — {count} entries",
                        "total_entries": count,
                        "extra_json": json.dumps({"total": count}),
                    })

            self._last_line_count = count
        except Exception as e:
            log.warning("[OFAC Sanctions] Error: %s", e)


# ═══════════════════════════════════════════════════════
# CAISO GRID (California ISO)
# No auth. 5-min LMPs for SP15/NP15 nodes.
# ═══════════════════════════════════════════════════════

class CaisoSource(BaseSource):
    """CAISO California grid — 5-min LMPs as nat gas demand proxy. No auth."""
    name = "CAISO"
    interval_seconds = 86400.0  # Blocked — daily poll to avoid wasted requests

    # Primary: CAISO Today's Outlook API
    OUTLOOK_URL = "https://www.caiso.com/api/TodaysOutlook/GetFuelTypeData"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(self.OUTLOOK_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status != 200:
                            log.warning("[CAISO] API returned HTTP %s — endpoint may have changed", resp.status)
                            return
                        data = await resp.json()
                except Exception as fetch_err:
                    log.warning("[CAISO] Fetch failed: %s — endpoint may be down", fetch_err)
                    return

            # API returns a list of fuel type objects or a dict with nested data
            items = data if isinstance(data, list) else data.get("data", data.get("Fuels", []))
            if not isinstance(items, list) or not items:
                log.warning("[CAISO] Unexpected response format — skipping")
                return

            summary = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                fuel = item.get("FuelType", item.get("fuel_type", item.get("name", "")))
                gen = item.get("CurrentValue", item.get("current", item.get("value", 0)))
                if fuel:
                    summary[fuel] = gen

            key = f"caiso-{json.dumps(summary, sort_keys=True)}"
            if self._already_seen(key):
                return

            natural_gas = summary.get("Natural Gas", summary.get("Gas", "0"))
            solar = summary.get("Solar", "0")
            wind = summary.get("Wind", "0")
            imports = summary.get("Imports", "0")

            await self.emit({
                "text": f"CAISO: Gas={natural_gas}MW Solar={solar}MW Wind={wind}MW Imports={imports}MW",
                "natural_gas_mw": natural_gas,
                "solar_mw": solar,
                "wind_mw": wind,
                "extra_json": json.dumps(summary),
            })
        except Exception as e:
            log.warning("[CAISO] Error: %s", e)


# ═══════════════════════════════════════════════════════
# EIA NATURAL GAS STORAGE (extends existing EIA key)
# Weekly Thursday 10:30 AM ET. High market impact.
# ═══════════════════════════════════════════════════════

class EiaNatGasSource(BaseSource):
    """EIA weekly natural gas storage report. Uses existing EIA key."""
    name = "EIA NatGas"
    interval_seconds = 900.0

    API_URL = "https://api.eia.gov/v2/natural-gas/stor/wkly/data/"

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("EIA_API_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return
        try:
            params = {"api_key": self.api_key, "data[]": "value", "sort[0][column]": "period", "sort[0][direction]": "desc", "length": "5"}
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            for record in data.get("response", {}).get("data", [])[:3]:
                period = record.get("period", "")
                value = record.get("value", "")
                process = record.get("process-name", record.get("process", ""))
                area = record.get("area-name", record.get("area", ""))

                key = f"eia-ng-{period}-{area}-{value}"
                if self._already_seen(key):
                    continue

                await self.emit({
                    "text": f"EIA NatGas: {area} storage {value} Bcf ({process}) — {period}",
                    "value_bcf": value,
                    "period": period,
                    "area": area,
                    "extra_json": json.dumps(record),
                })
        except Exception as e:
            log.warning("[EIA NatGas] Error: %s", e)


# ═══════════════════════════════════════════════════════
# BANK OF JAPAN RSS (launched Feb 2026)
# No auth. Rate decisions, yield curve control.
# ═══════════════════════════════════════════════════════

class BojSource(BaseSource):
    """Bank of Japan — monetary policy RSS. No auth."""
    name = "BOJ"
    interval_seconds = 300.0

    RSS_URL = "https://www.boj.or.jp/en/rss/whatsnew.xml"

    async def poll(self) -> None:
        try:
            loop = asyncio.get_event_loop()
            feed = await loop.run_in_executor(None, feedparser.parse, self.RSS_URL)
            for entry in feed.entries[:10]:
                if not self._is_fresh_rss(entry):
                    continue
                uid = entry.get("id") or entry.get("link", "")
                if self._already_seen(uid):
                    continue
                title = entry.get("title", "")
                await self.emit({
                    "text": f"BOJ: {title}",
                    "title": title,
                    "link": entry.get("link", ""),
                    "extra_json": json.dumps({"uid": uid}),
                })
        except Exception as e:
            log.warning("[BOJ] Error: %s", e)


# ═══════════════════════════════════════════════════════
# DOJ ANTITRUST RSS
# No auth. Criminal indictments, merger blocks.
# ═══════════════════════════════════════════════════════

class DojAntitrustSource(BaseSource):
    """DOJ Antitrust Division — criminal + civil filings. No auth."""
    name = "DOJ Antitrust"
    interval_seconds = 300.0

    FEEDS = {
        "criminal": "https://www.justice.gov/media/1194211/dl?inline",
        "civil": "https://www.justice.gov/media/1190096/dl?inline",
    }

    async def poll(self) -> None:
        loop = asyncio.get_event_loop()
        for feed_name, url in self.FEEDS.items():
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, url)
                for entry in feed.entries[:10]:
                    if not self._is_fresh_rss(entry):
                        continue
                    uid = entry.get("id") or entry.get("link", "")
                    if self._already_seen(uid):
                        continue
                    title = entry.get("title", "")
                    await self.emit({
                        "text": f"DOJ Antitrust [{feed_name}]: {title}",
                        "title": title,
                        "feed": feed_name,
                        "link": entry.get("link", ""),
                        "extra_json": json.dumps({"uid": uid, "feed": feed_name}),
                    })
            except Exception as e:
                log.warning("[DOJ Antitrust] %s error: %s", feed_name, e)


# ═══════════════════════════════════════════════════════
# BANK OF CANADA VALET API
# No auth. CAD/USD rate + RSS for rate decisions.
# ═══════════════════════════════════════════════════════

class BocSource(BaseSource):
    """Bank of Canada — CAD/USD rate + policy RSS. No auth."""
    name = "BoC"
    interval_seconds = 300.0

    RATE_URL = "https://www.bankofcanada.ca/valet/observations/FXCADUSD/json"
    RSS_URL = "https://www.bankofcanada.ca/feed/"

    async def poll(self) -> None:
        try:
            # CAD/USD rate
            async with aiohttp.ClientSession() as session:
                async with session.get(self.RATE_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        obs = data.get("observations", [])
                        if obs:
                            latest = obs[-1]
                            date = latest.get("d", "")
                            rate = latest.get("FXCADUSD", {}).get("v", "")
                            key = f"boc-rate-{date}-{rate}"
                            if rate and not self._already_seen(key):
                                await self.emit({
                                    "text": f"BoC: CAD/USD = {rate} ({date})",
                                    "rate": rate,
                                    "date": date,
                                    "extra_json": json.dumps({"rate": rate, "date": date}),
                                })
        except Exception as e:
            log.warning("[BoC] rate error: %s", e)

        # RSS
        try:
            loop = asyncio.get_event_loop()
            feed = await loop.run_in_executor(None, feedparser.parse, self.RSS_URL)
            for entry in feed.entries[:5]:
                if not self._is_fresh_rss(entry):
                    continue
                uid = entry.get("id") or entry.get("link", "")
                if self._already_seen(uid):
                    continue
                title = entry.get("title", "")
                await self.emit({
                    "text": f"BoC: {title}",
                    "title": title,
                    "link": entry.get("link", ""),
                    "extra_json": json.dumps({"uid": uid}),
                })
        except Exception as e:
            log.warning("[BoC] RSS error: %s", e)


# ═══════════════════════════════════════════════════════
# CBOE PUT/CALL RATIO
# No auth. CSV download. Contrarian sentiment indicator.
# ═══════════════════════════════════════════════════════

class CboePcRatioSource(BaseSource):
    """CBOE equity put/call ratio — contrarian sentiment. No auth."""
    name = "CBOE P/C"
    interval_seconds = 1800.0

    EQUITY_URL = "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/equitypc.csv"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.EQUITY_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    text = await resp.text()

            lines = text.strip().split("\n")
            if len(lines) < 2:
                return

            latest = lines[-1].split(",")
            if len(latest) < 5:
                return

            date = latest[0].strip()
            pc_ratio = latest[-1].strip()

            key = f"cboe-pc-{date}"
            if self._already_seen(key):
                return

            try:
                ratio = float(pc_ratio)
                signal = ""
                if ratio > 1.2:
                    signal = " [EXTREME FEAR — contrarian BUY]"
                elif ratio < 0.6:
                    signal = " [EXTREME COMPLACENCY — risk warning]"
            except ValueError:
                ratio = 0
                signal = ""

            await self.emit({
                "text": f"CBOE: Equity P/C ratio = {pc_ratio}{signal} ({date})",
                "pc_ratio": pc_ratio,
                "date": date,
                "extra_json": json.dumps({"ratio": pc_ratio, "date": date}),
            })
        except Exception as e:
            log.warning("[CBOE P/C] Error: %s", e)


# ═══════════════════════════════════════════════════════
# DEFI LLAMA — TVL, stablecoin flows, DEX volume
# No auth. Leading indicator for crypto capital flows.
# ═══════════════════════════════════════════════════════

class DefiLlamaSource(BaseSource):
    """DeFi Llama — TVL + stablecoin supply + DEX volume. No auth."""
    name = "DeFi Llama"
    interval_seconds = 600.0

    STABLECOIN_URL = "https://stablecoins.llama.fi/stablecoins?includePrices=true"
    TVL_URL = "https://api.llama.fi/v2/historicalChainTvl"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                # Stablecoin supply
                async with session.get(self.STABLECOIN_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        stables = data.get("peggedAssets", [])
                        for s in stables[:5]:  # top 5 stablecoins
                            name = s.get("name", "")
                            symbol = s.get("symbol", "")
                            chains = s.get("chainCirculating", {})
                            total = sum(float(v.get("current", {}).get("peggedUSD", 0) or 0) for v in chains.values())

                            key = f"defi-stable-{symbol}-{int(total/1e6)}"
                            if self._already_seen(key):
                                continue

                            await self.emit({
                                "text": f"DeFi Llama: {name} ({symbol}) supply = ${total/1e9:.2f}B",
                                "stablecoin": symbol,
                                "supply_usd": total,
                                "extra_json": json.dumps({"symbol": symbol, "supply": total}),
                            })

                # Total chain TVL
                async with session.get(self.TVL_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and isinstance(data, list):
                            latest = data[-1]
                            tvl = float(latest.get("tvl", 0))
                            date = latest.get("date", 0)

                            key = f"defi-tvl-{int(tvl/1e9)}"
                            if not self._already_seen(key):
                                await self.emit({
                                    "text": f"DeFi Llama: Total DeFi TVL = ${tvl/1e9:.1f}B",
                                    "tvl": tvl,
                                    "extra_json": json.dumps({"tvl": tvl}),
                                })
        except Exception as e:
            log.warning("[DeFi Llama] Error: %s", e)


# ═══════════════════════════════════════════════════════
# HYPERLIQUID DEX — largest on-chain perp exchange
# No auth. Funding, OI, whale tracking.
# ═══════════════════════════════════════════════════════

class HyperliquidSource(BaseSource):
    """Hyperliquid DEX — perpetual funding/OI/volume. No auth."""
    name = "Hyperliquid"
    interval_seconds = 30.0

    API_URL = "https://api.hyperliquid.xyz/info"
    TRACKED = {"BTC", "ETH", "SOL"}

    def __init__(self, queue: asyncio.Queue):
        super().__init__(queue)
        self._last_funding = {}

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.API_URL, json={"type": "metaAndAssetCtxs"}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            if not isinstance(data, list) or len(data) < 2:
                return

            meta = data[0].get("universe", [])
            ctxs = data[1]

            for i, asset in enumerate(meta):
                name = asset.get("name", "")
                if name not in self.TRACKED or i >= len(ctxs):
                    continue

                ctx = ctxs[i]
                funding = float(ctx.get("funding", 0) or 0)
                oi = float(ctx.get("openInterest", 0) or 0)
                mark = float(ctx.get("markPx", 0) or 0)

                key = f"hyper-{name}-{funding:.8f}"
                if self._already_seen(key):
                    continue

                funding_pct = funding * 100
                await self.emit({
                    "text": f"Hyperliquid {name}: funding {funding_pct:+.4f}%, OI ${oi:,.0f}, mark ${mark:,.2f}",
                    "asset": name,
                    "funding_rate": funding,
                    "open_interest": oi,
                    "mark_price": mark,
                    "extra_json": json.dumps({"asset": name, "funding": funding, "oi": oi, "mark": mark}),
                })
        except Exception as e:
            log.warning("[Hyperliquid] Error: %s", e)


# ═══════════════════════════════════════════════════════
# SWISS NATIONAL BANK RSS
# No auth. CHF safe-haven + FX intervention signals.
# ═══════════════════════════════════════════════════════

class SnbSource(BaseSource):
    """Swiss National Bank — monetary policy + adhoc alerts. No auth."""
    name = "SNB"
    interval_seconds = 300.0

    FEEDS = {
        "monetary_policy": "https://www.snb.ch/public/en/rss/mopo",
        "adhoc": "https://www.snb.ch/public/en/rss/adhoc",
    }

    async def poll(self) -> None:
        loop = asyncio.get_event_loop()
        for feed_name, url in self.FEEDS.items():
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, url)
                for entry in feed.entries[:10]:
                    if not self._is_fresh_rss(entry):
                        continue
                    uid = entry.get("id") or entry.get("link", "")
                    if self._already_seen(uid):
                        continue
                    title = entry.get("title", "")
                    priority = "URGENT" if feed_name == "adhoc" else "normal"
                    await self.emit({
                        "text": f"SNB [{priority}]: {title}",
                        "title": title,
                        "priority": priority,
                        "feed": feed_name,
                        "extra_json": json.dumps({"uid": uid, "feed": feed_name, "priority": priority}),
                    })
            except Exception as e:
                log.warning("[SNB] %s error: %s", feed_name, e)


# ═══════════════════════════════════════════════════════
# WIKIPEDIA PAGEVIEWS — public attention as leading indicator
# No auth. Attention spikes predict market moves.
# ═══════════════════════════════════════════════════════

class WikiPageviewSource(BaseSource):
    """Wikipedia pageview spikes — attention signal for breaking events. No auth."""
    name = "Wiki Pageviews"
    interval_seconds = 3600.0  # hourly

    API_URL = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia.org/all-access/user"

    WATCHLIST = ["Bitcoin", "Recession", "Bank_run", "OPEC", "Federal_Reserve",
                 "Crude_oil", "Gold", "Inflation", "Stock_market_crash", "Tariff"]

    def __init__(self, queue: asyncio.Queue):
        super().__init__(queue)
        self._baselines = {}

    async def poll(self) -> None:
        try:
            now = datetime.datetime.utcnow()
            yesterday = (now - datetime.timedelta(days=1)).strftime("%Y%m%d")
            week_ago = (now - datetime.timedelta(days=8)).strftime("%Y%m%d")

            headers = {"User-Agent": "APEX Signal Trader research@apex.local"}
            async with aiohttp.ClientSession() as session:
                for page in self.WATCHLIST:
                    url = f"{self.API_URL}/{page}/daily/{week_ago}/{yesterday}"
                    try:
                        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    items = data.get("items", [])
                    if len(items) < 2:
                        continue

                    # Latest day vs average of prior days
                    latest_views = items[-1].get("views", 0)
                    prior_avg = sum(i.get("views", 0) for i in items[:-1]) / max(len(items) - 1, 1)

                    if prior_avg < 100:
                        continue

                    spike_ratio = latest_views / prior_avg if prior_avg else 0

                    key = f"wiki-{page}-{yesterday}"
                    if self._already_seen(key):
                        continue

                    if spike_ratio > 2.0:
                        await self.emit({
                            "text": f"Wiki: '{page.replace('_',' ')}' pageviews {spike_ratio:.1f}x above normal ({latest_views:,} vs avg {prior_avg:,.0f})",
                            "page": page,
                            "views": latest_views,
                            "avg_views": prior_avg,
                            "spike_ratio": spike_ratio,
                            "extra_json": json.dumps({"page": page, "views": latest_views, "spike": round(spike_ratio, 1)}),
                        })
        except Exception as e:
            log.warning("[Wiki Pageviews] Error: %s", e)


# ═══════════════════════════════════════════════════════
# FTC COMPETITION RSS
# No auth. Merger blocks, enforcement.
# ═══════════════════════════════════════════════════════

class FtcCompetitionSource(BaseSource):
    """FTC Competition enforcement — merger blocks, investigations. No auth."""
    name = "FTC"
    interval_seconds = 300.0

    RSS_URL = "https://www.ftc.gov/feeds/press-release-competition.xml"

    async def poll(self) -> None:
        try:
            loop = asyncio.get_event_loop()
            feed = await loop.run_in_executor(
                None,
                lambda: feedparser.parse(self.RSS_URL, request_headers={"User-Agent": "Mozilla/5.0 (compatible; APEX Signal Trader)"}),
            )
            for entry in feed.entries[:10]:
                if not self._is_fresh_rss(entry):
                    continue
                uid = entry.get("id") or entry.get("link", "")
                if self._already_seen(uid):
                    continue
                title = entry.get("title", "")
                await self.emit({
                    "text": f"FTC: {title}",
                    "title": title,
                    "link": entry.get("link", ""),
                    "extra_json": json.dumps({"uid": uid}),
                })
        except Exception as e:
            log.warning("[FTC] Error: %s", e)


# ═══════════════════════════════════════════════════════
# NOAA CO-OPS TIDES & CURRENTS — port water levels
# No auth. 6-min resolution at major ports.
# ═══════════════════════════════════════════════════════

class NoaaCoopsSource(BaseSource):
    """NOAA CO-OPS water levels at key ports. No auth."""
    name = "NOAA Ports"
    interval_seconds = 300.0

    API_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

    STATIONS = {
        "8770570": "Houston Ship Channel",
        "9410660": "Port of LA",
        "8670870": "Savannah",
        "8518750": "New York (Battery)",
    }

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                for station_id, name in self.STATIONS.items():
                    params = {
                        "date": "latest",
                        "station": station_id,
                        "product": "water_level",
                        "datum": "MLLW",
                        "units": "english",
                        "time_zone": "gmt",
                        "format": "json",
                        "application": "APEX_SignalTrader",
                    }
                    try:
                        async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    readings = data.get("data", [])
                    if not readings:
                        continue

                    latest = readings[-1]
                    level = float(latest.get("v", 0))
                    time_str = latest.get("t", "")

                    key = f"coops-{station_id}-{time_str}"
                    if self._already_seen(key):
                        continue

                    alert = ""
                    if level > 5.0:
                        alert = " [FLOOD RISK]"
                    elif level > 3.0:
                        alert = " [ELEVATED]"

                    await self.emit({
                        "text": f"NOAA Ports: {name} water level {level:.2f} ft{alert} ({time_str})",
                        "station": name,
                        "water_level_ft": level,
                        "extra_json": json.dumps({"station": name, "level": level, "time": time_str}),
                    })
        except Exception as e:
            log.warning("[NOAA Ports] Error: %s", e)


# ═══════════════════════════════════════════════════════
# EUROSTAT SDMX — Eurozone GDP, HICP, unemployment
# No auth. Primary source for ECB policy inputs.
# ═══════════════════════════════════════════════════════

class EurostatSource(BaseSource):
    """Eurostat — Eurozone macro stats (HICP, GDP, unemployment). No auth."""
    name = "Eurostat"
    interval_seconds = 1800.0

    API_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"

    DATASETS = {
        "prc_hicp_mmor": "HICP Monthly Inflation",
        "namq_10_gdp": "GDP Quarterly",
        "une_rt_m": "Unemployment Monthly",
    }

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                for dataset, label in self.DATASETS.items():
                    url = f"{self.API_URL}/{dataset}?format=JSON&lang=en&geo=EA20"
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    values = data.get("value", {})
                    times = data.get("dimension", {}).get("time", {}).get("category", {}).get("index", {})

                    if not values or not times:
                        continue

                    # Get latest value
                    max_idx = max(int(k) for k in values.keys()) if values else -1
                    if max_idx < 0:
                        continue

                    val = values.get(str(max_idx), "")
                    # Find time period for this index
                    period = ""
                    for t, idx in times.items():
                        if idx == max_idx:
                            period = t
                            break

                    key = f"eurostat-{dataset}-{period}-{val}"
                    if self._already_seen(key):
                        continue

                    await self.emit({
                        "text": f"Eurostat: {label} = {val} ({period})",
                        "dataset": dataset,
                        "label": label,
                        "value": val,
                        "period": period,
                        "extra_json": json.dumps({"dataset": dataset, "value": val, "period": period}),
                    })
        except Exception as e:
            log.warning("[Eurostat] Error: %s", e)


# ═══════════════════════════════════════════════════════
# USGS WATER SERVICES — flood/drought at 15-min resolution
# No auth. Mississippi River + Gulf Coast refinery risk.
# ═══════════════════════════════════════════════════════

class UsgsWaterSource(BaseSource):
    """USGS streamgage data — flood/drought detection. No auth."""
    name = "USGS Water"
    interval_seconds = 300.0

    API_URL = "https://waterservices.usgs.gov/nwis/iv/"

    SITES = {
        "07032000": "Mississippi at Memphis",
        "07289000": "Mississippi at Vicksburg",
        "08075000": "Brays Bayou Houston",
    }

    async def poll(self) -> None:
        try:
            site_ids = ",".join(self.SITES.keys())
            params = {
                "format": "json",
                "sites": site_ids,
                "parameterCd": "00065",  # gage height
                "siteStatus": "active",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            ts_list = data.get("value", {}).get("timeSeries", [])
            for ts in ts_list:
                site_code = ts.get("sourceInfo", {}).get("siteCode", [{}])[0].get("value", "")
                site_name = self.SITES.get(site_code, site_code)
                values = ts.get("values", [{}])[0].get("value", [])

                if not values:
                    continue

                latest = values[-1]
                gage_height = float(latest.get("value", 0))
                time_str = latest.get("dateTime", "")

                key = f"usgs-water-{site_code}-{gage_height:.1f}"
                if self._already_seen(key):
                    continue

                alert = ""
                if "Memphis" in site_name and gage_height < 0:
                    alert = " [LOW WATER — barge restrictions]"
                elif gage_height > 30:
                    alert = " [FLOOD STAGE]"

                await self.emit({
                    "text": f"USGS Water: {site_name} gage height {gage_height:.2f} ft{alert}",
                    "site": site_name,
                    "gage_height_ft": gage_height,
                    "extra_json": json.dumps({"site": site_name, "height": gage_height}),
                })
        except Exception as e:
            log.warning("[USGS Water] Error: %s", e)


# ═══════════════════════════════════════════════════════
# USDA LIVESTOCK MANDATORY PRICE REPORTING
# No auth. Daily cattle/hog prices — inflation leading indicator.
# ═══════════════════════════════════════════════════════

class UsdaLmprSource(BaseSource):
    """USDA livestock prices — food inflation leading indicator. No auth."""
    name = "USDA LMPR"
    interval_seconds = 1800.0

    API_URL = "https://mpr.datamart.ams.usda.gov/services/v1.1/reports"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                # National Daily Direct Hog Report
                async with session.get(f"{self.API_URL}/2511", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results", []) if isinstance(data, dict) else data
                        if isinstance(results, list):
                            for record in results[:3]:
                                if not isinstance(record, dict):
                                    continue
                                date = record.get("report_date", record.get("for_date_begin", ""))
                                price = record.get("avg_price", record.get("wtd_avg", ""))
                                key = f"usda-hog-{date}-{price}"
                                if not self._already_seen(key) and price:
                                    await self.emit({
                                        "text": f"USDA: National hog price ${price}/cwt ({date})",
                                        "commodity": "hog",
                                        "price": price,
                                        "date": date,
                                        "extra_json": json.dumps({"commodity": "hog", "price": price}),
                                    })
        except Exception as e:
            log.warning("[USDA LMPR] Error: %s", e)


# ═══════════════════════════════════════════════════════
# BEA DATA API — GDP, PCE, personal income at source
# Free key. Faster than FRED on release mornings.
# ═══════════════════════════════════════════════════════

class BeaSource(BaseSource):
    """BEA — GDP, PCE Price Index, personal income. Free key."""
    name = "BEA"
    interval_seconds = 900.0

    API_URL = "https://apps.bea.gov/api/data"

    DATASETS = [
        {"DatasetName": "NIPA", "TableName": "T10101", "Frequency": "Q", "label": "GDP"},
        {"DatasetName": "NIPA", "TableName": "T20804", "Frequency": "M", "label": "PCE Price Index"},
    ]

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("BEA_API_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return
        try:
            async with aiohttp.ClientSession() as session:
                for ds in self.DATASETS:
                    params = {
                        "UserID": self.api_key,
                        "method": "GetData",
                        "DatasetName": ds["DatasetName"],
                        "TableName": ds["TableName"],
                        "Frequency": ds["Frequency"],
                        "Year": "LAST5",
                        "ResultFormat": "JSON",
                    }
                    try:
                        async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    results = data.get("BEAAPI", {}).get("Results", {})
                    rows = results.get("Data", [])
                    if not rows:
                        continue

                    # Get latest data point (last row with a real value)
                    for row in reversed(rows):
                        value = row.get("DataValue", "")
                        if not value or value == "---":
                            continue
                        period = row.get("TimePeriod", "")
                        line_desc = row.get("LineDescription", "")

                        key = f"bea-{ds['label']}-{period}-{value}"
                        if self._already_seen(key):
                            break

                        await self.emit({
                            "text": f"BEA {ds['label']}: {line_desc} = {value} ({period})",
                            "dataset": ds["label"],
                            "value": value,
                            "period": period,
                            "line": line_desc,
                            "extra_json": json.dumps({"dataset": ds["label"], "value": value, "period": period}),
                        })
                        break
        except Exception as e:
            log.warning("[BEA] Error: %s", e)


# ═══════════════════════════════════════════════════════
# CENSUS EITS API — housing, retail, durable goods, trade
# Free key. Primary source for tier-1 economic releases.
# ═══════════════════════════════════════════════════════

class CensusEitsSource(BaseSource):
    """Census EITS — housing starts, retail sales, durable goods. Free key."""
    name = "Census EITS"
    interval_seconds = 900.0

    ENDPOINTS = {
        "resconst": {"url": "https://api.census.gov/data/timeseries/eits/resconst", "label": "Housing Starts"},
        "marts": {"url": "https://api.census.gov/data/timeseries/eits/marts", "label": "Retail Sales"},
        "advm3": {"url": "https://api.census.gov/data/timeseries/eits/advm3", "label": "Durable Goods"},
    }

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("CENSUS_API_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return
        try:
            async with aiohttp.ClientSession() as session:
                for name, info in self.ENDPOINTS.items():
                    params = {
                        "get": "cell_value,time_slot_name,category_code",
                        "key": self.api_key,
                        "time": "from 2025",
                    }
                    try:
                        async with session.get(info["url"], params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    if not isinstance(data, list) or len(data) < 2:
                        continue

                    headers = data[0]
                    for row in data[-3:]:  # last 3 records
                        record = dict(zip(headers, row))
                        value = record.get("cell_value", "")
                        period = record.get("time_slot_name", record.get("time", ""))
                        category = record.get("category_code", "")

                        if not value or value == "N/A":
                            continue

                        key = f"census-{name}-{period}-{category}-{value}"
                        if self._already_seen(key):
                            continue

                        await self.emit({
                            "text": f"Census {info['label']}: {category} = {value} ({period})",
                            "dataset": info["label"],
                            "value": value,
                            "period": period,
                            "category": category,
                            "extra_json": json.dumps({"dataset": name, "value": value, "period": period}),
                        })
        except Exception as e:
            log.warning("[Census EITS] Error: %s", e)




# ═══════════════════════════════════════════════════════
# HACKER NEWS (Firebase API)
# No auth. Top stories + Algolia search for market terms.
# ═══════════════════════════════════════════════════════

class HackerNewsSource(BaseSource):
    """Hacker News top stories + market keyword search. No auth."""
    name = "Hacker News"
    interval_seconds = 120.0

    ALGOLIA_URL = "https://hn.algolia.com/api/v1/search_by_date"
    KEYWORDS = ["SEC", "Federal Reserve", "tariff", "sanctions", "bank run", "crypto regulation", "OPEC", "recession"]

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                for kw in self.KEYWORDS:
                    params = {"query": kw, "tags": "story", "hitsPerPage": "3"}
                    try:
                        async with session.get(self.ALGOLIA_URL, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    for hit in data.get("hits", [])[:2]:
                        oid = hit.get("objectID", "")
                        if not oid or self._already_seen(oid):
                            continue
                        title = hit.get("title", "")
                        points = hit.get("points", 0)
                        if points and int(points) < 10:
                            continue
                        await self.emit({
                            "text": f"HN: {title} ({points} pts)",
                            "title": title, "points": points,
                            "extra_json": json.dumps({"id": oid, "title": title, "points": points}),
                        })
        except Exception as e:
            log.warning("[Hacker News] Error: %s", e)


# ═══════════════════════════════════════════════════════
# NIFC WILDFIRE PERIMETERS (ArcGIS)
# No auth. Current US wildfire boundaries.
# ═══════════════════════════════════════════════════════

class NifcWildfireSource(BaseSource):
    """NIFC active wildfire perimeters — size, region, name. No auth."""
    name = "NIFC Wildfire"
    interval_seconds = 300.0

    API_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/Public_Wildfire_Perimeters_View/FeatureServer/0/query"

    async def poll(self) -> None:
        try:
            params = {"where": "1=1", "outFields": "poly_IncidentName,poly_GISAcres,poly_MapMethod,poly_DateCurrent", "returnGeometry": "false", "f": "json", "resultRecordCount": "10", "orderByFields": "poly_DateCurrent DESC"}
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            for feat in data.get("features", []):
                attr = feat.get("attributes", {})
                name = attr.get("poly_IncidentName", "")
                acres = attr.get("poly_GISAcres", 0)
                key = f"nifc-{name}-{int(acres or 0)}"
                if not name or self._already_seen(key):
                    continue
                await self.emit({
                    "text": f"NIFC: {name} wildfire — {acres:,.0f} acres" if acres else f"NIFC: {name} wildfire active",
                    "fire_name": name, "acres": acres,
                    "extra_json": json.dumps({"name": name, "acres": acres}),
                })
        except Exception as e:
            log.warning("[NIFC Wildfire] Error: %s", e)


# ═══════════════════════════════════════════════════════
# NASA EONET (Earth Observatory Natural Event Tracker)
# No auth. Global natural disasters in one feed.
# ═══════════════════════════════════════════════════════

class NasaEonetSource(BaseSource):
    """NASA EONET — global natural events (fires, volcanoes, storms). No auth."""
    name = "NASA EONET"
    interval_seconds = 300.0

    API_URL = "https://eonet.gsfc.nasa.gov/api/v3/events"

    async def poll(self) -> None:
        try:
            params = {"status": "open", "limit": "15"}
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            for event in data.get("events", []):
                eid = event.get("id", "")
                if not eid or self._already_seen(eid):
                    continue
                title = event.get("title", "")
                categories = ", ".join(c.get("title", "") for c in event.get("categories", []))
                await self.emit({
                    "text": f"NASA EONET: {title} [{categories}]",
                    "title": title, "categories": categories,
                    "extra_json": json.dumps({"id": eid, "title": title, "categories": categories}),
                })
        except Exception as e:
            log.warning("[NASA EONET] Error: %s", e)


# ═══════════════════════════════════════════════════════
# RELIEFWEB API (UN humanitarian intelligence)
# No auth. Every active conflict and disaster worldwide.
# ═══════════════════════════════════════════════════════

class ReliefWebSource(BaseSource):
    """ReliefWeb — UN-curated conflict and disaster intelligence. No auth."""
    name = "ReliefWeb"
    interval_seconds = 86400.0  # Blocked — daily poll to avoid wasted requests

    API_URL = "https://api.reliefweb.int/v2/reports"

    async def poll(self) -> None:
        try:
            params = {"appname": "apex-signal-trader", "limit": "10", "sort[]": "date:desc",
                      "fields[include][]": "title,date.created,country.name,disaster.name,source.name"}
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            for item in data.get("data", []):
                rid = str(item.get("id", ""))
                if not rid or self._already_seen(rid):
                    continue
                fields = item.get("fields", {})
                title = fields.get("title", "")
                countries = ", ".join(c.get("name", "") for c in fields.get("country", [])[:3])
                await self.emit({
                    "text": f"ReliefWeb: {title[:120]} ({countries})",
                    "title": title[:150], "countries": countries,
                    "extra_json": json.dumps({"id": rid, "title": title[:100], "countries": countries}),
                })
        except Exception as e:
            log.warning("[ReliefWeb] Error: %s", e)


# ═══════════════════════════════════════════════════════
# CELESTRAK (satellite/debris orbital elements)
# No auth. All 30,000+ tracked objects in space.
# ═══════════════════════════════════════════════════════

class CelesTrakSource(BaseSource):
    """CelesTrak — satellite catalog, Starlink, GPS, military recon. No auth."""
    name = "CelesTrak"
    interval_seconds = 3600.0  # every 2 hours

    API_URL = "https://celestrak.org/NORAD/elements/gp.php"

    GROUPS = {"active": "Active Satellites", "starlink": "Starlink", "gps-ops": "GPS Operational"}

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                for group, label in self.GROUPS.items():
                    params = {"GROUP": group, "FORMAT": "json"}
                    try:
                        async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    count = len(data) if isinstance(data, list) else 0
                    key = f"celestrak-{group}-{count}"
                    if self._already_seen(key):
                        continue
                    await self.emit({
                        "text": f"CelesTrak: {label} — {count:,} objects tracked",
                        "group": group, "count": count,
                        "extra_json": json.dumps({"group": group, "count": count}),
                    })
        except Exception as e:
            log.warning("[CelesTrak] Error: %s", e)


# ═══════════════════════════════════════════════════════
# NOAA NDBC OCEAN BUOYS
# No auth. Wind, wave height, pressure from 900+ buoys.
# ═══════════════════════════════════════════════════════

class NoaaBuoySource(BaseSource):
    """NOAA NDBC ocean buoys — wind, waves, pressure at key locations. No auth."""
    name = "NOAA Buoys"
    interval_seconds = 900.0

    STATIONS = {
        "42001": "Gulf of Mexico Central",
        "44025": "NY Bight",
        "46025": "Santa Monica Basin",
        "51000": "Hawaii",
    }

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                for sid, name in self.STATIONS.items():
                    url = f"https://www.ndbc.noaa.gov/data/realtime2/{sid}.txt"
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status != 200:
                                continue
                            text = await resp.text()
                    except Exception:
                        continue

                    lines = text.strip().split("\n")
                    if len(lines) < 3:
                        continue

                    # First 2 lines are headers, 3rd is latest data
                    headers = lines[0].split()
                    values = lines[2].split()
                    if len(values) < len(headers):
                        continue

                    row = dict(zip(headers, values))
                    wdir = row.get("WDIR", "")
                    wspd = row.get("WSPD", "")
                    wvht = row.get("WVHT", "")
                    pres = row.get("PRES", "")
                    atmp = row.get("ATMP", "")

                    key = f"buoy-{sid}-{wspd}-{wvht}-{pres}"
                    if self._already_seen(key):
                        continue

                    text_parts = [f"wind {wspd}m/s" if wspd != "MM" else None,
                                  f"waves {wvht}m" if wvht != "MM" else None,
                                  f"pressure {pres}mb" if pres != "MM" else None]
                    detail = ", ".join(p for p in text_parts if p)

                    await self.emit({
                        "text": f"NOAA Buoy {name}: {detail}",
                        "station": name, "wind_speed": wspd, "wave_height": wvht, "pressure": pres,
                        "extra_json": json.dumps({"station": sid, "wspd": wspd, "wvht": wvht, "pres": pres}),
                    })
        except Exception as e:
            log.warning("[NOAA Buoys] Error: %s", e)


# ═══════════════════════════════════════════════════════
# USGS VOLCANO HAZARDS
# No auth. Alert levels for 170 US volcanoes.
# ═══════════════════════════════════════════════════════

class UsgsVolcanoSource(BaseSource):
    """USGS Volcano Hazards — elevated alert levels. No auth."""
    name = "USGS Volcano"
    interval_seconds = 600.0

    API_URL = "https://volcanoes.usgs.gov/hans-public/api/volcano/getElevatedVolcanoes"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            volcanoes = data if isinstance(data, list) else data.get("result", data.get("volcanoes", []))
            for v in (volcanoes if isinstance(volcanoes, list) else []):
                if not isinstance(v, dict):
                    continue
                name = v.get("vName", v.get("volcanoName", ""))
                alert = v.get("alertLevel", v.get("alert_level", ""))
                color = v.get("colorCode", v.get("aviation_color_code", ""))
                key = f"volcano-{name}-{alert}"
                if not name or self._already_seen(key):
                    continue
                await self.emit({
                    "text": f"USGS Volcano: {name} — Alert: {alert}, Aviation: {color}",
                    "volcano": name, "alert_level": alert, "aviation_color": color,
                    "extra_json": json.dumps({"volcano": name, "alert": alert, "color": color}),
                })
        except Exception as e:
            log.warning("[USGS Volcano] Error: %s", e)


# ═══════════════════════════════════════════════════════
# BBC NEWS RSS
# No auth. Top stories, world, business.
# ═══════════════════════════════════════════════════════

class BbcNewsSource(BaseSource):
    """BBC News RSS — top stories, world, business. No auth."""
    name = "BBC News"
    interval_seconds = 120.0

    FEEDS = {
        "top": "https://feeds.bbci.co.uk/news/rss.xml",
        "world": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    }

    async def poll(self) -> None:
        loop = asyncio.get_event_loop()
        for feed_name, url in self.FEEDS.items():
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, url)
                for entry in feed.entries[:5]:
                    if not self._is_fresh_rss(entry):
                        continue
                    uid = entry.get("id") or entry.get("link", "")
                    if self._already_seen(uid):
                        continue
                    title = entry.get("title", "")
                    await self.emit({
                        "text": f"BBC [{feed_name}]: {title}",
                        "title": title, "feed": feed_name,
                        "link": entry.get("link", ""),
                        "extra_json": json.dumps({"uid": uid, "feed": feed_name}),
                    })
            except Exception as e:
                log.warning("[BBC News] %s error: %s", feed_name, e)


# ═══════════════════════════════════════════════════════
# UK CARBON INTENSITY API
# No auth. UK grid generation mix by fuel type.
# ═══════════════════════════════════════════════════════

class UkCarbonSource(BaseSource):
    """UK Carbon Intensity — grid generation mix by fuel. No auth."""
    name = "UK Carbon"
    interval_seconds = 900.0

    GEN_URL = "https://api.carbonintensity.org.uk/generation"
    INTENSITY_URL = "https://api.carbonintensity.org.uk/intensity"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.GEN_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                data_block = data.get("data", {})
                # API may return {"data": {"generationmix": [...]}} or {"data": [{"generationmix": [...]}]}
                if isinstance(data_block, list) and data_block:
                    data_block = data_block[0]
                gen_mix = data_block.get("generationmix", []) if isinstance(data_block, dict) else []
                if not gen_mix:
                    return

                summary = ", ".join(f"{g.get('fuel','')}: {g.get('perc',0):.0f}%" for g in gen_mix[:5])
                gas_pct = next((g.get("perc", 0) for g in gen_mix if g.get("fuel") == "gas"), 0)

                key = f"ukcarbon-{int(gas_pct)}"
                if self._already_seen(key):
                    return

                await self.emit({
                    "text": f"UK Grid: {summary} (gas={gas_pct:.0f}%)",
                    "gas_pct": gas_pct,
                    "extra_json": json.dumps({"mix": {g["fuel"]: g["perc"] for g in gen_mix}}),
                })
        except Exception as e:
            log.warning("[UK Carbon] Error: %s", e)


# ═══════════════════════════════════════════════════════
# COPERNICUS EMERGENCY MANAGEMENT SERVICE
# No auth. Satellite-derived damage footprints.
# ═══════════════════════════════════════════════════════

class CopernicusEmsSource(BaseSource):
    """Copernicus EMS — satellite damage assessment for global disasters. No auth."""
    name = "Copernicus EMS"
    interval_seconds = 1800.0

    API_URL = "https://mapping.emergency.copernicus.eu/activations/api/activations/"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            activations = data if isinstance(data, list) else data.get("results", data.get("activations", []))
            for act in (activations if isinstance(activations, list) else [])[:10]:
                if not isinstance(act, dict):
                    continue
                code = act.get("code", act.get("id", ""))
                title = act.get("title", act.get("name", ""))
                country = act.get("country", act.get("countries", ""))
                event_type = act.get("type", act.get("event_type", ""))

                if not code or self._already_seen(code):
                    continue

                await self.emit({
                    "text": f"Copernicus EMS: {title} ({event_type}) — {country}",
                    "code": code, "title": title, "event_type": event_type,
                    "extra_json": json.dumps({"code": code, "title": title[:100], "type": event_type}),
                })
        except Exception as e:
            log.warning("[Copernicus EMS] Error: %s", e)


# ═══════════════════════════════════════════════════════
# AL JAZEERA RSS
# No auth. Middle East, Africa, Asia coverage.
# ═══════════════════════════════════════════════════════

class AlJazeeraSource(BaseSource):
    """Al Jazeera RSS — Middle East, Africa, Asia news. No auth."""
    name = "Al Jazeera"
    interval_seconds = 300.0

    RSS_URL = "https://www.aljazeera.com/xml/rss/all.xml"

    async def poll(self) -> None:
        try:
            loop = asyncio.get_event_loop()
            feed = await loop.run_in_executor(None, feedparser.parse, self.RSS_URL)
            for entry in feed.entries[:10]:
                if not self._is_fresh_rss(entry):
                    continue
                uid = entry.get("id") or entry.get("link", "")
                if self._already_seen(uid):
                    continue
                title = entry.get("title", "")
                await self.emit({
                    "text": f"Al Jazeera: {title}",
                    "title": title,
                    "link": entry.get("link", ""),
                    "extra_json": json.dumps({"uid": uid}),
                })
        except Exception as e:
            log.warning("[Al Jazeera] Error: %s", e)


# ═══════════════════════════════════════════════════════
# US DROUGHT MONITOR
# No auth. Weekly D0-D4 drought severity.
# ═══════════════════════════════════════════════════════

class DroughtMonitorSource(BaseSource):
    """US Drought Monitor — D0-D4 severity for grain/energy impact. No auth."""
    name = "US Drought"
    interval_seconds = 3600.0  # every 2 hours (data is weekly Thursday)

    API_URL = "https://usdmdataservices.unl.edu/api/USStatistics/GetDroughtSeverityStatisticsByArea"

    async def poll(self) -> None:
        try:
            now = datetime.datetime.utcnow()
            start = (now - datetime.timedelta(days=14)).strftime("%m/%d/%Y")
            end = now.strftime("%m/%d/%Y")

            params = {"aoi": "us", "startdate": start, "enddate": end, "statisticsType": "1"}
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    text = await resp.text()

            # API returns CSV: MapDate,AreaOfInterest,None,D0,D1,D2,D3,D4,ValidStart,ValidEnd,StatisticFormatID
            lines = text.strip().split("\n")
            if len(lines) < 2:
                return
            headers_row = [h.strip() for h in lines[0].split(",")]
            for line in lines[-2:]:
                cols = line.split(",")
                if len(cols) < len(headers_row):
                    continue
                row = dict(zip(headers_row, cols))
                date = row.get("MapDate", "").strip()
                d0 = row.get("D0", "0").strip()
                d1 = row.get("D1", "0").strip()
                d2 = row.get("D2", "0").strip()
                d3 = row.get("D3", "0").strip()
                d4 = row.get("D4", "0").strip()

                key = f"drought-{date}"
                if not date or self._already_seen(key):
                    continue

                await self.emit({
                    "text": f"US Drought: D0={d0}% D1={d1}% D2={d2}% D3={d3}% D4={d4}% ({date})",
                    "d0": d0, "d1": d1, "d2": d2, "d3": d3, "d4": d4, "date": date,
                    "extra_json": json.dumps({"d0": d0, "d1": d1, "d2": d2, "d3": d3, "d4": d4, "date": date}),
                })
        except Exception as e:
            log.warning("[US Drought] Error: %s", e)


# ═══════════════════════════════════════════════════════
# AISSTREAM.IO — Real-time global vessel tracking (REST poll)
# Free API key. Vessel positions, cargo type, destination.
# ═══════════════════════════════════════════════════════

class AisStreamSource(BaseSource):
    """AISStream — vessel positions near key energy chokepoints. Free key."""
    name = "AISStream"
    interval_seconds = 30.0

    # REST search endpoint for vessels in bounding boxes
    API_URL = "https://api.aisstream.io/v0/search"

    # Key maritime chokepoints for energy trading
    WATCH_ZONES = {
        "Strait of Hormuz": [[25.0, 55.0], [27.0, 57.0]],
        "Suez Canal": [[29.5, 32.0], [31.5, 33.0]],
        "Houston Ship Channel": [[29.3, -95.5], [29.8, -94.5]],
    }

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("AISSTREAM_API_KEY", "")
        self._last_counts = {}

    async def poll(self) -> None:
        if not self.api_key:
            return

        try:
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            async with aiohttp.ClientSession() as session:
                for zone_name, bbox in self.WATCH_ZONES.items():
                    payload = {
                        "BoundingBoxes": [bbox],
                        "FilterMessageTypes": ["PositionReport"],
                    }
                    try:
                        async with session.post(
                            self.API_URL,
                            json=payload,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status != 200:
                                # Try GET fallback — some API versions differ
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    vessels = data if isinstance(data, list) else data.get("data", data.get("vessels", []))
                    count = len(vessels) if isinstance(vessels, list) else 0

                    last = self._last_counts.get(zone_name, 0)
                    self._last_counts[zone_name] = count

                    key = f"ais-{zone_name}-{count}"
                    if self._already_seen(key):
                        continue

                    delta = count - last
                    delta_text = f" ({delta:+d})" if last > 0 else ""

                    await self.emit({
                        "text": f"AISStream: {zone_name} — {count} vessels{delta_text}",
                        "zone": zone_name,
                        "vessel_count": count,
                        "extra_json": json.dumps({"zone": zone_name, "count": count, "delta": delta}),
                    })

        except Exception as e:
            log.warning("[AISStream] Error: %s", e)


# ═══════════════════════════════════════════════════════
# AIRNOW — US Air Quality Index
# Free API key. AQI at key industrial/population centers.
# ═══════════════════════════════════════════════════════

class AirNowSource(BaseSource):
    """AirNow — AQI readings at key US locations. Free key."""
    name = "AirNow"
    interval_seconds = 1800.0  # hourly

    API_URL = "https://www.airnowapi.org/aq/observation/latLong/current/"

    # Key locations: Houston (refineries), LA (ports), NYC (finance), Chicago (CBOT)
    LOCATIONS = {
        "Houston": {"lat": 29.76, "lon": -95.37},
        "Los Angeles": {"lat": 34.05, "lon": -118.24},
        "New York": {"lat": 40.71, "lon": -74.01},
        "Chicago": {"lat": 41.88, "lon": -87.63},
    }

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("AIRNOW_API_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return

        try:
            async with aiohttp.ClientSession() as session:
                for city, coords in self.LOCATIONS.items():
                    params = {
                        "format": "application/json",
                        "API_KEY": self.api_key,
                        "latitude": coords["lat"],
                        "longitude": coords["lon"],
                        "distance": "25",
                    }
                    try:
                        async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    for reading in (data if isinstance(data, list) else []):
                        aqi = reading.get("AQI", 0)
                        param = reading.get("ParameterName", "")
                        category = reading.get("Category", {}).get("Name", "")

                        key = f"airnow-{city}-{param}-{aqi}"
                        if self._already_seen(key):
                            continue

                        alert = ""
                        if aqi > 150:
                            alert = " [UNHEALTHY]"
                        elif aqi > 200:
                            alert = " [VERY UNHEALTHY]"
                        elif aqi > 300:
                            alert = " [HAZARDOUS]"

                        await self.emit({
                            "text": f"AirNow: {city} {param} AQI={aqi} ({category}){alert}",
                            "city": city,
                            "parameter": param,
                            "aqi": aqi,
                            "category": category,
                            "extra_json": json.dumps({"city": city, "param": param, "aqi": aqi}),
                        })

        except Exception as e:
            log.warning("[AirNow] Error: %s", e)


# ═══════════════════════════════════════════════════════
# ACLED CONFLICT DATA
# Free registration. Battles, protests, explosions geolocated.
# ═══════════════════════════════════════════════════════

class AcledSource(BaseSource):
    """ACLED — geolocated conflict events worldwide. OAuth2 token auth."""
    name = "ACLED"
    interval_seconds = 1800.0  # hourly (data is weekly)

    TOKEN_URL = "https://acleddata.com/oauth/token"
    API_URL = "https://acleddata.com/api/acled/read"

    def __init__(self, queue: asyncio.Queue, email: str = "", password: str = "", **_):
        super().__init__(queue)
        self.email = email or os.environ.get("ACLED_EMAIL", "")
        self.password = password or os.environ.get("ACLED_PASSWORD", "")
        self._token = None
        self._token_expires = 0

    async def _get_token(self, session) -> str:
        """Get OAuth2 access token (valid 24h)."""
        import time
        if self._token and time.time() < self._token_expires:
            return self._token

        data = {
            "username": self.email,
            "password": self.password,
            "grant_type": "password",
            "client_id": "acled",
        }
        async with session.post(
            self.TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                log.warning("[ACLED] Token request failed: HTTP %s", resp.status)
                return ""
            result = await resp.json()

        self._token = result.get("access_token", "")
        # Token valid 24h, refresh at 23h
        self._token_expires = time.time() + 82800
        return self._token

    async def poll(self) -> None:
        if not self.email or not self.password:
            return

        try:
            now = datetime.datetime.utcnow()
            week_ago = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d")

            async with aiohttp.ClientSession() as session:
                token = await self._get_token(session)
                if not token:
                    return

                headers = {"Authorization": f"Bearer {token}"}
                params = {
                    "event_date": week_ago,
                    "event_date_where": ">=",
                    "limit": "20",
                }

                async with session.get(self.API_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        log.warning("[ACLED] API request failed: HTTP %s", resp.status)
                        return
                    data = await resp.json()

            events = data.get("data", data) if isinstance(data, dict) else data
            for event in (events if isinstance(events, list) else [])[:10]:
                if not isinstance(event, dict):
                    continue
                eid = event.get("event_id_cnty", event.get("event_id", ""))
                if not eid or self._already_seen(str(eid)):
                    continue

                event_type = event.get("event_type", "")
                country = event.get("country", "")
                region = event.get("admin1", "")
                fatalities = event.get("fatalities", 0)
                date = event.get("event_date", "")

                await self.emit({
                    "text": f"ACLED: {event_type} in {country}/{region} — {fatalities} fatalities ({date})",
                    "event_type": event_type,
                    "country": country,
                    "fatalities": fatalities,
                    "extra_json": json.dumps({
                        "id": eid, "type": event_type,
                        "country": country, "fatalities": fatalities,
                    }),
                })

        except Exception as e:
            log.warning("[ACLED] Error: %s", e)




# ═══════════════════════════════════════════════════════
# CNN FEAR & GREED INDEX (undocumented endpoint)
# No auth. Intraday 7-component equity sentiment composite.
# ═══════════════════════════════════════════════════════

class CnnFearGreedSource(BaseSource):
    """CNN Fear & Greed — 7-component equity sentiment (0-100). No auth."""
    name = "CNN Fear&Greed"
    interval_seconds = 86400.0  # Blocked — daily poll to avoid wasted requests

    async def poll(self) -> None:
        try:
            today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
            url = f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{today}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            fg = data.get("fear_and_greed", {})
            score = fg.get("score", 0)
            rating = fg.get("rating", "")
            prev_close = fg.get("previous_close", 0)

            key = f"cnn-fg-{int(score)}"
            if self._already_seen(key):
                return

            delta = score - prev_close if prev_close else 0
            await self.emit({
                "text": f"CNN Fear&Greed: {score:.0f}/100 ({rating}) {delta:+.1f} from prev close",
                "score": score, "rating": rating, "delta": delta,
                "extra_json": json.dumps({"score": score, "rating": rating, "delta": round(delta, 1)}),
            })
        except Exception as e:
            log.warning("[CNN Fear&Greed] Error: %s", e)


# ═══════════════════════════════════════════════════════
# STOCKTWITS — pre-labeled bull/bear per ticker
# No auth. Self-tagged sentiment on every message.
# ═══════════════════════════════════════════════════════

class StockTwitsSource(BaseSource):
    """StockTwits — pre-labeled bull/bear messages per ticker. No auth."""
    name = "StockTwits"
    interval_seconds = 60.0

    API_URL = "https://api.stocktwits.com/api/2/streams/symbol"
    TICKERS = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA", "BTC.X", "ETH.X"]

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                for ticker in self.TICKERS:
                    url = f"{self.API_URL}/{ticker}.json"
                    try:
                        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    messages = data.get("messages", [])
                    bulls = sum(1 for m in messages if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish")
                    bears = sum(1 for m in messages if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bearish")
                    total = len(messages)

                    key = f"stwits-{ticker}-{bulls}-{bears}"
                    if self._already_seen(key):
                        continue

                    ratio = bulls / max(bulls + bears, 1)
                    signal = ""
                    if ratio > 0.8:
                        signal = " [EXTREME BULLISH]"
                    elif ratio < 0.3:
                        signal = " [EXTREME BEARISH]"

                    await self.emit({
                        "text": f"StockTwits {ticker}: {bulls}B/{bears}B of {total} msgs ({ratio:.0%} bullish){signal}",
                        "ticker": ticker, "bulls": bulls, "bears": bears, "ratio": ratio,
                        "extra_json": json.dumps({"ticker": ticker, "bulls": bulls, "bears": bears, "ratio": round(ratio, 2)}),
                    })
        except Exception as e:
            log.warning("[StockTwits] Error: %s", e)


# ═══════════════════════════════════════════════════════
# 4CHAN /BIZ/ — anonymous unfiltered crypto/retail sentiment
# No auth. 1 req/sec limit. Raw narrative detection.
# ═══════════════════════════════════════════════════════

class FourChanBizSource(BaseSource):
    """4chan /biz/ — anonymous crypto/retail sentiment. No auth."""
    name = "4chan /biz/"
    interval_seconds = 120.0

    CATALOG_URL = "https://a.4cdn.org/biz/catalog.json"

    # Keywords that signal market-relevant discussion
    MARKET_KEYWORDS = ["bitcoin", "btc", "ethereum", "eth", "crash", "moon", "pump", "dump",
                       "recession", "fed", "inflation", "tariff", "short", "bull", "bear"]

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.CATALOG_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            for page in (data if isinstance(data, list) else []):
                for thread in page.get("threads", [])[:5]:
                    tid = thread.get("no", 0)
                    if not tid or self._already_seen(str(tid)):
                        continue

                    sub = (thread.get("sub", "") or "").lower()
                    com = (thread.get("com", "") or "").lower()
                    replies = thread.get("replies", 0)
                    text_combined = f"{sub} {com}"

                    # Only emit threads with market keywords and decent engagement
                    if replies < 5:
                        continue
                    if not any(kw in text_combined for kw in self.MARKET_KEYWORDS):
                        continue

                    # Clean HTML from comment
                    import re
                    clean_sub = re.sub(r'<[^>]+>', '', thread.get("sub", "") or "")[:80]
                    clean_com = re.sub(r'<[^>]+>', '', thread.get("com", "") or "")[:80]
                    display = clean_sub or clean_com

                    await self.emit({
                        "text": f"/biz/: {display} ({replies} replies)",
                        "thread_id": tid, "replies": replies,
                        "extra_json": json.dumps({"thread": tid, "replies": replies}),
                    })
        except Exception as e:
            log.warning("[4chan /biz/] Error: %s", e)


# ═══════════════════════════════════════════════════════
# ALTERNATIVE.ME CRYPTO FEAR & GREED INDEX
# No auth. Daily composite BTC sentiment (0-100).
# ═══════════════════════════════════════════════════════

class CryptoFearGreedSource(BaseSource):
    """Alternative.me Crypto Fear & Greed — daily BTC sentiment. No auth."""
    name = "Crypto F&G"
    interval_seconds = 1800.0

    API_URL = "https://api.alternative.me/fng/?limit=1"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            items = data.get("data", [])
            if not items:
                return

            item = items[0]
            value = int(item.get("value", 0))
            classification = item.get("value_classification", "")
            timestamp = item.get("timestamp", "")

            key = f"crypto-fg-{timestamp}"
            if self._already_seen(key):
                return

            signal = ""
            if value <= 20:
                signal = " [EXTREME FEAR — contrarian BUY zone]"
            elif value >= 80:
                signal = " [EXTREME GREED — contrarian SELL zone]"

            await self.emit({
                "text": f"Crypto F&G: {value}/100 ({classification}){signal}",
                "value": value, "classification": classification,
                "extra_json": json.dumps({"value": value, "class": classification}),
            })
        except Exception as e:
            log.warning("[Crypto F&G] Error: %s", e)


# ═══════════════════════════════════════════════════════
# SENTICRYPT — BTC Twitter NLP sentiment (2-hour updates)
# No auth. Scores -1.0 to +1.0.
# ═══════════════════════════════════════════════════════

class SentiCryptSource(BaseSource):
    """SentiCrypt — BTC Twitter NLP sentiment scores. No auth."""
    name = "SentiCrypt"
    interval_seconds = 3600.0  # every 2 hours (matches update frequency)

    API_URL = "https://api.senticrypt.com/v2/latest.json"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            if not data:
                return

            # Latest entry
            latest = data[-1] if isinstance(data, list) else data
            mean = float(latest.get("mean", 0) or 0)
            median = float(latest.get("median", 0) or 0)
            rate = latest.get("rate", 0)  # post volume
            ts = latest.get("date", latest.get("timestamp", ""))

            key = f"senticrypt-{ts}"
            if self._already_seen(key):
                return

            signal = ""
            if mean > 0.3:
                signal = " [STRONGLY POSITIVE]"
            elif mean < -0.3:
                signal = " [STRONGLY NEGATIVE]"

            await self.emit({
                "text": f"SentiCrypt: BTC sentiment mean={mean:+.3f} median={median:+.3f} vol={rate}{signal}",
                "mean": mean, "median": median, "rate": rate,
                "extra_json": json.dumps({"mean": mean, "median": median, "rate": rate}),
            })
        except Exception as e:
            log.warning("[SentiCrypt] Error: %s", e)


# ═══════════════════════════════════════════════════════
# KALSHI PUBLIC API — real-money regulated event contracts
# No auth for reads. CFTC-regulated prediction market.
# ═══════════════════════════════════════════════════════

class KalshiSource(BaseSource):
    """Kalshi — real-money regulated event contracts (CPI, GDP, Fed, weather). No auth."""
    name = "Kalshi"
    interval_seconds = 60.0

    API_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"

    TRACKED_KEYWORDS = ["fed", "cpi", "inflation", "gdp", "recession", "unemployment",
                        "rate", "bitcoin", "oil", "hurricane", "shutdown", "tariff",
                        "war", "iran", "china", "default", "debt ceiling"]

    # Filter OUT sports/entertainment noise
    EXCLUDE_KEYWORDS = ["nfl", "nba", "nhl", "mlb", "soccer", "football", "baseball",
                        "basketball", "hockey", "panthers", "sabres", "hurricanes",
                        "lakers", "celtics", "warriors", "super bowl", "playoff",
                        "oscar", "grammy", "emmy", "bachelor"]

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                params = {"limit": "50", "status": "open"}
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            markets = data.get("markets", [])
            for m in markets:
                ticker = m.get("ticker", "")
                title = m.get("title", m.get("event_title", ""))
                yes_price = m.get("yes_bid", m.get("last_price", 0))
                volume = m.get("volume", 0)

                if not title:
                    continue

                t_lower = title.lower()

                # Filter out sports/entertainment noise
                if any(ex in t_lower for ex in self.EXCLUDE_KEYWORDS):
                    continue

                # Only keep macro/financial markets
                relevant = any(kw in t_lower for kw in self.TRACKED_KEYWORDS)
                if not relevant:
                    continue

                # Skip zero-volume markets
                if volume is not None and int(volume or 0) == 0:
                    continue

                key = f"kalshi-{ticker}-{yes_price}"
                if self._already_seen(key):
                    continue

                prob = float(yes_price) * 100 if yes_price and float(yes_price) <= 1 else float(yes_price or 0)

                await self.emit({
                    "text": f"Kalshi: \"{title[:80]}\" at {prob:.0f}% (vol: {volume})",
                    "ticker": ticker, "title": title[:120], "probability": prob, "volume": volume,
                    "extra_json": json.dumps({"ticker": ticker, "prob": round(prob, 1), "vol": volume}),
                })
        except Exception as e:
            log.warning("[Kalshi] Error: %s", e)


# ═══════════════════════════════════════════════════════
# METACULUS — calibrated long-horizon forecasts
# No auth for public questions. ML-weighted predictions.
# ═══════════════════════════════════════════════════════

class MetaculusSource(BaseSource):
    """Metaculus — calibrated crowd forecasts on geopolitics/economics. No auth."""
    name = "Metaculus"
    interval_seconds = 86400.0  # Blocked — daily poll to avoid wasted requests

    API_URL = "https://www.metaculus.com/api2/questions/"

    TRACKED_KEYWORDS = ["nuclear", "recession", "war", "pandemic", "inflation",
                        "fed", "bitcoin", "ai", "china", "taiwan", "oil"]

    async def poll(self) -> None:
        try:
            params = {"status": "open", "order_by": "-last_prediction_time", "limit": "30"}
            headers = {"Accept": "application/json"}
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            results = data.get("results", data) if isinstance(data, dict) else data
            for q in (results if isinstance(results, list) else []):
                qid = q.get("id", "")
                title = q.get("title", "")[:120]
                prediction = q.get("community_prediction", {})

                if not title:
                    continue

                t_lower = title.lower()
                relevant = any(kw in t_lower for kw in self.TRACKED_KEYWORDS)
                if not relevant:
                    continue

                # Get probability (binary) or median (continuous)
                prob = None
                if isinstance(prediction, dict):
                    prob = prediction.get("full", {}).get("q2") or prediction.get("q2")
                elif isinstance(prediction, (int, float)):
                    prob = prediction

                key = f"metaculus-{qid}-{prob}"
                if self._already_seen(key):
                    continue

                prob_text = f" — {float(prob)*100:.0f}%" if prob and float(prob) <= 1 else (f" — {prob}" if prob else "")

                await self.emit({
                    "text": f"Metaculus: \"{title}\"{prob_text}",
                    "question_id": qid, "title": title, "prediction": prob,
                    "extra_json": json.dumps({"id": qid, "title": title[:80], "pred": prob}),
                })
        except Exception as e:
            log.warning("[Metaculus] Error: %s", e)


# ═══════════════════════════════════════════════════════
# PREDICTIT — US political market prices
# No auth. Real-money contracts (~180 active markets).
# ═══════════════════════════════════════════════════════

class PredictItSource(BaseSource):
    """PredictIt — US political market prices. No auth. Delta-only: >2% change."""
    name = "PredictIt"
    interval_seconds = 120.0

    API_URL = "https://www.predictit.org/api/marketdata/all/"

    def __init__(self, queue: asyncio.Queue):
        super().__init__(queue)
        self._last_prices = {}  # market_id -> last price

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            markets = data.get("markets", [])
            for m in markets[:20]:  # top 20 by volume
                mid = m.get("id", "")
                name = m.get("name", m.get("shortName", ""))[:100]
                contracts = m.get("contracts", [])

                if not contracts:
                    continue

                # Get the top contract
                top = max(contracts, key=lambda c: c.get("lastTradePrice", 0) or 0)
                contract_name = top.get("name", top.get("shortName", ""))[:60]
                price = top.get("lastTradePrice", 0)

                prob = float(price) * 100 if price else 0

                # Only emit on >2% probability change
                last_prob = self._last_prices.get(mid)
                if last_prob is not None and abs(prob - last_prob) < 2.0:
                    continue
                self._last_prices[mid] = prob

                key = f"predictit-{mid}-{int(prob)}"
                if self._already_seen(key):
                    continue

                await self.emit({
                    "text": f"PredictIt: \"{name}\" — {contract_name} at {prob:.0f}%",
                    "market_id": mid, "market": name, "contract": contract_name, "probability": prob,
                    "extra_json": json.dumps({"id": mid, "market": name[:60], "prob": round(prob, 1)}),
                })
        except Exception as e:
            log.warning("[PredictIt] Error: %s", e)


# ═══════════════════════════════════════════════════════
# MANIFOLD MARKETS — calibrated prediction market (REST)
# No auth for reads. Fast-spinning markets on breaking events.
# ═══════════════════════════════════════════════════════

class ManifoldSource(BaseSource):
    """Manifold Markets — prediction markets on breaking events. No auth."""
    name = "Manifold"
    interval_seconds = 60.0

    API_URL = "https://api.manifold.markets/v0/markets"

    TRACKED = ["fed", "recession", "nuclear", "war", "bitcoin", "trump",
               "election", "inflation", "ai", "china", "tariff"]

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                params = {"limit": "50", "sort": "updated-time"}
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            for m in (data if isinstance(data, list) else []):
                mid = m.get("id", "")
                question = m.get("question", "")[:120]
                prob = m.get("probability", 0)
                volume = m.get("volume", 0)

                if not question:
                    continue

                q_lower = question.lower()
                relevant = any(kw in q_lower for kw in self.TRACKED)
                if not relevant:
                    continue

                prob_pct = float(prob) * 100 if prob and float(prob) <= 1 else float(prob or 0)

                key = f"manifold-{mid}-{int(prob_pct)}"
                if self._already_seen(key):
                    continue

                await self.emit({
                    "text": f"Manifold: \"{question}\" at {prob_pct:.0f}% (vol: ${volume:,.0f})",
                    "market_id": mid, "question": question, "probability": prob_pct, "volume": volume,
                    "extra_json": json.dumps({"id": mid, "question": question[:80], "prob": round(prob_pct, 1)}),
                })
        except Exception as e:
            log.warning("[Manifold] Error: %s", e)


# ═══════════════════════════════════════════════════════
# FINNHUB — news + social + insider sentiment fusion
# Free key. 60 calls/min. Multi-signal sentiment per ticker.
# ═══════════════════════════════════════════════════════

class FinnhubSource(BaseSource):
    """Finnhub — news sentiment + social sentiment + insider MSPR. Free key."""
    name = "Finnhub"
    interval_seconds = 120.0

    BASE_URL = "https://finnhub.io/api/v1"
    TICKERS = ["AAPL", "TSLA", "NVDA", "MSFT", "AMZN"]

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("FINNHUB_API_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return
        try:
            async with aiohttp.ClientSession() as session:
                for ticker in self.TICKERS:
                    # Quote endpoint (news-sentiment returns 403)
                    url = f"{self.BASE_URL}/quote?symbol={ticker}&token={self.api_key}"
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    price = data.get("c", 0)  # current price
                    change = data.get("d", 0)  # change
                    pct = data.get("dp", 0)  # percent change
                    high = data.get("h", 0)  # daily high
                    low = data.get("l", 0)  # daily low
                    prev = data.get("pc", 0)  # previous close

                    key = f"finnhub-{ticker}-{price}-{change}"
                    if self._already_seen(key):
                        continue

                    if price:
                        await self.emit({
                            "text": f"Finnhub {ticker}: ${price:.2f} ({change:+.2f}, {pct:+.1f}%) H={high:.2f} L={low:.2f}",
                            "ticker": ticker, "price": price, "change": change, "pct_change": pct, "high": high, "low": low,
                            "extra_json": json.dumps({"ticker": ticker, "price": price, "change": change, "pct": pct, "high": high, "low": low}),
                        })
        except Exception as e:
            log.warning("[Finnhub] Error: %s", e)


# ═══════════════════════════════════════════════════════
# NEWSAPI.AI — article-level sentiment + event clustering
# Free key. 2000 searches/month, 200K articles.
# ═══════════════════════════════════════════════════════

class NewsApiAiSource(BaseSource):
    """NewsAPI.ai — article sentiment + event clustering. Free key."""
    name = "NewsAPI.ai"
    interval_seconds = 1800.0  # conserve quota

    API_URL = "https://eventregistry.org/api/v1/article/getArticles"

    QUERIES = ["Federal Reserve", "crude oil sanctions", "tariff trade war", "crypto regulation"]

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("NEWSAPI_AI_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return
        try:
            async with aiohttp.ClientSession() as session:
                for query in self.QUERIES:
                    payload = {
                        "apiKey": self.api_key,
                        "keyword": query,
                        "lang": "eng",
                        "articlesCount": 3,
                        "articlesSortBy": "date",
                        "includeArticleSentiment": True,
                        "resultType": "articles",
                    }
                    try:
                        async with session.post(self.API_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    articles = data.get("articles", {}).get("results", [])
                    for art in articles:
                        uri = art.get("uri", "")
                        if not uri or self._already_seen(uri):
                            continue
                        title = art.get("title", "")[:120]
                        sentiment = art.get("sentiment", 0)
                        source_name = art.get("source", {}).get("title", "")

                        try:
                            sent_val = float(sentiment or 0)
                        except (ValueError, TypeError):
                            sent_val = 0

                        await self.emit({
                            "text": f"NewsAPI.ai: {title} (sentiment: {sent_val:+.2f}, via {source_name})",
                            "title": title, "sentiment": sent_val, "source_name": source_name,
                            "extra_json": json.dumps({"uri": uri, "sentiment": sent_val, "source": source_name}),
                        })
        except Exception as e:
            log.warning("[NewsAPI.ai] Error: %s", e)


# ═══════════════════════════════════════════════════════
# MARKETAUX — entity-level per-ticker news sentiment
# Free key. 100 req/day. Per-entity sentiment scores.
# ═══════════════════════════════════════════════════════

class MarketAuxSource(BaseSource):
    """MarketAux — per-ticker entity-level news sentiment. Free key."""
    name = "MarketAux"
    interval_seconds = 3600.0  # conserve 100 req/day limit

    API_URL = "https://api.marketaux.com/v1/news/all"

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("MARKETAUX_API_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return
        try:
            params = {
                "api_token": self.api_key,
                "symbols": "AAPL,TSLA,NVDA,MSFT,AMZN",
                "filter_entities": "true",
                "language": "en",
                "limit": "5",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            for art in data.get("data", []):
                uuid = art.get("uuid", "")
                if not uuid or self._already_seen(uuid):
                    continue
                title = art.get("title", "")[:120]
                entities = art.get("entities", [])

                # Per-entity sentiment
                entity_sentiments = []
                for ent in entities[:3]:
                    sym = ent.get("symbol", "")
                    sent = ent.get("sentiment_score", 0)
                    if sym:
                        entity_sentiments.append(f"{sym}:{sent:+.2f}")

                ent_text = ", ".join(entity_sentiments) if entity_sentiments else "no entities"

                await self.emit({
                    "text": f"MarketAux: {title} [{ent_text}]",
                    "title": title, "entities": entity_sentiments,
                    "extra_json": json.dumps({"uuid": uuid, "entities": entity_sentiments}),
                })
        except Exception as e:
            log.warning("[MarketAux] Error: %s", e)


# ═══════════════════════════════════════════════════════
# CURRENTS API — high-volume raw article text
# Free key. 1000 req/day. 43K+ sources, 70+ countries.
# ═══════════════════════════════════════════════════════

class CurrentsApiSource(BaseSource):
    """Currents API — high-volume news for NLP pipeline. Free key."""
    name = "Currents API"
    interval_seconds = 1800.0

    API_URL = "https://api.currentsapi.services/v1/latest-news"

    KEYWORDS = ["oil", "federal reserve", "tariff", "sanctions", "bitcoin", "recession"]

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("CURRENTS_API_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return
        try:
            async with aiohttp.ClientSession() as session:
                for kw in self.KEYWORDS[:2]:  # conserve quota
                    params = {"apiKey": self.api_key, "keywords": kw, "language": "en"}
                    try:
                        async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    for art in data.get("news", [])[:3]:
                        aid = art.get("id", art.get("url", ""))
                        if not aid or self._already_seen(aid):
                            continue
                        title = art.get("title", "")[:120]
                        source_name = art.get("author", "")

                        await self.emit({
                            "text": f"Currents: {title} (via {source_name})",
                            "title": title, "source_name": source_name,
                            "extra_json": json.dumps({"id": aid, "title": title[:80]}),
                        })
        except Exception as e:
            log.warning("[Currents API] Error: %s", e)




# ═══════════════════════════════════════════════════════
# NY FED MARKETS API — SOFR/EFFR/repo rates
# No auth. The definitive overnight funding rate complex.
# ═══════════════════════════════════════════════════════

class NyFedRatesSource(BaseSource):
    """NY Fed Markets — SOFR, EFFR, OBFR, repo ops. No auth."""
    name = "NY Fed Rates"
    interval_seconds = 300.0

    RATES_URL = "https://markets.newyorkfed.org/api/rates/all/latest.json"
    RRP_URL = "https://markets.newyorkfed.org/api/rp/reverserepo/all/results/lastTwoWeeks.json"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                # All overnight rates
                async with session.get(self.RATES_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                rates = data.get("refRates", data.get("rates", []))
                if isinstance(rates, list):
                    for r in rates:
                        rate_type = r.get("type", "")
                        rate_val = r.get("percentRate", r.get("rate", ""))
                        eff_date = r.get("effectiveDate", "")
                        volume = r.get("volumeInBillions", "")

                        key = f"nyfed-{rate_type}-{eff_date}-{rate_val}"
                        if not rate_type or self._already_seen(key):
                            continue

                        vol_text = f", vol ${volume}B" if volume else ""
                        await self.emit({
                            "text": f"NY Fed: {rate_type} = {rate_val}%{vol_text} ({eff_date})",
                            "rate_type": rate_type, "rate": rate_val, "volume": volume,
                            "extra_json": json.dumps({"type": rate_type, "rate": rate_val, "date": eff_date}),
                        })

                # RRP facility usage
                async with session.get(self.RRP_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        rrp_data = await resp.json()
                        ops = rrp_data.get("repo", {}).get("operations", [])
                        if ops:
                            latest = ops[0]
                            total = latest.get("totalAmtAccepted", 0)
                            date = latest.get("operationDate", "")
                            key = f"nyfed-rrp-{date}-{total}"
                            if not self._already_seen(key):
                                total_b = float(total) / 1e9 if total else 0
                                alert = " [LOW — collateral stress]" if total_b < 200 else ""
                                await self.emit({
                                    "text": f"NY Fed: RRP facility ${total_b:,.0f}B ({date}){alert}",
                                    "rrp_billions": total_b,
                                    "extra_json": json.dumps({"rrp": total_b, "date": date}),
                                })
        except Exception as e:
            log.warning("[NY Fed Rates] Error: %s", e)


# ═══════════════════════════════════════════════════════
# USASPENDING.GOV — federal contract awards
# No auth. DoD, NASA, DHS contracts with full details.
# ═══════════════════════════════════════════════════════

class UsaSpendingSource(BaseSource):
    """USASpending — federal contract awards (DoD, NASA, DHS). No auth."""
    name = "USASpending"
    interval_seconds = 1800.0

    API_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

    async def poll(self) -> None:
        try:
            now = datetime.datetime.utcnow()
            week_ago = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d")

            payload = {
                "filters": {
                    "time_period": [{"start_date": week_ago, "end_date": now.strftime("%Y-%m-%d")}],
                    "award_type_codes": ["A", "B", "C", "D"],
                    "award_amounts": [{"lower_bound": 10000000}],
                },
                "fields": ["Award ID", "Recipient Name", "Award Amount", "Description", "Awarding Agency", "Start Date"],
                "limit": 10,
                "order": "desc",
                "sort": "Award Amount",
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(self.API_URL, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            results = data.get("results", [])
            for award in results:
                aid = award.get("Award ID", "")
                if not aid or self._already_seen(aid):
                    continue

                recipient = award.get("Recipient Name", "")
                amount = award.get("Award Amount", 0)
                agency = award.get("Awarding Agency", "")
                desc = award.get("Description", "")[:100]

                try:
                    amt_m = float(amount) / 1e6
                except (ValueError, TypeError):
                    amt_m = 0

                await self.emit({
                    "text": f"USASpending: ${amt_m:,.0f}M to {recipient} ({agency}) — {desc}",
                    "recipient": recipient, "amount": amount, "agency": agency,
                    "extra_json": json.dumps({"id": aid, "recipient": recipient, "amount": amount, "agency": agency}),
                })
        except Exception as e:
            log.warning("[USASpending] Error: %s", e)


# ═══════════════════════════════════════════════════════
# IAEA RSS — nuclear geopolitical intelligence
# No auth. Iran enrichment, reactor incidents, DG statements.
# ═══════════════════════════════════════════════════════

class IaeaSource(BaseSource):
    """IAEA — nuclear weapons inspections, reactor incidents, DG statements. No auth."""
    name = "IAEA"
    interval_seconds = 300.0

    FEEDS = {
        "dg_statements": "https://www.iaea.org/feeds/dgstatements",
        "top_news": "https://www.iaea.org/feeds/topnews",
    }

    async def poll(self) -> None:
        loop = asyncio.get_event_loop()
        for feed_name, url in self.FEEDS.items():
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, url)
                for entry in feed.entries[:10]:
                    if not self._is_fresh_rss(entry):
                        continue
                    uid = entry.get("id") or entry.get("link", "")
                    if self._already_seen(uid):
                        continue
                    title = entry.get("title", "")
                    priority = "URGENT" if feed_name == "dg_statements" else "normal"
                    await self.emit({
                        "text": f"IAEA [{priority}]: {title}",
                        "title": title, "priority": priority, "feed": feed_name,
                        "link": entry.get("link", ""),
                        "extra_json": json.dumps({"uid": uid, "feed": feed_name, "priority": priority}),
                    })
            except Exception as e:
                log.warning("[IAEA] %s error: %s", feed_name, e)


# ═══════════════════════════════════════════════════════
# CDC WASTEWATER SURVEILLANCE (NWSS)
# No auth (Socrata). Detects outbreaks 1-2 weeks early.
# ═══════════════════════════════════════════════════════

class CdcWastewaterSource(BaseSource):
    """CDC NWSS wastewater surveillance — early outbreak detection. No auth."""
    name = "CDC Wastewater"
    interval_seconds = 3600.0  # weekly data, hourly poll

    API_URL = "https://data.cdc.gov/resource/2ew6-ywp6.json"

    async def poll(self) -> None:
        try:
            params = {"$limit": "20", "$order": "date_end DESC"}
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            for record in (data if isinstance(data, list) else []):
                state = record.get("wwtp_jurisdiction", record.get("state", ""))
                date_end = record.get("date_end", "")
                activity = record.get("activity_level", record.get("ptc_15d", ""))
                trend = record.get("detect_prop_15d", record.get("trend", ""))
                pathogen = record.get("pathogen", "SARS-CoV-2")

                key = f"cdc-ww-{state}-{date_end}-{pathogen}"
                if not state or self._already_seen(key):
                    continue

                alert = ""
                if isinstance(trend, str) and "increasing" in trend.lower():
                    alert = " [INCREASING]"

                await self.emit({
                    "text": f"CDC Wastewater: {state} {pathogen} activity={activity} trend={trend}{alert} ({date_end})",
                    "state": state, "pathogen": pathogen, "activity": activity, "trend": trend,
                    "extra_json": json.dumps({"state": state, "pathogen": pathogen, "activity": activity, "date": date_end}),
                })
        except Exception as e:
            log.warning("[CDC Wastewater] Error: %s", e)


# ═══════════════════════════════════════════════════════
# NASA POWER — satellite-derived energy/agriculture data
# No auth. Solar irradiance, wind speed, temperature globally.
# ═══════════════════════════════════════════════════════

class NasaPowerSource(BaseSource):
    """NASA POWER — satellite solar/wind/temp data for energy prediction. No auth."""
    name = "NASA POWER"
    interval_seconds = 3600.0  # daily data

    API_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"

    # Key energy locations
    LOCATIONS = {
        "Texas (Permian)": {"lat": 31.9, "lon": -102.1},
        "California (Solar)": {"lat": 35.3, "lon": -118.5},
        "Gulf of Mexico": {"lat": 28.0, "lon": -90.0},
    }

    async def poll(self) -> None:
        try:
            now = datetime.datetime.utcnow()
            end = (now - datetime.timedelta(days=3)).strftime("%Y%m%d")  # 2-3 day lag
            start = (now - datetime.timedelta(days=5)).strftime("%Y%m%d")

            async with aiohttp.ClientSession() as session:
                for loc_name, coords in self.LOCATIONS.items():
                    params = {
                        "parameters": "ALLSKY_SFC_SW_DWN,T2M,WS10M",
                        "community": "RE",
                        "longitude": coords["lon"],
                        "latitude": coords["lat"],
                        "start": start,
                        "end": end,
                        "format": "JSON",
                    }
                    try:
                        async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    props = data.get("properties", {}).get("parameter", {})
                    solar = props.get("ALLSKY_SFC_SW_DWN", {})
                    temp = props.get("T2M", {})
                    wind = props.get("WS10M", {})

                    if not solar:
                        continue

                    # Get latest day
                    latest_date = sorted(solar.keys())[-1] if solar else ""
                    sol_val = solar.get(latest_date, -999)
                    tmp_val = temp.get(latest_date, -999)
                    wnd_val = wind.get(latest_date, -999)

                    if sol_val == -999:
                        continue

                    key = f"power-{loc_name}-{latest_date}"
                    if self._already_seen(key):
                        continue

                    await self.emit({
                        "text": f"NASA POWER: {loc_name} — solar {sol_val:.1f} kWh/m2, temp {tmp_val:.1f}C, wind {wnd_val:.1f} m/s ({latest_date})",
                        "location": loc_name, "solar": sol_val, "temp": tmp_val, "wind": wnd_val,
                        "extra_json": json.dumps({"loc": loc_name, "solar": sol_val, "temp": tmp_val, "wind": wnd_val, "date": latest_date}),
                    })
        except Exception as e:
            log.warning("[NASA POWER] Error: %s", e)


# ═══════════════════════════════════════════════════════
# FMCS F-7 LABOR NOTICES — 30-day strike warning
# No auth. Legally mandated bargaining expiration notices.
# ═══════════════════════════════════════════════════════

class FmcsLaborSource(BaseSource):
    """FMCS F-7 notices — 30-day advance strike warning system. No auth."""
    name = "FMCS Labor"
    interval_seconds = 3600.0  # monthly data

    # Pattern: the FMCS publishes monthly Excel files
    # We'll check the FMCS notices page for the latest
    FMCS_PAGE_URL = "https://www.fmcs.gov/resources/documents-and-data/"

    async def poll(self) -> None:
        try:
            # Poll the FMCS documents page for latest F-7 notice links
            async with aiohttp.ClientSession() as session:
                async with session.get(self.FMCS_PAGE_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    text = await resp.text()

            # Look for F-7 notice file links
            import re
            links = re.findall(r'href="([^"]*F.?7[^"]*\.xlsx?)"', text, re.IGNORECASE)
            if not links:
                # Try alternate pattern
                links = re.findall(r'href="([^"]*Notices[^"]*\.xlsx?)"', text, re.IGNORECASE)

            for link in links[:2]:
                key = f"fmcs-{link}"
                if self._already_seen(key):
                    continue

                # Extract month from filename
                month_match = re.search(r'(\w+-\d{4})', link)
                month = month_match.group(1) if month_match else "latest"

                await self.emit({
                    "text": f"FMCS: New F-7 bargaining notices published ({month})",
                    "file_url": link, "period": month,
                    "extra_json": json.dumps({"url": link, "period": month}),
                })

        except Exception as e:
            log.warning("[FMCS Labor] Error: %s", e)


# ═══════════════════════════════════════════════════════
# USDA NASS QUICKSTATS — crop conditions + production
# Free key. Weekly crop progress, condition ratings.
# ═══════════════════════════════════════════════════════

class UsdaNassSource(BaseSource):
    """USDA NASS — crop conditions, production, acreage. Free key."""
    name = "USDA NASS"
    interval_seconds = 1800.0

    API_URL = "https://quickstats.nass.usda.gov/api/api_GET/"

    QUERIES = [
        {"commodity_desc": "CORN", "statisticcat_desc": "CONDITION", "agg_level_desc": "NATIONAL"},
        {"commodity_desc": "SOYBEANS", "statisticcat_desc": "CONDITION", "agg_level_desc": "NATIONAL"},
        {"commodity_desc": "WHEAT", "statisticcat_desc": "CONDITION", "agg_level_desc": "NATIONAL"},
    ]

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("USDA_NASS_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return
        try:
            year = str(datetime.datetime.utcnow().year)
            async with aiohttp.ClientSession() as session:
                for q in self.QUERIES:
                    params = {**q, "key": self.api_key, "year": year, "format": "JSON"}
                    try:
                        async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    records = data.get("data", [])
                    if not records:
                        continue

                    # Get latest week
                    latest = records[0]
                    commodity = latest.get("commodity_desc", "")
                    value = latest.get("Value", latest.get("value", ""))
                    unit = latest.get("unit_desc", "")
                    week = latest.get("week_ending", latest.get("reference_period_desc", ""))
                    category = latest.get("domaincat_desc", latest.get("domain_desc", ""))

                    key = f"nass-{commodity}-{week}-{value}"
                    if self._already_seen(key):
                        continue

                    await self.emit({
                        "text": f"USDA NASS: {commodity} {category} = {value}{unit} (week ending {week})",
                        "commodity": commodity, "value": value, "week": week,
                        "extra_json": json.dumps({"commodity": commodity, "value": value, "week": week}),
                    })
        except Exception as e:
            log.warning("[USDA NASS] Error: %s", e)


# ═══════════════════════════════════════════════════════
# OPENFEC — campaign finance + Super PAC spending
# Free key. Real-time e-filing RSS (no auth) + structured API.
# ═══════════════════════════════════════════════════════

class OpenFecSource(BaseSource):
    """OpenFEC — campaign finance, Super PAC spending. Free key + RSS."""
    name = "OpenFEC"
    interval_seconds = 600.0

    RSS_URL = "https://efilingapps.fec.gov/rss/generate?preDefinedFilingType=ALL"
    API_URL = "https://api.open.fec.gov/v1/schedules/schedule_e/"

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("OPENFEC_API_KEY", "")

    async def poll(self) -> None:
        # RSS feed (no auth needed)
        try:
            loop = asyncio.get_event_loop()
            feed = await loop.run_in_executor(None, feedparser.parse, self.RSS_URL)
            for entry in feed.entries[:10]:
                if not self._is_fresh_rss(entry):
                    continue
                uid = entry.get("id") or entry.get("link", "")
                if self._already_seen(uid):
                    continue
                title = entry.get("title", "")
                await self.emit({
                    "text": f"FEC Filing: {title}",
                    "title": title, "link": entry.get("link", ""),
                    "extra_json": json.dumps({"uid": uid}),
                })
        except Exception as e:
            log.warning("[OpenFEC] RSS error: %s", e)

        # Schedule E (Super PAC independent expenditures) via API
        if not self.api_key:
            return
        try:
            params = {
                "api_key": self.api_key,
                "sort": "-expenditure_date",
                "per_page": "10",
                "min_amount": "100000",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

            for result in data.get("results", []):
                sub_id = result.get("sub_id", "")
                if not sub_id or self._already_seen(str(sub_id)):
                    continue
                committee = result.get("committee", {}).get("name", "")
                candidate = result.get("candidate_name", "")
                amount = result.get("expenditure_amount", 0)
                support = result.get("support_oppose_indicator", "")
                date = result.get("expenditure_date", "")

                action = "SUPPORTING" if support == "S" else "OPPOSING"
                await self.emit({
                    "text": f"FEC: {committee} spent ${amount:,.0f} {action} {candidate} ({date})",
                    "committee": committee, "candidate": candidate, "amount": amount, "action": action,
                    "extra_json": json.dumps({"committee": committee, "candidate": candidate, "amount": amount}),
                })
        except Exception as e:
            log.warning("[OpenFEC] API error: %s", e)


# ═══════════════════════════════════════════════════════
# THE ODDS API — sports/event odds as breaking news proxy
# Free key. 500 credits/month. Line movement = information signal.
# ═══════════════════════════════════════════════════════

class OddsApiSource(BaseSource):
    """The Odds API — sports odds line movement as news detection. Free key."""
    name = "Odds API"
    interval_seconds = 1800.0  # conserve 500 credits/month

    API_URL = "https://api.the-odds-api.com/v4/sports"

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("ODDS_API_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return
        try:
            # First get active sports (free, no credits)
            async with aiohttp.ClientSession() as session:
                params = {"apiKey": self.api_key}
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    sports = await resp.json()

                # Find active sports with upcoming events
                active = [s for s in (sports if isinstance(sports, list) else [])
                          if s.get("active") and not s.get("has_outrights")]

                if not active:
                    return

                # Get events for top sport (costs 1 credit per market*region)
                top_sport = active[0].get("key", "")
                events_url = f"{self.API_URL}/{top_sport}/events"
                async with session.get(events_url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return
                    events = await resp.json()

                for event in (events if isinstance(events, list) else [])[:5]:
                    eid = event.get("id", "")
                    if not eid or self._already_seen(eid):
                        continue
                    home = event.get("home_team", "")
                    away = event.get("away_team", "")
                    commence = event.get("commence_time", "")

                    await self.emit({
                        "text": f"Odds: {away} @ {home} ({top_sport}) — {commence}",
                        "sport": top_sport, "home": home, "away": away,
                        "extra_json": json.dumps({"id": eid, "sport": top_sport, "home": home, "away": away}),
                    })
        except Exception as e:
            log.warning("[Odds API] Error: %s", e)


# ═══════════════════════════════════════════════════════
# NASDAQ DATA LINK (CHRIS) — commodity futures settlements
# Free key. 600+ continuous futures contracts.
# ═══════════════════════════════════════════════════════

class NasdaqDataLinkSource(BaseSource):
    """Nasdaq Data Link CHRIS — daily commodity futures settlements. Free key."""
    name = "Nasdaq CHRIS"
    interval_seconds = 86400.0  # Blocked — daily poll to avoid wasted requests

    API_URL = "https://data.nasdaq.com/api/v3/datasets/CHRIS"

    # Key continuous contracts
    CONTRACTS = {
        "CME_CL1": "Crude Oil (WTI)",
        "CME_NG1": "Natural Gas",
        "CME_GC1": "Gold",
        "CME_SI1": "Silver",
        "CME_ES1": "E-mini S&P 500",
        "EUREX_FESX1": "Euro Stoxx 50",
    }

    def __init__(self, queue: asyncio.Queue, api_key: str = ""):
        super().__init__(queue)
        self.api_key = api_key or os.environ.get("NASDAQ_DATA_LINK_KEY", "")

    async def poll(self) -> None:
        if not self.api_key:
            return
        try:
            async with aiohttp.ClientSession() as session:
                for contract, label in self.CONTRACTS.items():
                    url = f"{self.API_URL}/{contract}.json"
                    params = {"api_key": self.api_key, "rows": "2"}
                    try:
                        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    dataset = data.get("dataset", {})
                    rows = dataset.get("data", [])
                    columns = dataset.get("column_names", [])

                    if not rows or not columns:
                        continue

                    latest = dict(zip(columns, rows[0]))
                    date = latest.get("Date", "")
                    settle = latest.get("Settle", latest.get("Last", ""))
                    volume = latest.get("Volume", "")
                    oi = latest.get("Previous Day Open Interest", latest.get("Open Interest", ""))

                    key = f"chris-{contract}-{date}-{settle}"
                    if self._already_seen(key):
                        continue

                    change = ""
                    if len(rows) > 1:
                        prev = dict(zip(columns, rows[1]))
                        prev_settle = prev.get("Settle", prev.get("Last", 0))
                        try:
                            delta = float(settle) - float(prev_settle)
                            change = f" ({delta:+.2f})"
                        except (ValueError, TypeError):
                            pass

                    await self.emit({
                        "text": f"CHRIS: {label} settle ${settle}{change} vol={volume} OI={oi} ({date})",
                        "contract": contract, "label": label, "settle": settle, "volume": volume,
                        "extra_json": json.dumps({"contract": contract, "settle": settle, "volume": volume, "date": date}),
                    })
        except Exception as e:
            log.warning("[Nasdaq CHRIS] Error: %s", e)


# ═══════════════════════════════════════════════════════
# REGISTRY — add new sources here
# main.py reads this list to start all source tasks
# ═══════════════════════════════════════════════════════

import os

def build_sources(queue: asyncio.Queue, config: dict) -> list[BaseSource]:
    """Instantiate all active sources."""
    return [
        # -- Live Price Data --
        KrakenPriceSource(queue),

        # -- Government / Macro Sources --
        EiaPetroleumSource(queue, api_key=config.get("EIA_API_KEY", "")),
        OpecRssSource(queue),
        FedRssSource(queue),
        EcbRssSource(queue),
        FederalRegisterSource(queue),
        FedRegPrePubSource(queue),
        SecPressReleaseSource(queue),
        BlsSource(queue, api_key=config.get("BLS_API_KEY", "")),
        TreasuryAuctionSource(queue),
        CftcCotSource(queue),
        FredSource(queue, api_key=config.get("FRED_API_KEY", "")),
        BeaSource(queue, api_key=config.get("BEA_API_KEY", "")),
        CensusEitsSource(queue, api_key=config.get("CENSUS_API_KEY", "")),
        WhiteHouseSource(queue),
        CongressSource(queue, api_key=config.get("CONGRESS_API_KEY", "")),

        # -- Central Banks --
        BoeSource(queue),

        # -- SEC / Regulatory --
        EdgarFilingSource(queue),
        SecForm4Source(queue),
        FdaMedwatchSource(queue),
        SecEnforcementSource(queue),
        FercEnergySource(queue),
        NrcReactorSource(queue),

        # -- Physical World / Weather --
        NoaaSpaceWeatherSource(queue),
        UsgsEarthquakeSource(queue),
        NwsSevereWeatherSource(queue),
        NhcTropicalSource(queue),
        NasaFirmsSource(queue, api_key=config.get("NASA_FIRMS_KEY", "")),

        # -- Infrastructure / Trade / Grid --
        FaaNasSource(queue),
        CbpBorderSource(queue),
        MisoGridSource(queue),
        ErcotSource(queue, api_key=config.get("ERCOT_API_KEY", "")),
        CaisoSource(queue),
        NoaaCoopsSource(queue),
        UsgsWaterSource(queue),

        # -- Global Event Detection --
        GdeltDocSource(queue),
        WhoOutbreakSource(queue),
        OfacSanctionsSource(queue),

        # -- Financial Markets / Sentiment --
        PolymarketSource(queue),
        FinraAtsSource(queue),
        GieAgsiSource(queue, api_key=config.get("GIE_API_KEY", "")),
        CboePcRatioSource(queue),
        WikiPageviewSource(queue),
        EurostatSource(queue),

        # -- Central Banks --
        BojSource(queue),
        SnbSource(queue),
        BocSource(queue),

        # -- Legal / Regulatory --
        DojAntitrustSource(queue),
        FtcCompetitionSource(queue),

        # -- Agriculture / Drought --
        UsdaLmprSource(queue),
        DroughtMonitorSource(queue),

        # -- News / Intelligence --
        BbcNewsSource(queue),
        AlJazeeraSource(queue),
        HackerNewsSource(queue),
        ReliefWebSource(queue),

        # -- Natural Disasters --
        NasaEonetSource(queue),
        NifcWildfireSource(queue),
        UsgsVolcanoSource(queue),
        CopernicusEmsSource(queue),

        # -- Oceanographic / Maritime --
        NoaaBuoySource(queue),

        # -- Space / Satellites --
        CelesTrakSource(queue),

        # -- Energy Grid (International) --
        UkCarbonSource(queue),

        # -- Maritime / Vessel Tracking --
        AisStreamSource(queue, api_key=config.get("AISSTREAM_API_KEY", "")),

        # -- Air Quality --
        AirNowSource(queue, api_key=config.get("AIRNOW_API_KEY", "")),

        # -- Conflict Intelligence --
        AcledSource(queue, email=config.get("ACLED_EMAIL", ""), password=config.get("ACLED_PASSWORD", "")),

        # -- Sentiment / Fear & Greed --
        CnnFearGreedSource(queue),
        CryptoFearGreedSource(queue),
        SentiCryptSource(queue),
        StockTwitsSource(queue),
        FourChanBizSource(queue),

        # -- Prediction Markets --
        KalshiSource(queue),
        MetaculusSource(queue),
        PredictItSource(queue),
        ManifoldSource(queue),

        # -- News Sentiment --
        FinnhubSource(queue, api_key=config.get("FINNHUB_API_KEY", "")),
        NewsApiAiSource(queue, api_key=config.get("NEWSAPI_AI_KEY", "")),
        MarketAuxSource(queue, api_key=config.get("MARKETAUX_API_KEY", "")),
        CurrentsApiSource(queue, api_key=config.get("CURRENTS_API_KEY", "")),

        # -- Money Markets / Rates --
        NyFedRatesSource(queue),

        # -- Government Procurement --
        UsaSpendingSource(queue),

        # -- Nuclear / Geopolitical --
        IaeaSource(queue),

        # -- Health Surveillance --
        CdcWastewaterSource(queue),

        # -- Satellite Data --
        NasaPowerSource(queue),

        # -- Labor --
        FmcsLaborSource(queue),

        # -- Agriculture (crop conditions) --
        UsdaNassSource(queue, api_key=config.get("USDA_NASS_KEY", "")),

        # -- Political Finance --
        OpenFecSource(queue, api_key=config.get("OPENFEC_API_KEY", "")),

        # -- Sports / Event Odds --
        OddsApiSource(queue, api_key=config.get("ODDS_API_KEY", "")),

        # -- Commodity Futures Settlements --
        NasdaqDataLinkSource(queue, api_key=config.get("NASDAQ_DATA_LINK_KEY", "")),

        # -- Aircraft Tracking --
        OpenSkySource(queue, client_id=config.get("OPENSKY_CLIENT_ID", ""), client_secret=config.get("OPENSKY_CLIENT_SECRET", "")),

        # -- Real-Time Exchange Data --
        KrakenFundingRateSource(queue),
        OkxFundingRateSource(queue),
        BlockchainComSource(queue),

        # -- Crypto / DeFi --
        DydxSource(queue),
        MempoolSource(queue),
        HyperliquidSource(queue),
        DefiLlamaSource(queue),
        EiaNatGasSource(queue, api_key=config.get("EIA_API_KEY", "")),
        EtherscanSource(queue, api_key=config.get("ETHERSCAN_API_KEY", "")),
        CoinGeckoSource(queue),

        # -- Optional: Requires API Keys --
        # WhaleAlertSource(queue, api_key=config.get("WHALE_ALERT_KEY", "")),
        # CoinglassSource(queue, api_key=config.get("COINGLASS_KEY", "")),
    ]
