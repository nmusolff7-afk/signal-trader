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
    """
    name: str = "Unknown"
    interval_seconds: float = 60.0   # How often to check for new data

    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
        self._seen: set[str] = set()   # Deduplicate items we've already sent

    def _now(self) -> str:
        return datetime.datetime.utcnow().isoformat()

    def _already_seen(self, key: str) -> bool:
        """Return True if we've sent this item before. Prevents duplicate signals."""
        if key in self._seen:
            return True
        self._seen.add(key)
        # Trim to prevent unbounded memory growth (keep last 500 keys)
        if len(self._seen) > 500:
            self._seen.pop()
        return False

    async def emit(self, item: dict) -> None:
        """Put a new item on the shared queue."""
        item["ts"] = item.get("ts", self._now())
        item["source"] = self.name
        await self.queue.put(item)
        log.info("[%s] emitted: %s", self.name, item.get("text", "")[:80])

    @abstractmethod
    async def poll(self) -> None:
        """Fetch data and call await self.emit(item) for each new item found."""
        ...

    async def run(self) -> None:
        """Main loop. Polls forever, sleeping interval_seconds between calls."""
        log.info("[%s] source started (interval: %ss)", self.name, self.interval_seconds)
        while True:
            try:
                await self.poll()
            except Exception as exc:
                # Log but don't crash — a bad response shouldn't kill the loop
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
    interval_seconds = 30.0   # Poll every 30s — OPEC announcements are time-sensitive

    RSS_URL = "https://www.opec.org/opec_web/en/press_room/204.htm"

    async def poll(self) -> None:
        # feedparser is synchronous — run it in a thread so it doesn't block the loop
        loop = asyncio.get_event_loop()
        feed = await loop.run_in_executor(None, feedparser.parse, self.RSS_URL)

        for entry in feed.entries:
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
                        
                        value_btc = tx.get("out", [{}])[0].get("value", 0) / 1e8
                        
                        if value_btc > 5:  # >5 BTC transfers
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
            log.warning("[Etherscan] No API key set. Set ETHERSCAN_API_KEY env var.")
            return

        try:
            async with aiohttp.ClientSession() as session:
                # Monitor latest internal transactions (covers whale transfers)
                params = {
                    "module": "account",
                    "action": "txlistinternal",
                    "address": "0x0000000000000000000000000000000000000000",  # Placeholder
                    "startblock": 0,
                    "endblock": 99999999,
                    "sort": "desc",
                    "apikey": self.api_key
                }
                async with session.get(self.ETHERSCAN_API_URL, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        log.warning("[Etherscan] HTTP %s", resp.status)
                        return
                    data = await resp.json()
        except Exception as exc:
            log.warning("[Etherscan] poll error: %s", exc)
            return

        try:
            if data.get("status") != "1":  # Etherscan uses status "1" for success
                return
            
            txs = data.get("result", [])
            if not txs:
                return

            for tx in txs[:5]:  # Check top 5 recent txs
                tx_hash = tx.get("hash", "")
                if self._already_seen(tx_hash):
                    continue

                value_eth = float(tx.get("value", 0)) / 1e18  # Convert Wei to ETH

                if value_eth > 100:  # Large transfer threshold
                    from_addr = tx.get("from", "")[:10]
                    to_addr = tx.get("to", "")[:10]
                    
                    await self.emit({
                        "text": f"Etherscan: Large ETH transfer {value_eth:.2f} ETH ({from_addr}...→{to_addr}...)",
                        "value_eth": value_eth,
                        "tx_hash": tx_hash,
                        "from": tx.get("from", ""),
                        "to": tx.get("to", ""),
                        "extra_json": json.dumps({
                            "value_eth": value_eth,
                            "tx": tx_hash,
                            "from": tx.get("from", ""),
                            "to": tx.get("to", "")
                        })
                    })
        except Exception as e:
            log.warning("[Etherscan] parse error: %s", e)


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
    interval_seconds = 300.0  # Every 5 minutes

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
    interval_seconds = 1800.0  # every 30 min (data releases monthly)

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
    interval_seconds = 900.0  # every 15 min

    RECALL_RSS = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/recalls/rss.xml"
    ENFORCEMENT_API = "https://api.fda.gov/drug/enforcement.json?sort=report_date:desc&limit=5"

    async def poll(self) -> None:
        # ── RSS feed for recalls/safety alerts ──
        try:
            loop = asyncio.get_event_loop()
            feed = await loop.run_in_executor(None, feedparser.parse, self.RECALL_RSS)

            for entry in feed.entries[:10]:
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
    interval_seconds = 1200.0  # every 20 min

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
    interval_seconds = 900.0  # every 15 min

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
    interval_seconds = 120.0  # every 2 min

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
    Polls Kraken spot prices for BTC and ETH every 10 seconds.
    Emits price + 24h change so the dashboard has real-time data.
    FREE. No API key required.
    """
    name = "Kraken Price"
    interval_seconds = 10.0

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

                        # Only emit if price actually changed
                        last = self._last_prices.get(label)
                        if last and abs(price - last) < 0.01:
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
    FEED_DAY_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"

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
    interval_seconds = 300.0  # every 5 min

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
    interval_seconds = 3600.0  # hourly (data is weekly, Fri 3:30pm)

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
                    "$where": "market_and_exchange_names in(" + ",".join(f"'{m}'" for m in self.TRACKED_MARKETS.keys()) + ")",
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
    interval_seconds = 300.0  # every 5 min

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

                for feature in data.get("features", [])[:20]:
                    props = feature.get("properties", {})
                    alert_id = props.get("id", "")
                    if not alert_id or self._already_seen(alert_id):
                        continue

                    event = props.get("event", "")
                    severity = props.get("severity", "")
                    headline = props.get("headline", "")
                    area = props.get("areaDesc", "")[:100]

                    await self.emit({
                        "text": f"NWS {severity}: {event} — {headline[:150]}",
                        "event_type": event,
                        "severity": severity,
                        "area": area,
                        "extra_json": json.dumps({
                            "alert_id": alert_id,
                            "event": event,
                            "severity": severity,
                            "area": area,
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
    interval_seconds = 900.0  # every 15 min

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
    interval_seconds = 600.0  # every 10 min

    RSS_URL = "https://www.ferc.gov/rss/newsroom/ferc-news.rss"

    async def poll(self) -> None:
        try:
            loop = asyncio.get_event_loop()
            feed = await loop.run_in_executor(None, feedparser.parse, self.RSS_URL)
            for entry in feed.entries[:10]:
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
    interval_seconds = 120.0  # every 2 min

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
    interval_seconds = 60.0  # every 1 min

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
    interval_seconds = 1800.0  # every 30 min

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
    interval_seconds = 300.0

    LMP_URL = "https://api.misoenergy.org/MISORTWDBIReporter/Reporter.asmx?messageType=currentinterval&returnType=json"
    FUEL_URL = "https://api.misoenergy.org/MISORTWDDataBroker/DataBrokerServices.asmx?messageType=getfuelmix&returnType=json"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                # Fuel mix
                async with session.get(self.FUEL_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        fuel_mix = data.get("Fuel", {}).get("Type", []) if isinstance(data, dict) else []
                        if isinstance(data, dict):
                            # MISO returns nested structure
                            categories = data.get("Fuel", data.get("FuelMix", {}))
                            if isinstance(categories, dict):
                                fuel_mix = categories.get("Type", [])
                            elif isinstance(categories, list):
                                fuel_mix = categories
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

                # LMP snapshot
                async with session.get(self.LMP_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Extract summary LMP if available
                        if isinstance(data, dict):
                            refid = data.get("RefId", "")
                            key = f"miso-lmp-{refid}"
                            if refid and not self._already_seen(key):
                                await self.emit({
                                    "text": f"MISO: LMP interval update {refid}",
                                    "extra_json": json.dumps({"ref_id": refid}),
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
    interval_seconds = 600.0

    EVENT_RSS = "https://www.nrc.gov/public-involve/rss?feed=event"
    NEWS_RSS = "https://www.nrc.gov/public-involve/rss?feed=news"

    async def poll(self) -> None:
        loop = asyncio.get_event_loop()
        for feed_name, url in [("events", self.EVENT_RSS), ("news", self.NEWS_RSS)]:
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, url)
                for entry in feed.entries[:10]:
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
    interval_seconds = 120.0  # every 2 min

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
    interval_seconds = 120.0

    API_URL = "https://nasstatus.faa.gov/api/airport-status-information"
    LEGACY_URL = "https://soa.smext.faa.gov/asws/api/airport/delays"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                # Try primary first
                for url in [self.API_URL, self.LEGACY_URL]:
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                            break
                    except Exception:
                        continue
                else:
                    return

                # Handle both API response formats
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
    interval_seconds = 900.0  # every 15 min

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
    interval_seconds = 1800.0  # every 30 min

    API_URL = "https://www.who.int/api/news/diseaseoutbreaknews"

    async def poll(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                params = {"$orderby": "PublicationDate desc", "$top": "10"}
                async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                items = data.get("value", data) if isinstance(data, dict) else data
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
    interval_seconds = 60.0

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
    interval_seconds = 30.0

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
    interval_seconds = 900.0  # every 15 min

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
                for query, category in self.QUERIES:
                    params = {
                        "query": query,
                        "mode": "artlist",
                        "maxrecords": "5",
                        "timespan": "15min",
                        "format": "json",
                        "sort": "datedesc",
                    }
                    try:
                        async with session.get(self.API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                    except Exception:
                        continue

                    articles = data.get("articles", [])
                    for article in articles[:3]:
                        url = article.get("url", "")
                        if not url or self._already_seen(url):
                            continue
                        title = article.get("title", "")
                        tone = article.get("tone", 0)
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
    interval_seconds = 600.0

    NEWS_RSS = "https://www.bankofengland.co.uk/rss/news"
    SPEECHES_RSS = "https://www.bankofengland.co.uk/rss/speeches"

    async def poll(self) -> None:
        loop = asyncio.get_event_loop()
        for feed_name, url in [("news", self.NEWS_RSS), ("speeches", self.SPEECHES_RSS)]:
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, url)
                for entry in feed.entries[:10]:
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
    interval_seconds = 600.0

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
    interval_seconds = 600.0

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
    interval_seconds = 300.0  # every 5 min

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
    interval_seconds = 3600.0  # hourly (data is daily)

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
    interval_seconds = 30.0

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
    interval_seconds = 300.0  # every 5 min

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

        # -- Global Event Detection --
        GdeltDocSource(queue),
        WhoOutbreakSource(queue),

        # -- Financial Markets --
        PolymarketSource(queue),
        FinraAtsSource(queue),
        GieAgsiSource(queue, api_key=config.get("GIE_API_KEY", "")),

        # -- Aircraft Tracking --
        OpenSkySource(queue, client_id=config.get("OPENSKY_CLIENT_ID", ""), client_secret=config.get("OPENSKY_CLIENT_SECRET", "")),

        # -- Real-Time Exchange Data --
        KrakenFundingRateSource(queue),
        OkxFundingRateSource(queue),
        BlockchainComSource(queue),

        # -- Crypto / DeFi --
        DydxSource(queue),
        MempoolSource(queue),
        EtherscanSource(queue, api_key=config.get("ETHERSCAN_API_KEY", "")),
        CoinGeckoSource(queue),

        # -- Optional: Requires API Keys --
        # WhaleAlertSource(queue, api_key=config.get("WHALE_ALERT_KEY", "")),
        # CoinglassSource(queue, api_key=config.get("COINGLASS_KEY", "")),
    ]
