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
# BLS FEED (CPI, NFP, PCE)
# Feed: BLS press releases (no official RSS, use web scrape or email)
# Scheduled: Monthly (CPI 2nd Thurs), Monthly (NFP 1st Fri), Monthly (PCE)
# Classifier: keyword path → E032–E036
# ═══════════════════════════════════════════════════════

class BlsSource(BaseSource):
    """
    Fetches BLS major releases: CPI, NFP, PCE.
    Uses web scraping since BLS doesn't have a public RSS (yet).
    
    Scheduled releases:
      - CPI (all urban consumers): 2nd Thursday of month, 8:30 AM ET
      - Employment Situation (NFP): 1st Friday of month, 8:30 AM ET
      - Personal Income & Outlays (PCE): varies, ~last business day of month
    
    This implementation checks the BLS press release page for recent postings.
    FREE. No API key required for press releases.
    """
    name = "BLS Feed"
    interval_seconds = 300.0  # Poll every 5 minutes during scheduled windows
    
    # BLS press releases for major economic indicators
    BLS_PRESS_URL = "https://www.bls.gov/news.release/"
    
    async def poll(self) -> None:
        """
        LIMITATION: BLS doesn't publish machine-readable RSS for press releases.
        This is a placeholder that would need either:
          1. Web scraping (BeautifulSoup) to parse press release HTML
          2. Email subscription to BLS list + IMAP polling
          3. Economic calendar API integration (see notes below)
        
        For now, we recommend using an economic calendar API (PHASE 2.5).
        """
        log.info("[BLS] Placeholder: requires web scraping or email polling. See notes.")
        # TODO: Implement one of:
        #   - Selenium/BeautifulSoup web scrape of https://www.bls.gov/news.release/
        #   - IMAP polling of BLS press release email subscription
        #   - Integration with free economic calendar API (e.g., economicscalendar.com)


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
# FDA MedWatch (Drug Approvals & Rejections)
# API: FDA OpenData / MedWatch API
# Scheduled: Event-driven (varies by drug)
# Classifier: keyword path → E057–E060
# ═══════════════════════════════════════════════════════

class FdaMedWatchSource(BaseSource):
    """
    Polls FDA MedWatch for drug approvals, rejections, CRLs, black box warnings.
    
    FDA OpenData is free but limited. For real-time monitoring, you'd need:
      - SEC EDGAR 8-K filings from biotech companies (more reliable)
      - Paid service like Trecora/Refinitiv
    
    This implementation uses FDA's public API endpoints.
    
    FREE. No API key required.
    """
    name = "FDA MedWatch"
    interval_seconds = 300.0  # Poll every 5 minutes
    
    # FDA Drugs@FDA API for approvals
    FDA_DRUGS_API = "https://api.fda.gov/drug/event.json"
    
    async def poll(self) -> None:
        """
        FDA API has rate limits (~1000 req/hour) and doesn't expose approvals well.
        Better approach: Monitor SEC EDGAR 8-K filings for biotech companies mentioning FDA.
        
        This is a placeholder showing the API structure.
        """
        log.info("[FDA] Placeholder: FDA API limited. Recommend EDGAR 8-K monitoring instead.")
        # TODO: Would implement via:
        #   - EDGAR 8-K monitoring for biotech (see EdgarSource below)
        #   - Or paid FDA approval database integration


# ═══════════════════════════════════════════════════════
# EDGAR 8-K FILINGS (Insider Activity, M&A, Guidance, Resignations)
# API: SEC EDGAR REST API
# Scheduled: Real-time (filed during trading hours)
# Classifier: keyword path → E048–E053
# ═══════════════════════════════════════════════════════

class EdgarSource(BaseSource):
    """
    Monitors SEC EDGAR for real-time 8-K filings.
    Detects: insider sells, buybacks, CEO resignations, SEC subpoenas, M&A, guidance cuts.
    
    SEC EDGAR API is free and public.
    For real-time 8-K feeds, you could also use:
      - Xignite EDGAR API (premium, real-time)
      - Intrinio (premium, real-time)
    
    This implementation polls the SEC EDGAR API for recent 8-Ks.
    """
    name = "EDGAR 8-K"
    interval_seconds = 30.0  # Poll every 30 seconds (8-Ks are time-sensitive)
    
    EDGAR_API = "https://www.sec.gov/cgi-bin/browse-edgar"
    
    async def poll(self) -> None:
        """
        Query recent 8-K filings across all companies.
        This requires either:
          1. Bulk download of EDGAR indices (not real-time)
          2. CIK-by-CIK polling (slow, 10K+ companies)
          3. Third-party API (Xignite, Intrinio, etc.)
        
        For Phase 2, recommend starting with a paid service or
        implementing a queue of high-volume tickers.
        """
        log.info("[EDGAR] Placeholder: Real-time 8-K monitoring requires third-party API or bulk indexing.")
        # TODO: Implement via Xignite or Intrinio, or
        #       batch-process EDGAR daily archives for previous day's 8-Ks


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
# REGISTRY — add new sources here
# main.py reads this list to start all source tasks
# ═══════════════════════════════════════════════════════

import os

def build_sources(queue: asyncio.Queue, config: dict) -> list[BaseSource]:
    """
    Instantiate all active sources. Pass config dict with API keys.
    To disable a source temporarily, comment it out here.
    
    Config keys:
      - EIA_API_KEY (required for EIA)
      - WHALE_ALERT_KEY (optional, free tier available)
      - COINGLASS_KEY (optional, free tier available)
    
    All other sources use free RSS feeds or public APIs.
    """
    return [
        # ── Existing sources (Phase 0) ──
        EiaPetroleumSource(queue, api_key=config.get("EIA_API_KEY", "")),
        OpecRssSource(queue),
        FedRssSource(queue),
        EcbRssSource(queue),
        
        # ── Priority 1: RSS & Public APIs ──
        FederalRegisterSource(queue),
        SecPressReleaseSource(queue),
        
        # ── Priority 2: Requires API Keys ──
        # Uncomment and provide API keys:
        # FdaMedWatchSource(queue),
        # EdgarSource(queue),
        WhaleAlertSource(queue, api_key=config.get("WHALE_ALERT_KEY", "")),
        CoinglassSource(queue, api_key=config.get("COINGLASS_KEY", "")),
        
        # ── Placeholder sources (TODO): ──
        # BlsSource(queue),  # Needs web scraping or email polling
        # EdgarSource(queue),  # Needs third-party API or bulk indexing
        # FdaMedWatchSource(queue),  # Needs third-party integration
    ]
