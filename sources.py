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
# REGISTRY — add new sources here
# main.py reads this list to start all source tasks
# ═══════════════════════════════════════════════════════

import os

def build_sources(queue: asyncio.Queue, config: dict) -> list[BaseSource]:
    """
    Instantiate all active sources. Pass config dict with API keys.

    Config keys:
      - EIA_API_KEY (required for EIA)
      - BLS_API_KEY (optional, increases rate limit 25→500/day)
      - WHALE_ALERT_KEY (optional, free tier available)
      - COINGLASS_KEY (optional, free tier available)
      - ETHERSCAN_API_KEY (optional)
    """
    return [
        # ── Government / Macro Sources (all free, no key) ──
        EiaPetroleumSource(queue, api_key=config.get("EIA_API_KEY", "")),
        OpecRssSource(queue),
        FedRssSource(queue),
        EcbRssSource(queue),
        FederalRegisterSource(queue),
        SecPressReleaseSource(queue),
        BlsSource(queue, api_key=config.get("BLS_API_KEY", "")),

        # ── Regulatory / Safety Sources (all free RSS, no key) ──
        FdaMedwatchSource(queue),
        SecEnforcementSource(queue),
        NoaaSpaceWeatherSource(queue),

        # ── Real-Time Exchange Data ──
        KrakenFundingRateSource(queue),
        OkxFundingRateSource(queue),
        BlockchainComSource(queue),

        # ── Blockchain Data ──
        EtherscanSource(queue, api_key=config.get("ETHERSCAN_API_KEY", "")),
        CoinGeckoSource(queue),

        # ── Optional: Requires API Keys ──
        # WhaleAlertSource(queue, api_key=config.get("WHALE_ALERT_KEY", "")),
        # CoinglassSource(queue, api_key=config.get("COINGLASS_KEY", "")),
    ]
