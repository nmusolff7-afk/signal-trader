"""
APEX Classifier — Phase 4: Classifier Model Build
==================================================
Hybrid two-path architecture:

  Fast path  (structured sources)  : keyword/regex matching, <1ms
  Slow path  (unstructured sources) : DistilBERT fine-tuned, ~30ms CPU

Architecture decision rationale (from Primary Source Signal Trading research doc):
  - FinMTEB benchmark: BoW/keyword models outperform ALL dense embedding
    architectures on structured regulatory filings (Spearman 0.4845 vs 0.4342).
    Extensive boilerplate in filings introduces noise for contextual embeddings.
  - DistilBERT: 40% smaller, 60% faster than BERT, retains ~97% understanding,
    achieves 93.2% accuracy on SEntFiN — viable for real-time unstructured text.
  - FinBERT achieves ~80% accuracy with 250 labeled training examples.
    100 examples is below minimum viable threshold for all models.
  - Optimal design: keyword matching for structured sources (EIA, Fed, EDGAR, FDA,
    Whale Alert), transformer for free-form text (GDELT, Reddit, PACER, FCC).

Covers all 20 events defined in EVENT TAXONOMY tab (E001-E020).
"""

import re
import time
import logging
import sqlite3
import os
from dataclasses import dataclass, field
from typing import Optional
from collections import Counter

try:
    import torch
    import numpy as np
    from torch.utils.data import Dataset
    from transformers import (
        DistilBertForSequenceClassification,
        DistilBertTokenizerFast,
        AutoTokenizer,
        AutoModelForSequenceClassification,
        TrainingArguments,
        Trainer,
    )
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


# ═══════════════════════════════════════════════════════
# SECTION 1: TAXONOMY
# Mirrors EVENT TAXONOMY tab exactly. Single source of truth.
# ═══════════════════════════════════════════════════════

@dataclass
class EventArchetype:
    event_id: str
    source: str
    description: str
    asset: str
    direction: str          # LONG | SHORT | VARIES
    magnitude: int          # 1-5
    hold_time: str
    confidence_threshold: float
    notes: str = ""

TAXONOMY: list[EventArchetype] = [
    # ── EIA ──────────────────────────────────────────────────────────────────
    EventArchetype("E001", "EIA Petroleum",    "Inventory draw significantly larger than consensus (bullish oil)",         "MCL",          "LONG",   4, "10 min",  0.80, "Most reliable scheduled event. Hardcode local parser."),
    EventArchetype("E002", "EIA Petroleum",    "Inventory build significantly larger than consensus (bearish oil)",        "MCL",          "SHORT",  4, "10 min",  0.80, "Symmetric to E001."),
    EventArchetype("E021", "EIA Petroleum",    "Crude inventory draw: small (within 0.5M bbl of consensus)",              "MCL",          "LONG",   2, "5 min",   0.65, "Smaller signal. Short fade only."),
    EventArchetype("E022", "EIA Natural Gas",  "Natural gas storage draw significantly above consensus",                  "MCL",          "LONG",   3, "15 min",  0.75, "Linked to crude via energy complex."),
    EventArchetype("E023", "EIA Natural Gas",  "Natural gas storage build significantly above consensus",                 "MCL",          "SHORT",  2, "15 min",  0.72, "Bearish natgas, mild MCL correlation."),
    # ── Fed / ECB ─────────────────────────────────────────────────────────────
    EventArchetype("E003", "Fed RSS",          "Surprise rate cut or strongly dovish language",                            "MES, M6E, MGC, BTC", "LONG",  5, "15 min",  0.85, "Highest magnitude. Local keyword parser for speed."),
    EventArchetype("E004", "Fed RSS",          "Surprise rate hike or strongly hawkish language",                         "MES, M6E, MGC","SHORT",  5, "15 min",  0.85, "Symmetric to E003."),
    EventArchetype("E024", "Fed RSS",          "Fed announces restart of QE / asset purchases",                           "MES, MGC, BTC","LONG",   5, "1 day",   0.88),
    EventArchetype("E025", "Fed RSS",          "Fed accelerates quantitative tightening (QT) beyond schedule",            "MES, MGC",     "SHORT",  4, "1 day",   0.85),
    EventArchetype("E026", "Fed RSS",          "Fed calls unscheduled emergency meeting (no rate decision yet)",          "MES, MGC",     "VARIES", 3, "2 hr",    0.78),
    EventArchetype("E027", "Fed RSS",          "FOMC minutes released — net dovish shift vs prior statement",             "MES, MGC",     "LONG",   3, "30 min",  0.80),
    EventArchetype("E028", "Fed RSS",          "FOMC minutes released — net hawkish shift vs prior statement",            "MES",          "SHORT",  3, "30 min",  0.80),
    EventArchetype("E029", "ECB RSS",          "ECB surprise rate cut or stronger-than-expected dovish language",         "M6E, MES",     "LONG",   4, "20 min",  0.83),
    EventArchetype("E030", "ECB RSS",          "ECB surprise rate hike or stronger-than-expected hawkish language",       "M6E, MES",     "SHORT",  4, "20 min",  0.83),
    EventArchetype("E031", "ECB RSS",          "ECB emergency liquidity announcement (banking stress event)",             "M6E, MES, MGC","VARIES", 5, "4 hr",    0.80),
    # ── BLS ───────────────────────────────────────────────────────────────────
    EventArchetype("E032", "BLS Feed",         "CPI print significantly below consensus — lower than expected inflation", "MES, MGC, BTC","LONG",   4, "30 min",  0.85),
    EventArchetype("E033", "BLS Feed",         "CPI print significantly above consensus — hotter than expected inflation","MES, MGC",     "SHORT",  4, "30 min",  0.85),
    EventArchetype("E034", "BLS Feed",         "NFP significantly above consensus — strong jobs market",                  "MES",          "LONG",   3, "30 min",  0.82),
    EventArchetype("E035", "BLS Feed",         "NFP significantly below consensus — weak jobs market",                    "MES",          "SHORT",  3, "30 min",  0.82),
    EventArchetype("E036", "BLS Feed",         "PCE inflation print significantly above or below consensus",              "MES, MGC",     "VARIES", 3, "20 min",  0.80),
    # ── OPEC ──────────────────────────────────────────────────────────────────
    EventArchetype("E037", "OPEC RSS",         "OPEC+ announces surprise production cut >500k bpd",                       "MCL",          "LONG",   5, "4 hr",    0.90),
    EventArchetype("E038", "OPEC RSS",         "OPEC+ announces surprise production increase",                            "MCL",          "SHORT",  4, "4 hr",    0.87),
    EventArchetype("E039", "OPEC RSS",         "OPEC+ calls emergency/unscheduled meeting",                               "MCL",          "LONG",   3, "2 hr",    0.75),
    EventArchetype("E040", "OPEC RSS",         "OPEC+ member threatens to leave cartel or openly exceed quota",           "MCL",          "LONG",   2, "1 hr",    0.68),
    # ── PHMSA ─────────────────────────────────────────────────────────────────
    EventArchetype("E005", "PHMSA",            "Major pipeline rupture or explosion affecting supply",                    "MCL",          "LONG",   3, "2 hr",    0.75, "Rare but high impact."),
    EventArchetype("E041", "PHMSA Pipeline",   "PHMSA issues emergency order shutting down major pipeline segment",       "MCL",          "LONG",   4, "3 hr",    0.82),
    EventArchetype("E042", "PHMSA Pipeline",   "PHMSA major natural gas pipeline rupture or explosion",                   "MCL",          "LONG",   3, "2 hr",    0.75),
    # ── Coast Guard / AIS ────────────────────────────────────────────────────
    EventArchetype("E006", "Coast Guard/AIS",  "Strait of Hormuz or Suez Canal disruption, closure, or incident",        "MCL",          "LONG",   5, "4 hr",    0.82, "Major geopolitical supply shock."),
    EventArchetype("E043", "Coast Guard/AIS",  "Multiple AIS signals lost simultaneously in Strait of Hormuz",           "MCL",          "LONG",   4, "4 hr",    0.80),
    EventArchetype("E044", "Coast Guard/AIS",  "Coast Guard emergency broadcast: oil tanker fire, collision, or sinking", "MCL",         "LONG",   3, "3 hr",    0.78),
    EventArchetype("E045", "Coast Guard/AIS",  "AIS shows large tanker fleet rerouting away from Red Sea / Suez",        "MCL",          "LONG",   4, "6 hr",    0.77),
    # ── FAA NOTAM ────────────────────────────────────────────────────────────
    EventArchetype("E015", "FAA NOTAM",        "Emergency military airspace activation in unexpected region",             "MCL, MGC",     "LONG",   2, "1 hr",    0.58, "Weak signal. Requires confirmation."),
    EventArchetype("E046", "FAA NOTAM",        "FAA grounds all flights at major hub (weather or security)",             "Airlines",     "SHORT",  3, "2 hr",    0.72),
    EventArchetype("E047", "FAA NOTAM",        "FAA closes airspace over Middle East or oil-producing region",           "MCL, MGC",     "LONG",   2, "1 hr",    0.62),
    # ── EDGAR 8-K ────────────────────────────────────────────────────────────
    EventArchetype("E007", "EDGAR 8-K",        "Surprise M&A announcement for small/mid cap company",                    "That stock",   "LONG",   4, "Same day", 0.78, "Must extract ticker."),
    EventArchetype("E008", "EDGAR 8-K",        "Going concern notice or major restatement",                               "That stock",   "SHORT",  4, "Same day", 0.78),
    EventArchetype("E048", "EDGAR 8-K",        "8-K: Large unexpected C-suite insider sell >$5M",                        "That stock",   "SHORT",  3, "Same day", 0.72),
    EventArchetype("E049", "EDGAR 8-K",        "8-K: Surprise share buyback authorization announced",                    "That stock",   "LONG",   3, "Same day", 0.75),
    EventArchetype("E050", "EDGAR 8-K",        "8-K: Unexpected CEO or CFO resignation disclosed",                       "That stock",   "SHORT",  3, "Same day", 0.80),
    EventArchetype("E051", "EDGAR 8-K",        "8-K: SEC subpoena, DOJ investigation, or regulatory inquiry disclosed",  "That stock",   "SHORT",  4, "Same day", 0.82),
    EventArchetype("E052", "EDGAR 8-K",        "8-K: Material government contract awarded (defense, infrastructure)",    "That stock",   "LONG",   3, "Same day", 0.75),
    EventArchetype("E053", "EDGAR 8-K",        "8-K: Surprise revenue warning or mid-quarter guidance cut",              "That stock",   "SHORT",  4, "Same day", 0.85),
    # ── PACER ────────────────────────────────────────────────────────────────
    EventArchetype("E016", "PACER",            "Major Chapter 11 bankruptcy filing for public company",                  "That stock",   "SHORT",  4, "Same day", 0.80, "High confidence. Must identify sector."),
    EventArchetype("E054", "PACER",            "PACER: Major antitrust ruling against large-cap company",                "That stock",   "SHORT",  4, "Same day", 0.80),
    EventArchetype("E055", "PACER",            "PACER: Patent infringement verdict — large damages against company",     "That stock",   "SHORT",  3, "Same day", 0.75),
    EventArchetype("E056", "PACER",            "PACER: Environmental lawsuit settlement — large dollar amount",          "That stock",   "SHORT",  3, "1 day",   0.72),
    # ── FDA MedWatch ─────────────────────────────────────────────────────────
    EventArchetype("E009", "FDA MedWatch",     "Drug approval for specific company",                                      "That biotech", "LONG",   5, "Same day", 0.88, "Binary outcome. High win rate historically."),
    EventArchetype("E010", "FDA MedWatch",     "Drug rejection or safety recall for specific company",                    "That biotech", "SHORT",  5, "Same day", 0.88),
    EventArchetype("E057", "FDA MedWatch",     "FDA breakthrough therapy designation granted to company",                 "That biotech", "LONG",   3, "Same day", 0.78),
    EventArchetype("E058", "FDA MedWatch",     "FDA Complete Response Letter (CRL) issued — drug rejected",              "That biotech", "SHORT",  5, "Same day", 0.90),
    EventArchetype("E059", "FDA MedWatch",     "FDA import alert issued against foreign drug manufacturer",               "That pharma",  "SHORT",  3, "Same day", 0.75),
    EventArchetype("E060", "FDA MedWatch",     "FDA safety communication: new black box warning added",                   "That pharma",  "SHORT",  3, "1 day",   0.78),
    # ── Whale Alert ──────────────────────────────────────────────────────────
    EventArchetype("E011", "Whale Alert",      "Large BTC transfer TO known exchange wallet (>$50M)",                    "BTC",          "SHORT",  3, "2 hr",    0.65),
    EventArchetype("E012", "Whale Alert",      "Large BTC transfer FROM exchange to cold wallet (>$50M)",                "BTC",          "LONG",   2, "4 hr",    0.60),
    EventArchetype("E061", "Whale Alert",      "Large ETH transfer TO exchange >$50M (selling signal)",                  "ETH, BTC",     "SHORT",  3, "2 hr",    0.65),
    EventArchetype("E062", "Whale Alert",      "Large ETH transfer FROM exchange to cold wallet >$50M (accumulation)",   "ETH, BTC",     "LONG",   2, "4 hr",    0.60),
    EventArchetype("E063", "Whale Alert",      "Multiple whale wallets accumulating same token within 4-hour window",    "BTC",          "LONG",   3, "6 hr",    0.68),
    # ── Coinglass ────────────────────────────────────────────────────────────
    EventArchetype("E013", "Coinglass",        "Price within 0.3% of large long liquidation cluster above",              "BTC",          "LONG",   3, "30 min",  0.68),
    EventArchetype("E064", "Coinglass",        "Price within 0.3% of large SHORT liquidation cluster below spot",        "BTC",          "SHORT",  3, "30 min",  0.68),
    EventArchetype("E065", "Coinglass",        "Funding rate spikes to extreme positive (>0.1%/8hr) — longs over-leveraged","BTC",       "SHORT",  3, "4 hr",    0.72),
    EventArchetype("E066", "Coinglass",        "Funding rate drops to extreme negative (<-0.05%/8hr) — shorts over-leveraged","BTC",     "LONG",   3, "4 hr",    0.72),
    EventArchetype("E067", "Coinglass",        "Open interest drops >15% in under 1 hour (mass deleveraging cascade)",   "BTC",          "VARIES", 4, "1 hr",    0.70),
    # ── X / Trump ────────────────────────────────────────────────────────────
    EventArchetype("E014", "X / Trump account","Post explicitly positive about crypto or Bitcoin",                        "BTC",          "LONG",   4, "1 hr",    0.75, "Documented 5-10% BTC moves historically."),
    EventArchetype("E068", "X / Trump",        "Trump posts explicitly negative about crypto — calls for ban or crackdown","BTC",        "SHORT",  4, "1 hr",    0.75),
    EventArchetype("E069", "X / Trump",        "SEC Chair or key regulator posts about crypto enforcement action",        "BTC",          "SHORT",  3, "2 hr",    0.72),
    EventArchetype("E070", "X / Trump",        "Sovereign wealth fund or nation announces Bitcoin reserve purchase",      "BTC",          "LONG",   5, "1 day",   0.85),
    # ── Reddit velocity ───────────────────────────────────────────────────────
    EventArchetype("E018", "Reddit velocity",  "Ticker mentions spike from <10 to >200 in 5 minutes",                   "That stock",   "LONG",   3, "2 hr",    0.62),
    EventArchetype("E071", "Reddit velocity",  "Short squeeze setup: high short interest + Reddit velocity spike",        "That stock",   "LONG",   4, "2 hr",    0.68),
    # ── Stablecoin ────────────────────────────────────────────────────────────
    EventArchetype("E019", "Stablecoin mint",  "USDT or USDC mint event >$500M in single transaction",                  "BTC",          "LONG",   2, "6 hr",    0.58),
    EventArchetype("E072", "Stablecoin mint",  "USDT or USDC burn/redemption >$500M — liquidity drain",                 "BTC",          "SHORT",  2, "6 hr",    0.58),
    EventArchetype("E073", "Stablecoin mint",  "Multiple stablecoin mints >$1B total within 24-hour window",             "BTC",          "LONG",   3, "12 hr",   0.65),
    # ── Federal Register ─────────────────────────────────────────────────────
    EventArchetype("E020", "Federal Register", "Emergency rule affecting specific sector published",                      "Sector ETF",   "VARIES", 3, "Same day", 0.70),
    EventArchetype("E074", "Federal Register", "Emergency EPA rule restricting oil/gas drilling or transport",            "MCL",          "VARIES", 3, "1 day",   0.72),
    EventArchetype("E075", "Federal Register", "Emergency financial regulation — new capital requirements for banks",    "MES",          "SHORT",  3, "1 day",   0.70),
    EventArchetype("E076", "Federal Register", "Proposed crypto regulation published for public comment",                 "BTC",          "VARIES", 2, "Same day", 0.62),
    # ── GDELT (transformer-only) ─────────────────────────────────────────────
    EventArchetype("E017", "GDELT",            "Conflict escalation score spike in oil-producing region",                "MCL",          "LONG",   2, "3 hr",    0.55),
    EventArchetype("E077", "GDELT",            "GDELT conflict spike in Russia/Ukraine (energy supply at risk)",         "MCL, M6E",     "LONG",   3, "3 hr",    0.60),
    EventArchetype("E078", "GDELT",            "GDELT conflict spike in Taiwan Strait or South China Sea",               "MES, MGC",     "SHORT",  4, "6 hr",    0.62),
    EventArchetype("E079", "GDELT",            "GDELT political instability spike in Saudi Arabia",                      "MCL",          "LONG",   4, "6 hr",    0.65),
    # ── SEC Enforcement ───────────────────────────────────────────────────────
    EventArchetype("E080", "SEC Enforcement",  "SEC emergency trading halt (suspension) on specific stock",              "That stock",   "SHORT",  5, "2 days",  0.92),
    EventArchetype("E081", "SEC Enforcement",  "SEC charges filed against major crypto exchange or named token",         "BTC",          "SHORT",  4, "1 day",   0.82),
    EventArchetype("E082", "SEC Enforcement",  "SEC approves new spot crypto ETF product",                               "BTC",          "LONG",   5, "1 day",   0.88),
    # ── FCC Enforcement ───────────────────────────────────────────────────────
    EventArchetype("E083", "FCC Enforcement",  "FCC major fine or enforcement against large telecom company",            "That telecom", "SHORT",  3, "Same day", 0.75),
    EventArchetype("E084", "FCC Enforcement",  "FCC spectrum auction results — unexpected winner or loser",              "Telecom",      "VARIES", 2, "Same day", 0.65),
    # ── NOAA Space Weather ────────────────────────────────────────────────────
    EventArchetype("E085", "NOAA Space Weather","NOAA issues G4 or G5 geomagnetic storm warning",                        "MGC",          "LONG",   2, "1 day",   0.60),
    EventArchetype("E086", "NOAA Space Weather","NOAA X-class solar flare — satellite/GPS communications disrupted",     "MGC",          "LONG",   2, "6 hr",    0.58),
    # ── On-chain data ─────────────────────────────────────────────────────────
    EventArchetype("E087", "On-chain data",    "Miner BTC sell-off: >5,000 BTC from miner wallets in 24 hours",          "BTC",          "SHORT",  3, "1 day",   0.65),
    EventArchetype("E088", "On-chain data",    "Exchange BTC reserves drop to multi-year low (supply shock incoming)",   "BTC",          "LONG",   3, "1 day",   0.68),
    EventArchetype("E089", "On-chain data",    "SOPR ratio hits extreme: >3.5 euphoria or <0.5 capitulation",            "BTC",          "VARIES", 3, "1 day",   0.65),
    EventArchetype("E090", "On-chain data",    "BTC options max pain diverges >10% from spot — expiry magnet",           "BTC",          "VARIES", 2, "Until expiry", 0.60),
]

NO_EVENT          = "NO_EVENT"
EVENT_IDS         = [e.event_id for e in TAXONOMY] + [NO_EVENT]   # 91 classes
EVENT_ID_TO_IDX   = {eid: i for i, eid in enumerate(EVENT_IDS)}
IDX_TO_EVENT_ID   = {i: eid for eid, i in EVENT_ID_TO_IDX.items()}
TAXONOMY_MAP      = {e.event_id: e for e in TAXONOMY}
NUM_LABELS        = len(EVENT_IDS)  # 91


# ═══════════════════════════════════════════════════════
# SECTION 2: CLASSIFICATION RESULT
# ═══════════════════════════════════════════════════════

@dataclass
class ClassificationResult:
    event_id:   str
    confidence: float
    source:     str
    path:       str             # "keyword" | "transformer" | "none"
    latency_ms: float
    ticker:     Optional[str] = None    # Populated for E007/E008/E009/E010/E016
    raw_text:   str = ""

    def is_tradeable(self) -> bool:
        """Returns True if event is real and confidence meets its archetype threshold."""
        if self.event_id == NO_EVENT:
            return False
        archetype = TAXONOMY_MAP.get(self.event_id)
        if archetype is None:
            return False
        return self.confidence >= archetype.confidence_threshold


# ═══════════════════════════════════════════════════════
# SECTION 3: FAST PATH — KEYWORD CLASSIFIER
#
# Source-specific pattern matching, sub-1ms per item.
# Handles all 13 structured sources in the SOURCES tab.
#
# Justified by FinMTEB finding: keyword models outperform
# dense embeddings on structured regulatory filings.
# ═══════════════════════════════════════════════════════

class KeywordClassifier:

    # ── Fed terms ─────────────────────────────────────────────────────────────
    DOVISH = [
        "rate cut", "lower rates", "reduce the target range", "accommodative",
        "easing", "support economic activity", "below target", "downside risks",
        "pause", "patient", "data dependent", "inflation has eased",
        "labor market has cooled", "overly restrictive",
    ]
    HAWKISH = [
        "rate hike", "raise rates", "increase the target range", "restrictive",
        "tightening", "inflationary pressures", "above target", "upside risks",
        "further increases", "higher for longer", "50 basis points", "75 basis points",
        "inflation remains elevated", "additional firming", "ongoing increases",
    ]

    # ── FDA terms ─────────────────────────────────────────────────────────────
    FDA_APPROVE = [
        "approved", "approves", "approval", "cleared", "granted authorization",
        "authorized", "breakthrough therapy", "accelerated approval", "full approval",
        "granted priority review", "bla approved", "nda approved",
        "grants approval", "fda clears",
    ]
    FDA_REJECT = [
        "complete response letter", "not approved", "rejected", "refusal to file",
        "clinical hold", "warning letter", "recall", "safety alert",
        "market withdrawal", "adverse event", "crl issued", "refuse to file",
        "black box warning", "rejects", "not approve",
    ]

    # ── EDGAR terms ───────────────────────────────────────────────────────────
    EDGAR_MA = [
        "merger agreement", "acquisition", "definitive agreement", "going private",
        "tender offer", "business combination", "to be acquired", "to acquire",
        "strategic alternatives", "sale of the company", "merger consideration",
        "announces definitive", "agree to be acquired",
    ]
    EDGAR_NEG = [
        "going concern", "substantial doubt", "restatement", "material weakness",
        "accounting error", "fraud", "default", "delisting notice",
        "chapter 11", "bankruptcy", "insolvency", "covenant default",
    ]

    # ── Strait / canal terms ──────────────────────────────────────────────────
    STRAITS = [
        "strait of hormuz", "hormuz", "suez canal", "suez",
        "bab el-mandeb", "bab-el-mandeb", "strait of malacca",
        "persian gulf", "red sea transit",
    ]
    INCIDENT = [
        "blocked", "closed", "disrupted", "incident", "vessel aground",
        "collision", "attack", "seized", "fire aboard", "explosion",
        "force majeure", "diverted",
    ]

    # ── Pipeline terms ────────────────────────────────────────────────────────
    PIPELINE_INCIDENT = [
        "rupture", "explosion", "fire", "hazmat", "spill", "leak",
        "emergency shutdown", "force majeure", "outage", "burst",
    ]
    PIPELINE_ASSET = ["pipeline", "crude", "natural gas", "petroleum", "transmission line"]

    # ── Bankruptcy terms ──────────────────────────────────────────────────────
    BANKRUPTCY = [
        "chapter 11", "chapter 7", "voluntary petition", "bankruptcy protection",
        "filed for bankruptcy", "reorganization plan", "debtor in possession",
        "bankruptcy filing", "seeks bankruptcy",
    ]

    # ── Emergency rule terms ──────────────────────────────────────────────────
    EMERGENCY_RULE = [
        "interim final rule", "emergency rule", "immediate effect",
        "temporary rule", "without prior notice", "good cause", "emergency order",
        "effective immediately upon publication",
    ]

    # ── Trump/X crypto terms ──────────────────────────────────────────────────
    CRYPTO_POSITIVE = [
        "bitcoin", "btc", "crypto", "digital asset", "blockchain",
        "strategic reserve", "defi", "digital currency",
    ]
    POSITIVE_WORDS = [
        "great", "amazing", "love", "approve", "support", "best",
        "fantastic", "wonderful", "strategic", "buy", "backing",
    ]

    # ── QE / QT terms ────────────────────────────────────────────────────────
    QE_TERMS = [
        "asset purchases", "quantitative easing", "qe", "balance sheet expand",
        "purchase program", "treasury purchases", "mortgage-backed securities",
        "restart purchases", "resume purchases",
    ]
    QT_TERMS = [
        "quantitative tightening", "qt", "balance sheet reduce", "runoff",
        "accelerate runoff", "shrink balance sheet", "faster reduction",
        "above schedule", "beyond schedule",
    ]
    FED_EMERGENCY = [
        "emergency meeting", "unscheduled meeting", "special meeting",
        "conference call", "interim meeting", "extraordinary session",
    ]

    # ── ECB terms ─────────────────────────────────────────────────────────────
    ECB_DOVISH = [
        "ecb rate cut", "ecb easing", "ecb dovish", "deposit rate lower",
        "ecb accommodative", "ecb stimulus", "ecb cut", "lagarde dovish",
        "targeted ltro", "app expansion",
    ]
    ECB_HAWKISH = [
        "ecb rate hike", "ecb tightening", "ecb hawkish", "deposit rate higher",
        "ecb restrictive", "ecb increase", "lagarde hawkish", "ecb raise",
    ]
    ECB_EMERGENCY = [
        "ecb emergency", "emergency liquidity", "ela", "banking stress",
        "systemic risk", "bank contagion", "ecb backstop", "omt activation",
    ]

    # ── BLS / macro terms ────────────────────────────────────────────────────
    CPI_BELOW = [
        "cpi below", "inflation cooled", "inflation lower than expected",
        "consumer prices fell", "headline cpi", "core cpi below", "disinflation",
        "price pressures eased", "cooler than expected",
    ]
    CPI_ABOVE = [
        "cpi above", "inflation hotter", "inflation higher than expected",
        "consumer prices rose", "cpi surge", "core cpi above", "inflation accelerated",
        "price pressures increased", "hotter than expected",
    ]
    NFP_ABOVE = [
        "nonfarm payroll", "nfp", "jobs added", "payrolls beat", "employment above",
        "stronger than expected", "jobs report beat", "unemployment fell",
    ]
    NFP_BELOW = [
        "nonfarm payroll", "nfp", "jobs lost", "payrolls miss", "employment below",
        "weaker than expected", "jobs report miss", "unemployment rose",
    ]
    PCE_TERMS = [
        "pce", "personal consumption expenditure", "core pce", "pce deflator",
        "pce inflation", "above consensus", "below consensus",
    ]

    # ── OPEC terms ────────────────────────────────────────────────────────────
    OPEC_CUT = [
        "production cut", "output reduction", "quota reduction", "voluntary cut",
        "bpd cut", "barrel per day reduction", "opec cut", "opec+ cut",
        "supply reduction", "curtailment",
    ]
    OPEC_INCREASE = [
        "production increase", "output increase", "raise output", "increase quota",
        "bpd increase", "opec increase", "opec+ hike", "supply increase",
        "output hike", "production hike",
    ]
    OPEC_EMERGENCY = [
        "emergency meeting", "extraordinary meeting", "unscheduled opec",
        "urgent meeting", "opec crisis", "special session", "emergency session",
    ]

    # ── EDGAR extended terms ─────────────────────────────────────────────────
    INSIDER_SELL = [
        "form 4", "insider sell", "insider sold", "insider dispose",
        "officer sold", "director sold", "10b5-1", "open market sale",
        "beneficial owner sold",
    ]
    BUYBACK = [
        "repurchase program", "share repurchase", "buyback", "buy back",
        "stock repurchase", "repurchase authorization", "common stock repurchase",
    ]
    EXEC_RESIGN = [
        "resigned", "resignation", "stepping down", "departure effective",
        "ceo departure", "cfo departure", "executive departure",
        "effective immediately", "leaves the company",
    ]
    SEC_SUBPOENA = [
        "subpoena", "doj investigation", "sec investigation", "regulatory inquiry",
        "civil investigative demand", "grand jury", "doj subpoena", "sec probe",
        "under investigation",
    ]
    GOV_CONTRACT = [
        "government contract", "dod contract", "defense contract", "awarded contract",
        "contract awarded", "military contract", "federal contract", "billion contract",
    ]
    GUIDANCE_CUT = [
        "guidance", "lower guidance", "reduce guidance", "preliminary results",
        "below expectations", "revenue warning", "shortfall", "miss",
        "below prior guidance", "expects to report below",
    ]

    # ── PACER extended terms ─────────────────────────────────────────────────
    ANTITRUST = [
        "antitrust", "monopoly", "sherman act", "doj antitrust", "ftc antitrust",
        "anti-competitive", "market power", "injunction", "breakup", "divestiture",
    ]
    PATENT = [
        "patent infringement", "patent violation", "willful infringement",
        "patent damages", "royalty", "patent verdict", "ipr", "inter partes",
    ]
    ENVIRONMENTAL_SUIT = [
        "environmental settlement", "epa fine", "clean air act", "clean water act",
        "superfund", "environmental damages", "pollution settlement", "toxic waste",
    ]

    # ── FDA extended terms ────────────────────────────────────────────────────
    FDA_BREAKTHROUGH = [
        "breakthrough therapy", "bt designation", "breakthrough designation",
        "expedited development", "fast track", "priority review granted",
    ]
    FDA_CRL = [
        "complete response letter", "crl", "not approvable", "refuse to file",
        "reject", "rejection", "not approved",
    ]
    FDA_IMPORT_ALERT = [
        "import alert", "import ban", "detention without physical examination",
        "dwpe", "foreign facility", "cgmp violation", "manufacturing deficiencies",
    ]
    FDA_BLACKBOX = [
        "black box warning", "boxed warning", "safety communication",
        "new warning", "serious adverse", "risk evaluation", "rems",
    ]

    # ── Coinglass extended terms ──────────────────────────────────────────────
    FUNDING_HIGH = [
        "funding rate", "positive funding", "longs paying", "crowded long",
        "elevated funding", "funding above", "over-leveraged long",
    ]
    FUNDING_LOW = [
        "funding rate", "negative funding", "shorts paying", "crowded short",
        "extreme negative", "funding below", "over-leveraged short",
    ]
    OI_DROP = [
        "open interest", "oi drop", "deleveraging", "liquidation cascade",
        "flush", "mass liquidation", "oi fell", "open interest decline",
    ]

    # ── Trump/X extended terms ────────────────────────────────────────────────
    CRYPTO_NEGATIVE = [
        "ban", "restrict", "crackdown", "regulate crypto", "illegal crypto",
        "fraud", "criminal bitcoin", "bitcoin ban", "shut down crypto",
    ]
    SEC_ENFORCEMENT_TERMS = [
        "sec enforcement", "sec charges", "securities violation", "unregistered",
        "sec action", "crypto enforcement", "charged with", "sec complaint",
    ]
    NATION_RESERVE = [
        "sovereign reserve", "national bitcoin", "strategic bitcoin",
        "government purchase", "treasury bitcoin", "national reserve",
        "sovereign wealth fund", "state purchase",
    ]

    # ── SEC Enforcement terms ─────────────────────────────────────────────────
    SEC_HALT = [
        "trading suspension", "trading halt", "sec suspended", "cease trading",
        "trading suspended", "18j-1", "sec halts", "emergency halt",
    ]
    SEC_CRYPTO_CHARGE = [
        "sec charges", "sec complaint", "unregistered exchange", "unregistered securities",
        "crypto securities", "sec filed", "binance charges", "coinbase charges",
    ]
    SEC_ETF_APPROVE = [
        "spot bitcoin etf", "crypto etf approved", "19b-4", "sec approves etf",
        "bitcoin etf approval", "spot etf", "rule change effective",
    ]

    # ── FCC terms ────────────────────────────────────────────────────────────
    FCC_FINE = [
        "fcc fine", "forfeiture", "notice of apparent liability", "nal",
        "fcc penalty", "fcc enforcement", "fcc consent decree", "fcc order",
    ]
    FCC_SPECTRUM = [
        "spectrum auction", "fcc auction", "mhz", "ghz license", "spectrum license",
        "auction results", "spectrum winner", "bid awarded",
    ]

    # ── NOAA Space Weather terms ──────────────────────────────────────────────
    GEOMAGNETIC_SEVERE = [
        "g4", "g5", "severe geomagnetic", "extreme geomagnetic",
        "geomagnetic storm warning", "kp index", "solar storm severe",
        "swpc alert", "noaa space weather",
    ]
    SOLAR_FLARE = [
        "x-class", "x1 flare", "x2 flare", "x5 flare", "solar flare",
        "radio blackout", "satellite disruption", "gps disruption",
        "shortwave fadeout", "noaa flare",
    ]

    # ── On-chain terms ────────────────────────────────────────────────────────
    MINER_SELLOFF = [
        "miner outflow", "mining pool", "miner selling", "miner capitulation",
        "f2pool", "antpool", "btc miner", "miner wallet", "miner transfer",
    ]
    EXCHANGE_RESERVES_LOW = [
        "exchange reserves", "exchange balance", "btc on exchange", "exchange outflow",
        "supply shock", "exchange btc fell", "multi-year low reserves",
    ]
    SOPR_EXTREME = [
        "sopr", "spent output profit ratio", "realized profit", "realized loss",
        "on-chain profit", "euphoria", "capitulation signal", "sopr above",
    ]
    MAX_PAIN = [
        "max pain", "options expiry", "deribit expiry", "strike concentration",
        "open interest expiry", "pin risk", "gamma exposure", "options max pain",
    ]

    # ── Ticker NER ────────────────────────────────────────────────────────────
    _SKIP_TOKENS = {
        "CEO", "CFO", "COO", "CTO", "LLC", "INC", "LTD", "SEC", "FDA",
        "EIA", "FCC", "NFP", "CPI", "GDP", "ETF", "ESG", "NDA", "BLA",
        "IPO", "SPV", "CRL", "TFR", "AIS", "RSS", "XML", "API", "USD",
        "USDT", "USDC", "BTC", "ETH", "NYSE", "NASDAQ", "OTC", "IBKR",
    }
    _EXCHANGE_PATTERN = re.compile(r'(?:NYSE|NASDAQ|OTC)[:\s]+([A-Z]{1,5})')
    _PAREN_PATTERN    = re.compile(r'\(([A-Z]{2,5})\)')
    _DOLLAR_PATTERN   = re.compile(r'\$([A-Z]{1,5})\b')

    # ─────────────────────────────────────────────────────────────────────────

    def _extract_ticker(self, text: str) -> Optional[str]:
        """Extract most likely stock ticker. Exchange-prefixed > dollar-sign > parenthetical."""
        m = self._EXCHANGE_PATTERN.search(text)
        if m:
            return m.group(1)
        m = self._DOLLAR_PATTERN.search(text)
        if m and m.group(1) not in self._SKIP_TOKENS:
            return m.group(1)
        m = self._PAREN_PATTERN.search(text)
        if m and m.group(1) not in self._SKIP_TOKENS:
            return m.group(1)
        return None

    def _score(self, text_lower: str, terms: list[str]) -> float:
        """Fraction of terms found, floored at 0. Non-linear so 2+ hits give real signal."""
        hits = sum(1 for t in terms if t in text_lower)
        if hits == 0:
            return 0.0
        return min(0.30 + hits * 0.12, 0.95)

    def _result(self, eid, conf, source, t0, ticker=None, text="") -> ClassificationResult:
        return ClassificationResult(eid, conf, source, "keyword",
                                    (time.perf_counter() - t0) * 1000,
                                    ticker=ticker, raw_text=text)

    # ── Per-source classifiers ────────────────────────────────────────────────

    def classify_eia(self, item: dict) -> ClassificationResult:
        """
        EIA crude/natgas pre-parsed JSON.
        Fields: actual_mmb (float), consensus_mmb (float), report_type ('crude'|'natgas').
        Negative delta = draw (bullish), positive = build (bearish).
        """
        t0        = time.perf_counter()
        text      = item.get("text", "")
        actual    = float(item.get("actual_mmb", 0.0))
        consensus = float(item.get("consensus_mmb", 0.0))
        delta     = actual - consensus
        rtype     = item.get("report_type", "crude").lower()

        if rtype == "natgas":
            THRESH = 10.0  # bcf
            if delta <= -THRESH:
                conf = min(0.58 + abs(delta) * 0.003, 0.88)
                return self._result("E022", conf, "EIA Natural Gas", t0, text=text)
            elif delta >= THRESH:
                conf = min(0.55 + abs(delta) * 0.003, 0.85)
                return self._result("E023", conf, "EIA Natural Gas", t0, text=text)
            return self._result(NO_EVENT, 0.0, "EIA Natural Gas", t0, text=text)

        # Crude
        BIG_THRESH   = 1.5   # MMB — large surprise
        SMALL_THRESH = 0.3   # MMB — small draw (E021)
        if delta <= -BIG_THRESH:
            conf = min(0.62 + abs(delta) * 0.06, 0.93)
            return self._result("E001", conf, "EIA Petroleum", t0, text=text)
        elif delta >= BIG_THRESH:
            conf = min(0.62 + abs(delta) * 0.06, 0.93)
            return self._result("E002", conf, "EIA Petroleum", t0, text=text)
        elif -BIG_THRESH < delta <= -SMALL_THRESH:
            conf = min(0.55 + abs(delta) * 0.04, 0.72)
            return self._result("E021", conf, "EIA Petroleum", t0, text=text)
        return self._result(NO_EVENT, 0.0, "EIA Petroleum", t0, text=text)

    def classify_fed(self, text: str) -> ClassificationResult:
        t0 = time.perf_counter()
        tl = text.lower()

        # Emergency / structural signals take priority
        if any(t in tl for t in self.FED_EMERGENCY):
            return self._result("E026", 0.76, "Fed RSS", t0, text=text)
        if self._score(tl, self.QE_TERMS) > 0.20:
            return self._result("E024", min(self._score(tl, self.QE_TERMS) + 0.50, 0.93), "Fed RSS", t0, text=text)
        if self._score(tl, self.QT_TERMS) > 0.20:
            return self._result("E025", min(self._score(tl, self.QT_TERMS) + 0.48, 0.90), "Fed RSS", t0, text=text)

        ds = self._score(tl, self.DOVISH)
        hs = self._score(tl, self.HAWKISH)

        # FOMC minutes (longer documents — detect from context)
        is_minutes = "minutes" in tl or "meeting of the federal open market" in tl
        if is_minutes:
            if ds > hs and ds > 0.15:
                return self._result("E027", min(ds + 0.35, 0.88), "Fed RSS", t0, text=text)
            if hs > ds and hs > 0.15:
                return self._result("E028", min(hs + 0.35, 0.88), "Fed RSS", t0, text=text)

        if ds > hs and ds > 0.20:
            return self._result("E003", min(ds + 0.28, 0.91), "Fed RSS", t0, text=text)
        if hs > ds and hs > 0.20:
            return self._result("E004", min(hs + 0.28, 0.91), "Fed RSS", t0, text=text)
        return self._result(NO_EVENT, 0.0, "Fed RSS", t0, text=text)

    def classify_ecb(self, text: str) -> ClassificationResult:
        t0 = time.perf_counter()
        tl = text.lower()
        if self._score(tl, self.ECB_EMERGENCY) > 0.20:
            return self._result("E031", min(self._score(tl, self.ECB_EMERGENCY) + 0.45, 0.88), "ECB RSS", t0, text=text)
        ds = self._score(tl, self.ECB_DOVISH)
        hs = self._score(tl, self.ECB_HAWKISH)
        if ds > hs and ds > 0.15:
            return self._result("E029", min(ds + 0.45, 0.91), "ECB RSS", t0, text=text)
        if hs > ds and hs > 0.15:
            return self._result("E030", min(hs + 0.45, 0.91), "ECB RSS", t0, text=text)
        return self._result(NO_EVENT, 0.0, "ECB RSS", t0, text=text)

    def classify_bls(self, text: str) -> ClassificationResult:
        """BLS macro releases: CPI, NFP, PCE."""
        t0 = time.perf_counter()
        tl = text.lower()
        is_cpi = "consumer price" in tl or " cpi " in tl
        is_nfp = "nonfarm" in tl or " nfp " in tl or "payroll" in tl
        is_pce = "personal consumption" in tl or " pce " in tl

        if is_cpi:
            below = self._score(tl, self.CPI_BELOW)
            above = self._score(tl, self.CPI_ABOVE)
            if below > above and below > 0.15:
                return self._result("E032", min(below + 0.52, 0.92), "BLS Feed", t0, text=text)
            if above > below and above > 0.15:
                return self._result("E033", min(above + 0.52, 0.92), "BLS Feed", t0, text=text)
        if is_nfp:
            above = self._score(tl, self.NFP_ABOVE)
            below = self._score(tl, self.NFP_BELOW)
            beat  = any(w in tl for w in ["beat", "above", "stronger", "added more"])
            miss  = any(w in tl for w in ["miss", "below", "weaker", "lost", "fewer"])
            if (above > 0.15 or beat) and not miss:
                return self._result("E034", min(above + 0.55, 0.90), "BLS Feed", t0, text=text)
            if (below > 0.15 or miss) and not beat:
                return self._result("E035", min(below + 0.55, 0.90), "BLS Feed", t0, text=text)
        if is_pce and self._score(tl, self.PCE_TERMS) > 0.15:
            return self._result("E036", min(self._score(tl, self.PCE_TERMS) + 0.45, 0.88), "BLS Feed", t0, text=text)
        return self._result(NO_EVENT, 0.0, "BLS Feed", t0, text=text)

    def classify_opec(self, text: str) -> ClassificationResult:
        t0 = time.perf_counter()
        tl = text.lower()
        if self._score(tl, self.OPEC_EMERGENCY) > 0.20:
            return self._result("E039", min(self._score(tl, self.OPEC_EMERGENCY) + 0.35, 0.84), "OPEC RSS", t0, text=text)
        cs = self._score(tl, self.OPEC_CUT)
        is_ = self._score(tl, self.OPEC_INCREASE)
        if cs > is_ and cs > 0.20:
            return self._result("E037", min(cs + 0.48, 0.93), "OPEC RSS", t0, text=text)
        if is_ > cs and is_ > 0.20:
            return self._result("E038", min(is_ + 0.45, 0.91), "OPEC RSS", t0, text=text)
        # Threat to leave / quota non-compliance
        if any(t in tl for t in ["exceed quota", "non-compliance", "leave opec", "withdraw from opec", "not comply"]):
            return self._result("E040", 0.66, "OPEC RSS", t0, text=text)
        return self._result(NO_EVENT, 0.0, "OPEC RSS", t0, text=text)

    def classify_fda(self, text: str) -> ClassificationResult:
        t0  = time.perf_counter()
        tl  = text.lower()
        tkr = self._extract_ticker(text)
        # Check most specific signals first
        if self._score(tl, self.FDA_CRL) > 0.20:
            return self._result("E058", min(self._score(tl, self.FDA_CRL) + 0.50, 0.94), "FDA MedWatch", t0, tkr, text)
        if self._score(tl, self.FDA_IMPORT_ALERT) > 0.15:
            return self._result("E059", min(self._score(tl, self.FDA_IMPORT_ALERT) + 0.42, 0.88), "FDA MedWatch", t0, tkr, text)
        if self._score(tl, self.FDA_BLACKBOX) > 0.15:
            return self._result("E060", min(self._score(tl, self.FDA_BLACKBOX) + 0.42, 0.88), "FDA MedWatch", t0, tkr, text)
        if self._score(tl, self.FDA_BREAKTHROUGH) > 0.15:
            return self._result("E057", min(self._score(tl, self.FDA_BREAKTHROUGH) + 0.42, 0.88), "FDA MedWatch", t0, tkr, text)
        app = self._score(tl, self.FDA_APPROVE)
        rej = self._score(tl, self.FDA_REJECT)
        if app > rej and app > 0.15:
            return self._result("E009", min(app + 0.42, 0.93), "FDA MedWatch", t0, tkr, text)
        if rej > app and rej > 0.15:
            return self._result("E010", min(rej + 0.42, 0.93), "FDA MedWatch", t0, tkr, text)
        return self._result(NO_EVENT, 0.0, "FDA MedWatch", t0, tkr, text)

    def classify_edgar(self, text: str) -> ClassificationResult:
        t0  = time.perf_counter()
        tl  = text.lower()
        tkr = self._extract_ticker(text)
        # More specific signals first
        if self._score(tl, self.GUIDANCE_CUT) > 0.20:
            return self._result("E053", min(self._score(tl, self.GUIDANCE_CUT) + 0.40, 0.92), "EDGAR 8-K", t0, tkr, text)
        if self._score(tl, self.SEC_SUBPOENA) > 0.15:
            return self._result("E051", min(self._score(tl, self.SEC_SUBPOENA) + 0.42, 0.90), "EDGAR 8-K", t0, tkr, text)
        if self._score(tl, self.EXEC_RESIGN) > 0.20:
            return self._result("E050", min(self._score(tl, self.EXEC_RESIGN) + 0.38, 0.88), "EDGAR 8-K", t0, tkr, text)
        if self._score(tl, self.BUYBACK) > 0.20:
            return self._result("E049", min(self._score(tl, self.BUYBACK) + 0.38, 0.88), "EDGAR 8-K", t0, tkr, text)
        if self._score(tl, self.GOV_CONTRACT) > 0.20:
            return self._result("E052", min(self._score(tl, self.GOV_CONTRACT) + 0.38, 0.88), "EDGAR 8-K", t0, tkr, text)
        if self._score(tl, self.INSIDER_SELL) > 0.15:
            return self._result("E048", min(self._score(tl, self.INSIDER_SELL) + 0.35, 0.85), "EDGAR 8-K", t0, tkr, text)
        ma  = self._score(tl, self.EDGAR_MA)
        neg = self._score(tl, self.EDGAR_NEG)
        if ma > neg and ma > 0.15:
            return self._result("E007", min(ma + 0.34, 0.90), "EDGAR 8-K", t0, tkr, text)
        if neg > ma and neg > 0.15:
            return self._result("E008", min(neg + 0.34, 0.90), "EDGAR 8-K", t0, tkr, text)
        return self._result(NO_EVENT, 0.0, "EDGAR 8-K", t0, tkr, text)

    def classify_whale_alert(self, item: dict) -> ClassificationResult:
        """
        Whale Alert pre-parsed JSON.
        Fields: amount_usd, from_owner_type, to_owner_type (exchange/unknown/wallet),
                token ('BTC'|'ETH'|...), wallet_count (int, for multi-wallet accumulation).
        """
        t0          = time.perf_counter()
        text        = item.get("text", "")
        amount      = float(item.get("amount_usd", 0))
        from_type   = item.get("from_owner_type", "").lower()
        to_type     = item.get("to_owner_type",   "").lower()
        token       = item.get("token", "BTC").upper()
        wallet_count= int(item.get("wallet_count", 1))

        MIN_USD = 50_000_000
        if amount < MIN_USD:
            return self._result(NO_EVENT, 0.0, "Whale Alert", t0, text=text)

        size_boost = min((amount / 100_000_000) * 0.04, 0.12)

        # Multi-wallet accumulation (E063) takes priority
        if wallet_count >= 3 and to_type != "exchange":
            return self._result("E063", min(0.60 + size_boost + wallet_count * 0.02, 0.80), "Whale Alert", t0, text=text)

        if token == "ETH":
            if to_type == "exchange":
                return self._result("E061", 0.60 + size_boost, "Whale Alert", t0, text=text)
            if from_type == "exchange":
                return self._result("E062", 0.55 + size_boost, "Whale Alert", t0, text=text)
        else:  # BTC default
            if to_type == "exchange":
                return self._result("E011", 0.60 + size_boost, "Whale Alert", t0, text=text)
            if from_type == "exchange":
                return self._result("E012", 0.55 + size_boost, "Whale Alert", t0, text=text)
        return self._result(NO_EVENT, 0.0, "Whale Alert", t0, text=text)

    def classify_coast_guard(self, item) -> ClassificationResult:
        """
        Accepts dict (with optional ais_lost_count int) or plain text str.
        E043: multiple AIS dark signals in Hormuz.
        E044: tanker emergency broadcast.
        E045: fleet rerouting (transformer fallback, but keyword can catch it).
        E006: generic strait disruption.
        """
        t0   = time.perf_counter()
        text = item.get("text", item) if isinstance(item, dict) else item
        tl   = text.lower()
        ais_lost = int(item.get("ais_lost_count", 0)) if isinstance(item, dict) else 0

        # Multi-AIS dark in Hormuz
        if ais_lost >= 3 or ("ais" in tl and "dark" in tl and any(s in tl for s in ["hormuz", "strait"])):
            return self._result("E043", min(0.72 + ais_lost * 0.02, 0.90), "Coast Guard/AIS", t0, text=text)

        # Tanker emergency
        tanker_emergency = ["tanker fire", "tanker collision", "tanker sinking", "oil spill emergency",
                            "vessel on fire", "ship collision", "uscg emergency"]
        if any(t in tl for t in tanker_emergency):
            return self._result("E044", 0.76, "Coast Guard/AIS", t0, text=text)

        strait_hit   = any(s in tl for s in self.STRAITS)
        incident_hit = any(s in tl for s in self.INCIDENT)
        if strait_hit and incident_hit:
            return self._result("E006", 0.80, "Coast Guard/AIS", t0, text=text)
        if strait_hit:
            return self._result("E006", 0.55, "Coast Guard/AIS", t0, text=text)
        return self._result(NO_EVENT, 0.0, "Coast Guard/AIS", t0, text=text)

    def classify_phmsa(self, text: str) -> ClassificationResult:
        t0   = time.perf_counter()
        tl   = text.lower()
        inc_score = self._score(tl, self.PIPELINE_INCIDENT)
        asset_hit = any(a in tl for a in self.PIPELINE_ASSET)
        is_emergency_order = any(t in tl for t in ["emergency order", "corrective action order", "cao ", "immediate corrective"])
        is_natgas = "natural gas" in tl or "gas transmission" in tl or "gas distribution" in tl
        if is_emergency_order and asset_hit:
            return self._result("E041", min(inc_score + 0.45, 0.88), "PHMSA Pipeline", t0, text=text)
        if inc_score > 0.20 and asset_hit:
            eid = "E042" if is_natgas else "E005"
            return self._result(eid, min(inc_score + 0.32, 0.82), "PHMSA", t0, text=text)
        return self._result(NO_EVENT, 0.0, "PHMSA", t0, text=text)

    def classify_pacer(self, text: str) -> ClassificationResult:
        t0  = time.perf_counter()
        tl  = text.lower()
        tkr = self._extract_ticker(text)
        bk  = self._score(tl, self.BANKRUPTCY)
        if bk > 0.20:
            return self._result("E016", min(bk + 0.42, 0.90), "PACER", t0, tkr, text)
        at  = self._score(tl, self.ANTITRUST)
        if at > 0.20:
            return self._result("E054", min(at + 0.38, 0.88), "PACER", t0, tkr, text)
        pt  = self._score(tl, self.PATENT)
        if pt > 0.20:
            return self._result("E055", min(pt + 0.35, 0.85), "PACER", t0, tkr, text)
        ev  = self._score(tl, self.ENVIRONMENTAL_SUIT)
        if ev > 0.20:
            return self._result("E056", min(ev + 0.32, 0.82), "PACER", t0, tkr, text)
        return self._result(NO_EVENT, 0.0, "PACER", t0, tkr, text)

    def classify_trump_x(self, text: str) -> ClassificationResult:
        t0    = time.perf_counter()
        tl    = text.lower()
        # Nation/sovereign Bitcoin reserve (E070) — highest confidence
        if self._score(tl, self.NATION_RESERVE) > 0.15:
            return self._result("E070", min(self._score(tl, self.NATION_RESERVE) + 0.55, 0.92), "X / Trump", t0, text=text)
        # SEC enforcement mention (E069)
        if self._score(tl, self.SEC_ENFORCEMENT_TERMS) > 0.15:
            return self._result("E069", min(self._score(tl, self.SEC_ENFORCEMENT_TERMS) + 0.42, 0.85), "X / Trump", t0, text=text)
        # Negative crypto (E068)
        nhits = sum(1 for t in self.CRYPTO_NEGATIVE if t in tl)
        chits = sum(1 for t in self.CRYPTO_POSITIVE if t in tl)
        if nhits >= 2 and chits >= 1:
            return self._result("E068", min(0.55 + nhits * 0.05, 0.83), "X / Trump", t0, text=text)
        # Positive crypto (E014)
        phits = sum(1 for t in self.POSITIVE_WORDS if t in tl)
        if chits >= 2 and phits >= 1:
            conf = min(0.55 + chits * 0.05, 0.84)
            return self._result("E014", conf, "X / Trump account", t0, text=text)
        if chits >= 1:
            return self._result("E014", 0.50, "X / Trump account", t0, text=text)
        return self._result(NO_EVENT, 0.0, "X / Trump account", t0, text=text)

    def classify_federal_register(self, text: str) -> ClassificationResult:
        t0 = time.perf_counter()
        tl = text.lower()
        es = self._score(tl, self.EMERGENCY_RULE)
        if es > 0.20:
            # Route to specific sub-events based on content
            if any(t in tl for t in ["epa", "oil", "gas", "drilling", "pipeline", "petroleum"]):
                return self._result("E074", min(es + 0.30, 0.84), "Federal Register", t0, text=text)
            if any(t in tl for t in ["capital requirement", "bank", "financial institution", "liquidity"]):
                return self._result("E075", min(es + 0.28, 0.82), "Federal Register", t0, text=text)
            if any(t in tl for t in ["cryptocurrency", "digital asset", "crypto", "bitcoin"]):
                return self._result("E076", min(es + 0.20, 0.74), "Federal Register", t0, text=text)
            return self._result("E020", min(es + 0.28, 0.82), "Federal Register", t0, text=text)
        return self._result(NO_EVENT, 0.0, "Federal Register", t0, text=text)

    def classify_stablecoin(self, item: dict) -> ClassificationResult:
        """
        Fields: amount_usd (float), event_type ('mint'|'burn'), window_total_usd (float, 24hr aggregate).
        """
        t0           = time.perf_counter()
        text         = item.get("text", "")
        amount       = float(item.get("amount_usd", 0))
        event_type   = item.get("event_type", "mint").lower()
        window_total = float(item.get("window_total_usd", 0))

        if window_total >= 1_000_000_000:
            conf = min(0.55 + (window_total / 2_000_000_000) * 0.10, 0.76)
            return self._result("E073", conf, "Stablecoin mint", t0, text=text)
        if amount >= 500_000_000:
            conf = min(0.50 + (amount / 1_000_000_000) * 0.08, 0.72)
            eid  = "E072" if event_type == "burn" else "E019"
            return self._result(eid, conf, "Stablecoin mint", t0, text=text)
        return self._result(NO_EVENT, 0.0, "Stablecoin mint", t0, text=text)

    def classify_faa_notam(self, text: str) -> ClassificationResult:
        t0 = time.perf_counter()
        tu = text.upper()
        tl = text.lower()
        # Ground stop at major hub (E046)
        ground_stop = ["GROUND STOP", "GROUND HOLD", "EDCT", "ALL DEPARTURES SUSPENDED",
                       "AIRPORT CLOSURE", "GROUND DELAY PROGRAM"]
        gs_hits = sum(1 for m in ground_stop if m in tu)
        if gs_hits >= 1:
            return self._result("E046", min(0.60 + gs_hits * 0.06, 0.80), "FAA NOTAM", t0, text=text)
        # Airspace closure over oil region (E047)
        oil_regions = ["middle east", "gulf", "saudi", "iraq", "iran", "kuwait", "israel", "jordan"]
        if any(r in tl for r in oil_regions) and ("airspace" in tl or "notam" in tl or "restricted" in tl):
            return self._result("E047", 0.60, "FAA NOTAM", t0, text=text)
        # Generic military TFR (E015)
        military_markers = ["RESTRICTED", "PROHIBITED", "TFR", "MILITARY OPS",
                            "EMERGENCY", "ADIZ", "PROHIBITED AREA", "RESTRICTED AREA"]
        hits = sum(1 for m in military_markers if m in tu)
        if hits >= 2:
            conf = min(0.44 + hits * 0.05, 0.66)
            return self._result("E015", conf, "FAA NOTAM", t0, text=text)
        return self._result(NO_EVENT, 0.0, "FAA NOTAM", t0, text=text)

    def classify_coinglass(self, item: dict) -> ClassificationResult:
        """
        Coinglass JSON fields:
          current_price, cluster_price, cluster_size_usd  — liquidation cluster data
          funding_rate    (float, per 8hr, e.g. 0.001 = 0.1%)
          open_interest   (float, USD)
          oi_change_1h    (float, fraction, e.g. -0.18 = -18%)
          cluster_side    ('long'|'short')
        """
        t0            = time.perf_counter()
        text          = item.get("text", "")
        current       = float(item.get("current_price", 0))
        cluster       = float(item.get("cluster_price", 0))
        cluster_size  = float(item.get("cluster_size_usd", 0))
        cluster_side  = item.get("cluster_side", "long").lower()
        funding_rate  = float(item.get("funding_rate", 0))
        oi_change_1h  = float(item.get("oi_change_1h", 0))
        MIN_CLUSTER   = 10_000_000

        # OI drop cascade (E067)
        if oi_change_1h <= -0.15:
            return self._result("E067", min(0.62 + abs(oi_change_1h) * 0.5, 0.84), "Coinglass", t0, text=text)

        # Extreme funding rates
        if funding_rate >= 0.001:
            return self._result("E065", min(0.62 + funding_rate * 20, 0.84), "Coinglass", t0, text=text)
        if funding_rate <= -0.0005:
            return self._result("E066", min(0.62 + abs(funding_rate) * 30, 0.84), "Coinglass", t0, text=text)

        # Liquidation clusters
        if current > 0 and cluster > 0 and cluster_size >= MIN_CLUSTER:
            if cluster_side == "long" and cluster > current:
                pct_away = (cluster - current) / current
                if pct_away <= 0.003:
                    size_boost = min(cluster_size / 50_000_000 * 0.04, 0.10)
                    conf       = min(0.60 + size_boost + (0.003 - pct_away) * 20, 0.82)
                    return self._result("E013", conf, "Coinglass", t0, text=text)
            elif cluster_side == "short" and cluster < current:
                pct_away = (current - cluster) / current
                if pct_away <= 0.003:
                    size_boost = min(cluster_size / 50_000_000 * 0.04, 0.10)
                    conf       = min(0.60 + size_boost + (0.003 - pct_away) * 20, 0.82)
                    return self._result("E064", conf, "Coinglass", t0, text=text)

        return self._result(NO_EVENT, 0.0, "Coinglass", t0, text=text)


    def classify_sec(self, text: str) -> ClassificationResult:
        t0  = time.perf_counter()
        tl  = text.lower()
        tkr = self._extract_ticker(text)
        if self._score(tl, self.SEC_ETF_APPROVE) > 0.15:
            return self._result("E082", min(self._score(tl, self.SEC_ETF_APPROVE) + 0.52, 0.92), "SEC Enforcement", t0, text=text)
        if self._score(tl, self.SEC_HALT) > 0.15:
            return self._result("E080", min(self._score(tl, self.SEC_HALT) + 0.58, 0.95), "SEC Enforcement", t0, tkr, text)
        if self._score(tl, self.SEC_CRYPTO_CHARGE) > 0.15:
            return self._result("E081", min(self._score(tl, self.SEC_CRYPTO_CHARGE) + 0.48, 0.90), "SEC Enforcement", t0, text=text)
        return self._result(NO_EVENT, 0.0, "SEC Enforcement", t0, text=text)

    def classify_fcc(self, text: str) -> ClassificationResult:
        t0  = time.perf_counter()
        tl  = text.lower()
        tkr = self._extract_ticker(text)
        if self._score(tl, self.FCC_FINE) > 0.20:
            return self._result("E083", min(self._score(tl, self.FCC_FINE) + 0.35, 0.84), "FCC Enforcement", t0, tkr, text)
        if self._score(tl, self.FCC_SPECTRUM) > 0.20:
            return self._result("E084", min(self._score(tl, self.FCC_SPECTRUM) + 0.28, 0.76), "FCC Enforcement", t0, text=text)
        return self._result(NO_EVENT, 0.0, "FCC Enforcement", t0, text=text)

    def classify_noaa(self, text: str) -> ClassificationResult:
        t0 = time.perf_counter()
        tl = text.lower()
        if self._score(tl, self.GEOMAGNETIC_SEVERE) > 0.15:
            return self._result("E085", min(self._score(tl, self.GEOMAGNETIC_SEVERE) + 0.32, 0.78), "NOAA Space Weather", t0, text=text)
        if self._score(tl, self.SOLAR_FLARE) > 0.15:
            return self._result("E086", min(self._score(tl, self.SOLAR_FLARE) + 0.28, 0.74), "NOAA Space Weather", t0, text=text)
        return self._result(NO_EVENT, 0.0, "NOAA Space Weather", t0, text=text)

    def classify_onchain(self, item: dict) -> ClassificationResult:
        """
        On-chain data pre-parsed JSON.
        Fields: miner_outflow_btc (float), exchange_reserve_change (float),
                sopr (float), max_pain_price (float), spot_price (float).
        """
        t0 = time.perf_counter()
        text = item.get("text", "")
        miner_outflow  = float(item.get("miner_outflow_btc", 0))
        exchange_delta = float(item.get("exchange_reserve_change", 0))  # negative = outflow
        sopr           = float(item.get("sopr", 1.0))
        max_pain       = float(item.get("max_pain_price", 0))
        spot           = float(item.get("spot_price", 0))

        if max_pain > 0 and spot > 0 and abs(max_pain - spot) / spot >= 0.10:
            return self._result("E090", 0.60, "On-chain data", t0, text=text)
        if sopr >= 3.5 or sopr <= 0.5:
            conf = min(0.55 + abs(sopr - 1.0) * 0.04, 0.78)
            return self._result("E089", conf, "On-chain data", t0, text=text)
        if exchange_delta <= -5000:
            return self._result("E088", min(0.60 + abs(exchange_delta) / 50000 * 0.08, 0.80), "On-chain data", t0, text=text)
        if miner_outflow >= 5000:
            return self._result("E087", min(0.58 + miner_outflow / 50000 * 0.08, 0.78), "On-chain data", t0, text=text)
        return self._result(NO_EVENT, 0.0, "On-chain data", t0, text=text)


# ═══════════════════════════════════════════════════════
# SECTION 4: TRAINING DATA GENERATION
#
# Each event type needs 250+ labeled examples minimum
# (FinBERT research finding). Templates here provide the
# floor; real historical RSS/API archives should supplement.
# ═══════════════════════════════════════════════════════

def generate_training_data(rng_seed: int = 42) -> tuple[list[str], list[int]]:
    """
    Returns (texts, label_indices) where each index maps to EVENT_IDS.
    Synthetic templates — expand with real historical feed data.
    """
    rng  = np.random.default_rng(rng_seed)
    data: list[tuple[str, str]] = []   # (text, event_id)

    # ── helpers ──────────────────────────────────────────────────────────────
    def add(event_id: str, templates: list[str], repeats: int = 1):
        for _ in range(repeats):
            for t in templates:
                data.append((t, event_id))

    COMPANIES  = ["NexGen Corp", "PinPoint Tech", "Sierra Pharma", "Atlas Energy",
                  "Velo Systems", "Quantum Bio", "CrestView Inc", "Apex Materials"]
    ACQUIRERS  = ["Apollo Capital", "Blackstone Partners", "Peak Holdings",
                  "Cascade Corp", "Thermo Industries", "Summit Equity"]
    DRUGS      = ["ramozumab", "cintelstat", "plerixafor", "xanosite",
                  "delbacin", "vorazetib", "lumacex"]
    CONDITIONS = ["metastatic breast cancer", "NSCLC", "Type 2 diabetes",
                  "COPD", "AML", "relapsed/refractory lymphoma"]
    TICKERS    = ["GME", "AMC", "BBBY", "SPCE", "MVIS", "CLOV", "WKHS"]
    STATES     = ["Texas", "North Dakota", "Wyoming", "Oklahoma", "Kansas"]
    REGIONS    = ["Midland Basin", "Permian", "Bakken", "Cushing hub"]
    EXCHANGES  = ["NYSE", "NASDAQ", "NYSE American"]

    # ── E001: EIA bullish draw ────────────────────────────────────────────────
    for _ in range(50):
        n = round(3.5 + rng.random() * 4, 1)
        m = round(0.8 + rng.random() * 1.5, 1)
        add("E001", [
            f"EIA reports crude oil inventories fell {n} million barrels, analysts expected draw of {m}M",
            f"Weekly petroleum status report shows drawdown of {n}M barrels versus consensus {m}M draw",
            f"Crude stocks declined by {n} million barrels, beating {m}M barrel draw estimate",
            f"EIA petroleum data: crude inventory draw {n}M bbl, survey expected -{m}M",
            f"Oil inventories post surprise draw of {n}M barrels against {m}M forecast",
        ])

    # ── E002: EIA bearish build ───────────────────────────────────────────────
    for _ in range(50):
        n = round(3.5 + rng.random() * 4, 1)
        m = round(0.8 + rng.random() * 1.5, 1)
        add("E002", [
            f"EIA reports crude oil inventories rose {n} million barrels, analysts expected build of {m}M",
            f"Weekly crude data shows unexpected build of {n}M barrels versus {m}M expected",
            f"Crude stocks increased by {n} million barrels, exceeding {m}M barrel build estimate",
            f"EIA: crude inventory build {n}M bbl, survey expected +{m}M",
            f"Oil inventories post surprise build of {n}M barrels against {m}M forecast",
        ])

    # ── E003: Dovish Fed ──────────────────────────────────────────────────────
    add("E003", [
        "Federal Reserve cuts rates by 25 basis points citing slowing economic growth",
        "FOMC votes to reduce the target range for the federal funds rate to support economic activity",
        "Fed signals patient approach, rate cuts possible if economic conditions deteriorate",
        "Federal Reserve statement: committee has decided to lower the target range",
        "FOMC: downside risks to outlook warrant accommodative monetary policy stance",
        "Fed chair signals potential pivot, says higher rates no longer appropriate",
        "Federal Reserve announces surprise rate cut amid financial market turmoil",
        "FOMC minutes reveal members discussed reducing rates at several upcoming meetings",
        "Fed signals pause in rate hikes, language shifts to monitoring incoming data",
        "Federal Reserve statement removes additional firming language, seen as dovish pivot",
        "FOMC: inflation has eased considerably, labor market has cooled from overheated levels",
        "Fed chair: policy may be overly restrictive, willing to adjust as appropriate",
    ], repeats=21)

    # ── E004: Hawkish Fed ─────────────────────────────────────────────────────
    add("E004", [
        "Federal Reserve raises rates by 50 basis points to combat persistent inflation",
        "FOMC votes to increase the target range for the federal funds rate",
        "Fed signals further increases appropriate, inflation remains above target",
        "Federal Reserve statement: ongoing increases in the target range will be appropriate",
        "FOMC: upside inflation risks warrant restrictive monetary policy stance",
        "Fed chair says higher for longer stance necessary to restore price stability",
        "Federal Reserve delivers surprise 75 basis point rate hike",
        "FOMC minutes reveal members see need for additional tightening",
        "Fed signals rates to remain restrictive, no cuts expected this year",
        "Federal Reserve statement adds language about additional increases being appropriate",
        "FOMC: inflationary pressures remain elevated, tightening must continue",
        "Fed chair: we are committed to returning inflation to 2 percent target",
    ], repeats=21)

    # ── E005: PHMSA pipeline incident ─────────────────────────────────────────
    for _ in range(25):
        st = rng.choice(STATES)
        rg = rng.choice(REGIONS)
        n  = int(rng.integers(30, 90))
        add("E005", [
            f"Pipeline rupture reported in {st}, crude oil spill affecting supply",
            f"Natural gas pipeline explosion near {rg}, emergency shutdown declared",
            f"PHMSA incident report: pipeline fire, hazmat response deployed in {st}",
            f"Major pipeline leak discovered in {rg}, operator declared force majeure",
            f"Pipeline transmission line failure in {st}, supply disruption expected",
            f"Crude oil pipeline burst detected in {rg}, flow reduced {n}% pending repair",
        ])

    # ── E006: Strait / canal disruption ──────────────────────────────────────
    add("E006", [
        "Houthi forces attack tanker in Strait of Hormuz, shipping diverted",
        "Iran announces naval exercise in Persian Gulf, Hormuz transit disrupted",
        "Vessel runs aground in Suez Canal, blocking transit for southbound traffic",
        "Coast Guard broadcast: Strait of Hormuz closed to commercial shipping",
        "Military incident near Suez Canal, Egyptian authorities halt transit",
        "Oil tanker seized near Strait of Hormuz by naval forces",
        "Shipping disruption: vessels avoiding Suez Canal following attack on tanker",
        "Bab-el-Mandeb Strait closure announced, Red Sea shipping rerouted",
        "AIS anomaly: tanker fleet rerouting around Cape of Good Hope, Hormuz risk",
        "Suez Canal Authority announces temporary closure following vessel collision",
        "USCG maritime safety broadcast: strait disrupted, transit halted",
        "Persian Gulf incident: tanker seized, crude shipping lanes blocked",
    ], repeats=21)

    # ── E007: M&A announcement ────────────────────────────────────────────────
    for _ in range(25):
        co  = rng.choice(COMPANIES)
        acq = rng.choice(ACQUIRERS)
        n   = int(rng.integers(12, 80))
        add("E007", [
            f"{co} announces definitive merger agreement with {acq}",
            f"{acq} to acquire {co} in all-cash deal at ${n} per share",
            f"{co} enters into merger agreement, board unanimously approved",
            f"8-K: {co} signs definitive agreement for strategic combination with {acq}",
            f"{co} announces going-private transaction, tender offer at ${n}",
            f"{co} board approves sale of company to {acq} for ${n} per share",
        ])

    # ── E008: Going concern / restatement ────────────────────────────────────
    for _ in range(25):
        co = rng.choice(COMPANIES)
        ex = rng.choice(EXCHANGES)
        add("E008", [
            f"8-K: {co} announces going concern doubt, auditor qualification issued",
            f"{co} files 8-K disclosing material weakness in internal controls",
            f"8-K: {co} to restate financial statements for fiscal years 2022-2023",
            f"{co} discloses material accounting error, SEC investigation launched",
            f"8-K: {co} receives delisting notice from {ex}",
            f"{co} 8-K: substantial doubt about ability to continue as going concern",
        ])

    # ── E009: FDA approval ────────────────────────────────────────────────────
    for _ in range(25):
        co   = rng.choice(COMPANIES)
        drug = rng.choice(DRUGS)
        cond = rng.choice(CONDITIONS)
        n, m = int(rng.integers(8, 14)), int(rng.integers(1, 5))
        add("E009", [
            f"FDA approves {co}'s {drug} for treatment of {cond}",
            f"FDA grants approval to {co} for {drug}, stock surges premarket",
            f"{co} receives FDA approval for {drug} NDA, commercialization begins",
            f"FDA advisory committee votes {n}-{m} to recommend approval of {drug}",
            f"{co}'s {drug} receives FDA breakthrough therapy designation approval",
            f"FDA issues complete approval for {co}'s {drug} following priority review",
        ])

    # ── E010: FDA rejection / recall ──────────────────────────────────────────
    for _ in range(25):
        co   = rng.choice(COMPANIES)
        drug = rng.choice(DRUGS)
        cond = rng.choice(CONDITIONS)
        n, m = int(rng.integers(1, 5)), int(rng.integers(8, 14))
        add("E010", [
            f"FDA issues complete response letter to {co} for {drug} NDA",
            f"{co} receives FDA rejection for {drug}, shares plunge premarket",
            f"FDA warning letter issued to {co} regarding {drug} manufacturing",
            f"FDA places clinical hold on {co}'s {drug} trial following adverse events",
            f"{co} announces voluntary recall of {drug} following FDA safety alert",
            f"FDA advisory committee votes {n}-{m} against recommending approval of {drug}",
        ])

    # ── E011: Whale to exchange ───────────────────────────────────────────────
    for _ in range(50):
        n = int(rng.integers(50, 500))
        add("E011", [
            f"Whale alert: {n}M USDT transferred to Binance from unknown wallet",
            f"Large BTC transfer detected: {n}M to exchange, potential sell pressure",
            f"On-chain: {n} million USD in bitcoin moved to Coinbase hot wallet",
            f"Whale moves {n}M to exchange address, market watching for large sell",
            f"Alert: large inflow to exchange detected, {n}M equivalent BTC",
        ])

    # ── E012: Whale from exchange ─────────────────────────────────────────────
    for _ in range(50):
        n = int(rng.integers(50, 500))
        add("E012", [
            f"Whale alert: {n}M BTC withdrawn from Binance to cold storage wallet",
            f"Large BTC withdrawal from exchange: {n}M moved to unknown address",
            f"On-chain: {n} million USD in bitcoin withdrawn from Coinbase to hardware wallet",
            f"Whale moves {n}M from exchange to cold storage, accumulation signal",
            f"Alert: large outflow from exchange detected, {n}M equivalent BTC withdrawn",
        ])

    # ── E013: Coinglass liquidation cluster ───────────────────────────────────
    for _ in range(50):
        n = int(rng.integers(20, 200))
        add("E013", [
            f"Coinglass: BTC price approaching ${n}K liquidation cluster, {n*10}M longs above",
            f"Liquidation heatmap: large long cluster detected just above current price",
            f"BTC within 0.2% of ${n}K level where ${n*10}M in long positions will liquidate",
            f"Coinglass alert: price magnet detected at ${n}K, large liquidation cluster",
            f"Liquidation cascade zone identified above market, ${n*8}M at risk",
        ])

    # ── E014: Trump / X crypto post ───────────────────────────────────────────
    add("E014", [
        "Strategic Bitcoin reserve is a great idea, we are fully backing it",
        "America will be the crypto capital of the world, bitcoin is the future",
        "I love bitcoin and crypto, we will make america great with digital assets",
        "Strategic crypto reserve approved, bitcoin and digital assets will thrive",
        "Digital currency is the future, the United States is fully supporting bitcoin",
        "Bitcoin is amazing, we will hold it in our strategic reserve",
        "Crypto deregulation coming, america will lead the world in digital assets",
        "Love what bitcoin is doing, great asset for our strategic reserve",
    ], repeats=31)

    # ── E015: FAA military NOTAM ──────────────────────────────────────────────
    add("E015", [
        "FAA NOTAM: TFR established over restricted area, military operations active",
        "Emergency temporary flight restriction, military ADIZ activated over region",
        "FAA issues prohibited area designation, military operations in progress",
        "NOTAM: restricted airspace emergency military activity, effective immediately",
        "FAA temporary flight restriction declared, military ops in progress below",
        "Air defense identification zone activated, TFR in effect for military exercise",
    ], repeats=42)

    # ── E016: Bankruptcy ──────────────────────────────────────────────────────
    for _ in range(25):
        co = rng.choice(COMPANIES)
        n  = int(rng.integers(50, 2000))
        add("E016", [
            f"{co} files for Chapter 11 bankruptcy protection in Delaware",
            f"{co} voluntarily files for bankruptcy, lists ${n}M in liabilities",
            f"Chapter 11 petition filed by {co}, reorganization plan to follow",
            f"{co} seeks bankruptcy protection, equity likely worthless",
            f"PACER: {co} chapter 11 voluntary petition filed",
            f"{co} enters Chapter 11 with ${n}M in debt, operations continue",
        ])

    # ── E017: GDELT conflict ──────────────────────────────────────────────────
    add("E017", [
        "GDELT conflict score spikes for Iraq, oil export route threatened",
        "Military clashes near Basra, Iraqi oil infrastructure at risk",
        "Iran-Saudi tensions escalate, Persian Gulf oil transit at risk",
        "Conflict escalation in Libya, oil field production disrupted by fighting",
        "Nigeria militant activity near oil-producing Delta region increasing",
        "Armed conflict near Kirkuk disrupts Kurdish oil exports via Turkey",
        "Yemen conflict spreads toward oil terminals, Aden port at risk",
        "GDELT: conflict event density rising in oil-producing MENA region",
        "Sudan fighting near oil pipeline infrastructure threatens production",
        "Geopolitical tension index elevated for oil-producing region",
    ], repeats=25)

    # ── E018: Reddit meme velocity ────────────────────────────────────────────
    for _ in range(25):
        tk = rng.choice(TICKERS)
        a  = int(rng.integers(5, 15))
        b  = int(rng.integers(200, 800))
        n  = int(rng.integers(20, 60))
        add("E018", [
            f"WSB: {tk} mention count surged from {a} to {b} in last 5 minutes, short squeeze",
            f"APEwisdom spike: {tk} from {a} to {b} mentions in 5-minute window",
            f"Reddit velocity alert: {tk} mentions {b}x increase, momentum trade setup",
            f"{tk} trending on r/wallstreetbets, {n}% short float, squeeze incoming",
            f"Social signal: {tk} mention velocity {b} in 5-min window vs baseline {a}",
        ])

    # ── E019: Stablecoin mint ─────────────────────────────────────────────────
    for _ in range(50):
        n = int(rng.integers(500, 3000))
        add("E019", [
            f"Whale alert: Tether treasury minted {n}M USDT, capital inflow signal",
            f"USDT mint event detected: {n} million new tokens issued on Ethereum",
            f"Stablecoin alert: {n}M USDC minted from treasury, bullish for crypto",
            f"On-chain: ${n}M USDT minted in single transaction from Tether treasury",
            f"Circle mints {n}M USDC, large capital entering crypto ecosystem",
        ])

    # ── E020: Federal Register emergency rule ─────────────────────────────────
    add("E020", [
        "Federal Register: interim final rule affecting energy sector, effective immediately",
        "Emergency rule published without prior notice, affects pharmaceutical manufacturers",
        "Federal Register: final rule effective immediately upon publication today",
        "Agency issues emergency order affecting banking sector under good cause exemption",
        "Federal Register interim final rule: telecom spectrum allocation changed immediately",
        "Emergency regulatory action: crude oil export controls modified by executive order",
        "Federal Register: temporary rule affecting semiconductor imports, effective today",
        "Without prior public notice: agency emergency rule affecting financial sector",
        "Federal Register: interim rule affecting agricultural commodity trading, immediate",
        "Emergency rulemaking: chemical plant safety standards modified, immediate effect",
    ], repeats=25)

    # ── NO_EVENT: noise floor (~25% of dataset) ───────────────────────────────
    add(NO_EVENT, [
        "Markets closed for holiday, no trading activity expected today",
        "Scheduled maintenance window for exchange systems tonight",
        "EIA postpones weekly petroleum status report to Thursday due to holiday",
        "Federal Reserve publishes academic research paper on inflation dynamics",
        "SEC staff issues guidance on fund registration procedures",
        "USDA publishes crop progress report, conditions normal for season",
        "Coast Guard issues routine notice to mariners for buoy maintenance",
        "FAA issues routine NOTAM for airshow activity near regional airport",
        "Pipeline operator files routine quarterly report with PHMSA",
        "FDA publishes draft guidance for industry comment period",
        "Federal court denies routine motion in ongoing civil litigation",
        "Reddit: general market discussion, no specific ticker mentioned",
        "Whale alert: routine transfer between two known institutional cold wallets",
        "GDELT: routine political event, low conflict score in stable region",
        "Federal Register: proposed rule open for public comment, 60-day period",
        "Stablecoin transfer between treasury wallets, routine rebalancing",
        "X post: political opinion completely unrelated to financial markets",
        "EIA publishes monthly short-term energy outlook, no major revisions",
        "Fed publishes Beige Book, conditions described as modest and gradual growth",
        "SEC releases annual report on enforcement activities, statistics only",
        "PACER: routine scheduling order in civil case",
        "FAA NOTAM: runway closure at regional airport for maintenance",
        "PHMSA: routine safety inspection report, no incidents noted",
        "Federal Register: administrative notice, no regulatory change",
    ], repeats=26)

    texts  = [t for t, _ in data]
    labels = [EVENT_ID_TO_IDX[eid] for _, eid in data]
    
    # Shuffle
    idx = rng.permutation(len(texts))
    return [texts[i] for i in idx], [labels[i] for i in idx]


# ═══════════════════════════════════════════════════════
# SECTION 5: TRANSFORMER TRAINING PIPELINE
# ═══════════════════════════════════════════════════════

if TORCH_AVAILABLE:
    class APEXDataset(Dataset):
        def __init__(self, texts: list[str], labels: list[int],
                     tokenizer, max_length: int = 128):
            self.encodings = tokenizer(texts, truncation=True, padding=True,
                                       max_length=max_length, return_tensors="pt")
            self.labels = torch.tensor(labels)

        def __len__(self):
            return len(self.labels)

        def __getitem__(self, idx):
            return {
                "input_ids":      self.encodings["input_ids"][idx],
                "attention_mask": self.encodings["attention_mask"][idx],
                "labels":         self.labels[idx],
            }


if TORCH_AVAILABLE:
    def train_classifier(
        output_dir:  str = "./apex_model",
        base_model:  str = "distilbert-base-uncased",
        epochs:      int = 3,
        batch_size:  int = 16,
        max_length:  int = 128,
    ) -> dict:
        """
        Fine-tune DistilBERT on APEX event taxonomy.

        Model rationale (from Signal Trading research PDF):
          - DistilBERT: 40% smaller / 60% faster than BERT, 97% retained performance
          - Achieves 93.2% on SEntFiN; ~80% accuracy with 250 training examples
          - INT8 quantization brings CPU inference to <10ms per item
          - FinBERT alternative achieves 97% on Financial PhraseBank but is larger;
            DistilBERT is preferred for latency requirements.

        After training, supplement with real historical RSS archives per source.
        Target: 85%+ accuracy on labeled validation set.
        """
        log = logging.getLogger("APEXTrainer")
        log.info("Generating training data...")

        texts, labels = generate_training_data()
        log.info(f"Total examples: {len(texts)}")

        dist = Counter(labels)
        for idx in sorted(dist):
            log.info(f"  {IDX_TO_EVENT_ID[idx]}: {dist[idx]} examples")

        # 80/20 split
        rng   = np.random.default_rng(99)
        idx   = rng.permutation(len(texts))
        split = int(len(texts) * 0.8)
        train_t = [texts[i] for i in idx[:split]]
        train_l = [labels[i] for i in idx[:split]]
        val_t   = [texts[i] for i in idx[split:]]
        val_l   = [labels[i] for i in idx[split:]]

        log.info("Loading DistilBERT tokenizer and model...")
        tokenizer = DistilBertTokenizerFast.from_pretrained(base_model)
        model     = DistilBertForSequenceClassification.from_pretrained(
            base_model,
            num_labels=NUM_LABELS,
            id2label=IDX_TO_EVENT_ID,
            label2id=EVENT_ID_TO_IDX,
        )

        train_ds = APEXDataset(train_t, train_l, tokenizer, max_length)
        val_ds   = APEXDataset(val_t,   val_l,   tokenizer, max_length)

        def compute_metrics(eval_pred):
            from sklearn.metrics import f1_score, accuracy_score
            logits, labs = eval_pred
            preds = np.argmax(logits, axis=1)
            return {
                "accuracy":    accuracy_score(labs, preds),
                "f1_weighted": f1_score(labs, preds, average="weighted", zero_division=0),
            }

        args = TrainingArguments(
            output_dir=output_dir,
            num_train_epochs=epochs,
            per_device_train_batch_size=batch_size,
            per_device_eval_batch_size=batch_size,
            warmup_steps=100,
            weight_decay=0.01,
            eval_strategy="epoch",
            save_strategy="best",
            load_best_model_at_end=True,
            metric_for_best_model="f1_weighted",
            logging_steps=50,
            fp16=torch.cuda.is_available(),
            dataloader_num_workers=0,
            report_to="none",
        )

        trainer = Trainer(
            model=model,
            args=args,
            train_dataset=train_ds,
            eval_dataset=val_ds,
            compute_metrics=compute_metrics,
        )

        log.info("Training...")
        trainer.train()
        trainer.save_model(output_dir)
        tokenizer.save_pretrained(output_dir)

        results = trainer.evaluate()
        log.info(f"Eval results: {results}")

        # INT8 quantization for <10ms CPU inference
        try:
            quantized = torch.quantization.quantize_dynamic(
                model, {torch.nn.Linear}, dtype=torch.qint8
            )
            torch.save(quantized.state_dict(), f"{output_dir}/quantized_weights.pt")
            log.info("INT8 quantized weights saved — use for production inference")
        except Exception as e:
            log.warning(f"Quantization skipped: {e}")

        return results


# ═══════════════════════════════════════════════════════
# SECTION 6: TRANSFORMER INFERENCE CLASS
# ═══════════════════════════════════════════════════════

if TORCH_AVAILABLE:
    class TransformerClassifier:
        """
        Runs fine-tuned DistilBERT inference. Loaded once at startup.
        Temperature scaling (T=1.5) corrects typical transformer overconfidence.
        """
        TEMPERATURE = 1.5   # Calibration constant; tune against real outcomes

        def __init__(self, model_path: str = "./apex_model"):
            log = logging.getLogger("TransformerClassifier")
            self.tokenizer = AutoTokenizer.from_pretrained(model_path)
            self.model     = AutoModelForSequenceClassification.from_pretrained(model_path)
            self.model.eval()
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            self.model.to(self.device)
            log.info(f"Transformer loaded on {self.device}")

        def classify(self, text: str, source: str = "") -> ClassificationResult:
            t0     = time.perf_counter()
            inputs = self.tokenizer(
                text, return_tensors="pt", truncation=True,
                padding=True, max_length=128
            ).to(self.device)

            with torch.no_grad():
                logits = self.model(**inputs).logits

            # Apply temperature scaling before softmax
            scaled = logits / self.TEMPERATURE
            probs  = torch.softmax(scaled, dim=-1)[0].cpu().numpy()
            top    = int(np.argmax(probs))
            conf   = float(probs[top])
            eid    = IDX_TO_EVENT_ID[top]

            latency = (time.perf_counter() - t0) * 1000
            return ClassificationResult(eid, conf, source, "transformer", latency, raw_text=text)


# ═══════════════════════════════════════════════════════
# SECTION 7: MAIN HYBRID CLASSIFIER
#
# Routing logic:
#   1. Always run keyword classifier first (<1ms)
#   2. If keyword confidence ≥ event threshold → return immediately
#   3. For unstructured sources that fail keyword → try transformer
#   4. Return highest-confidence result
#
# This guarantees structured sources (EIA, Fed, EDGAR, FDA, Whale)
# NEVER touch the transformer — consistent with the FinMTEB finding.
# ═══════════════════════════════════════════════════════

# Sources that map to a specific keyword method
SOURCE_METHOD_MAP = {
    # EIA
    "EIA Petroleum":        "eia",
    "EIA Petroleum Report": "eia",
    "EIA Natural Gas":      "eia",
    # Fed / central banks
    "Fed RSS":              "fed",
    "ECB RSS":              "ecb",
    "ECB Feed":             "ecb",
    # Macro
    "BLS":                  "bls",
    "BLS Feed":             "bls",
    "BLS CPI":              "bls",
    "BLS NFP":              "bls",
    # OPEC
    "OPEC RSS":             "opec",
    "OPEC Feed":            "opec",
    # FDA
    "FDA MedWatch":         "fda",
    # EDGAR
    "EDGAR 8-K":            "edgar",
    "EDGAR Full-Text Feed": "edgar",
    "EDGAR Filings":        "edgar",
    # Whale Alert
    "Whale Alert":          "whale",
    "Whale Alert On-Chain": "whale",
    # Coast Guard / AIS
    "Coast Guard/AIS":      "coast_guard",
    "Coast Guard Maritime": "coast_guard",
    # PHMSA
    "PHMSA":                "phmsa",
    "PHMSA Pipeline Alerts":"phmsa",
    # PACER
    "PACER":                "pacer",
    "PACER Federal Courts": "pacer",
    # X / Trump
    "X / Trump account":    "trump_x",
    "X Key Accounts":       "trump_x",
    "X / Trump":            "trump_x",
    # Federal Register
    "Federal Register":     "federal_register",
    # Stablecoin
    "Stablecoin mint":      "stablecoin",
    "Stablecoin Mints":     "stablecoin",
    # FAA
    "FAA NOTAM":            "faa",
    "FAA NOTAM System":     "faa",
    # Coinglass
    "Coinglass":            "coinglass",
    "Coinglass Liquidations":"coinglass",
    # SEC
    "SEC Enforcement":      "sec",
    "SEC Feed":             "sec",
    # FCC
    "FCC Enforcement":      "fcc",
    "FCC Feed":             "fcc",
    # NOAA
    "NOAA Space Weather":   "noaa",
    "NOAA SWPC":            "noaa",
    # On-chain
    "On-chain data":        "onchain",
    "Glassnode":            "onchain",
    "CryptoQuant":          "onchain",
    "Blockchain.com":       "onchain",
    "CoinGecko":            "onchain",
    # Exchange funding rates → coinglass classifier (handles E065/E066/E067)
    "Kraken Funding":       "coinglass",   # replaces Binance+Bybit (geo-blocked on Railway)
    "OKX Funding":          "coinglass",
    # Live prices — pass through as onchain (price monitoring)
    "Kraken Price":         "onchain",
    # Physical world / weather
    "USGS Earthquake":      "noaa",
    "NWS Weather":          "noaa",
    "NHC Tropical":         "noaa",
    # Government / macro
    "Treasury Auction":     "fed",
    "CFTC COT":             "fed",
    "FRED":                 "fed",
    "FERC Energy":          "eia",
    "White House":          "federal_register",
    "FedReg PrePub":        "federal_register",
    "BoE":                  "ecb",
    # SEC insider
    "SEC Form 4":           "edgar",
    # Prediction markets
    "Polymarket":           "fed",
    # Infrastructure / grid
    "MISO Grid":            "eia",
    "NRC Reactor":          "noaa",
    "FAA NAS":              "faa",
    "CBP Border":           "fed",
    # Global events / alternative data
    "GDELT":                "fed",
    "WHO Outbreak":         "noaa",
    "FINRA ATS":            "edgar",
    # DeFi / crypto
    "dYdX":                 "coinglass",
    "Mempool":              "onchain",
    # Legislative / intelligence
    "Congress":             "federal_register",
    "NASA FIRMS":           "noaa",
    "GIE AGSI":             "eia",
    "OpenSky":              "noaa",
    "ERCOT":                "eia",
    "CAISO":                "eia",
    "EIA NatGas":           "eia",
    "OFAC Sanctions":       "fed",
    "BOJ":                  "ecb",
    "SNB":                  "ecb",
    "BoC":                  "ecb",
    "DOJ Antitrust":        "sec",
    "FTC":                  "sec",
    "CBOE P/C":             "fed",
    "DeFi Llama":           "onchain",
    "Hyperliquid":          "coinglass",
    "Wiki Pageviews":       "fed",
    "NOAA Ports":           "noaa",
    "Eurostat":             "ecb",
    "USGS Water":           "noaa",
    "USDA LMPR":            "eia",
    "FINRA ATS":            "edgar",
}

# Sources where keyword will often fail — send to transformer
TRANSFORMER_ELIGIBLE = {
    "GDELT", "GDELT Event Stream",
    "Reddit velocity", "Reddit WSB/Crypto",
    "Coast Guard/AIS",  # E045 rerouting detection
    "OPEC RSS",         # E040 quota threats
    "Federal Register", # E074-E076 transformer fallback
}


class APEXClassifier:
    """
    Production classifier. Thread-safe for asyncio ingestion loop.
    """

    def __init__(self, model_path: Optional[str] = "./apex_model"):
        self._kw  = KeywordClassifier()
        self._tr: Optional[TransformerClassifier] = None
        self._log = logging.getLogger("APEXClassifier")

        if model_path and os.path.exists(model_path):
            try:
                self._tr = TransformerClassifier(model_path)
                self._log.info("Transformer classifier ready")
            except Exception as e:
                self._log.warning(f"Transformer load failed ({e}). Keyword-only mode.")
        else:
            self._log.info("No trained model found. Keyword-only mode.")

    def classify(self, item: dict) -> ClassificationResult:
        """
        Main entry point.

        item dict keys:
          source  (str)       — matches SOURCES tab SOURCE NAME
          text    (str)       — raw text from feed
          + source-specific fields for structured sources:
            EIA:         actual_mmb, consensus_mmb  (float)
            Whale Alert: amount_usd, from_owner_type, to_owner_type  (str/float)
            Coinglass:   current_price, cluster_price, cluster_size_usd (float)
            Stablecoin:  amount_usd  (float)
        """
        source = item.get("source", "")
        text   = item.get("text", "")

        # ── Fast path: keyword ──────────────────────────────────────────────
        kw_result = self._dispatch_keyword(source, item, text)

        # If keyword is confident enough → return without touching transformer
        if kw_result.event_id != NO_EVENT and kw_result.is_tradeable():
            return kw_result

        # ── Slow path: transformer for unstructured sources ─────────────────
        if (source in TRANSFORMER_ELIGIBLE
                and self._tr is not None
                and text.strip()):
            tr_result = self._tr.classify(text, source)
            if tr_result.confidence > kw_result.confidence:
                return tr_result

        return kw_result

    def _dispatch_keyword(self, source: str, item: dict, text: str) -> ClassificationResult:
        method = SOURCE_METHOD_MAP.get(source)
        kw     = self._kw
        try:
            if method == "eia":            return kw.classify_eia(item)
            if method == "fed":            return kw.classify_fed(text)
            if method == "fda":            return kw.classify_fda(text)
            if method == "edgar":          return kw.classify_edgar(text)
            if method == "whale":          return kw.classify_whale_alert(item)
            if method == "phmsa":          return kw.classify_phmsa(text)
            if method == "pacer":          return kw.classify_pacer(text)
            if method == "trump_x":        return kw.classify_trump_x(text)
            if method == "federal_register": return kw.classify_federal_register(text)
            if method == "stablecoin":     return kw.classify_stablecoin(item)
            if method == "faa":            return kw.classify_faa_notam(text)
            if method == "coinglass":      return kw.classify_coinglass(item)
            if method == "ecb":            return kw.classify_ecb(text)
            if method == "bls":            return kw.classify_bls(text)
            if method == "opec":           return kw.classify_opec(text)
            if method == "sec":            return kw.classify_sec(text)
            if method == "fcc":            return kw.classify_fcc(text)
            if method == "noaa":           return kw.classify_noaa(text)
            if method == "onchain":        return kw.classify_onchain(item)
            if method == "coast_guard":    return kw.classify_coast_guard(item)
        except Exception as e:
            self._log.error(f"Keyword error source={source}: {e}")
        return ClassificationResult(NO_EVENT, 0.0, source, "keyword", 0.0, raw_text=text)


# ═══════════════════════════════════════════════════════
# SECTION 8: FEEDBACK LOOP
# Tracks precision per event type; recalibrates thresholds.
# Phase 7 of the DASHBOARD build log.
# ═══════════════════════════════════════════════════════

class FeedbackTracker:

    def __init__(self, db_path: str = "./apex_feedback.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init()

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trade_outcomes (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id          TEXT    NOT NULL,
                confidence        REAL    NOT NULL,
                path              TEXT,
                ticker            TEXT,
                asset             TEXT,
                entry_price       REAL,
                exit_price        REAL,
                pnl_ticks         REAL,
                correct_direction INTEGER,   -- 1=correct 0=wrong NULL=pending
                timestamp         DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS threshold_overrides (
                event_id          TEXT PRIMARY KEY,
                threshold         REAL    NOT NULL,
                precision_30d     REAL,
                sample_count      INTEGER,
                updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.conn.commit()

    def log_entry(self, result: ClassificationResult, asset: str, entry_price: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO trade_outcomes (event_id,confidence,path,ticker,asset,entry_price)"
            " VALUES (?,?,?,?,?,?)",
            (result.event_id, result.confidence, result.path,
             result.ticker, asset, entry_price)
        )
        self.conn.commit()
        return cur.lastrowid

    def log_exit(self, trade_id: int, exit_price: float,
                 pnl_ticks: float, correct: int):
        self.conn.execute(
            "UPDATE trade_outcomes SET exit_price=?,pnl_ticks=?,correct_direction=? WHERE id=?",
            (exit_price, pnl_ticks, correct, trade_id)
        )
        self.conn.commit()

    def precision_report(self, days: int = 30) -> dict:
        rows = self.conn.execute("""
            SELECT event_id,
                   COUNT(*)          AS total,
                   SUM(correct_direction) AS correct,
                   AVG(pnl_ticks)    AS avg_pnl
            FROM trade_outcomes
            WHERE correct_direction IS NOT NULL
              AND timestamp >= datetime('now', ?)
            GROUP BY event_id
        """, (f"-{days} days",)).fetchall()

        return {
            eid: {
                "precision": correct / total if total else 0,
                "n":         total,
                "avg_pnl":   avg_pnl,
            }
            for eid, total, correct, avg_pnl in rows
        }

    def recalibrate(self, min_samples: int = 10, retire_below: float = 0.50):
        """
        Raise threshold for underperformers; lower for high performers.
        Retire (mark inactive) archetypes below retire_below precision.
        """
        report = self.precision_report()
        for eid, stats in report.items():
            if stats["n"] < min_samples:
                continue
            prec = stats["precision"]
            base = TAXONOMY_MAP[eid].confidence_threshold if eid in TAXONOMY_MAP else 0.70

            if prec < retire_below:
                new_t  = min(base + 0.05, 0.95)
                status = f"↑ threshold (precision {prec:.0%} < {retire_below:.0%})"
            elif prec > 0.75 and stats["n"] >= 30:
                new_t  = max(base - 0.03, 0.50)
                status = f"↓ threshold (precision {prec:.0%}, n={stats['n']})"
            else:
                continue

            self.conn.execute(
                "INSERT OR REPLACE INTO threshold_overrides"
                " (event_id,threshold,precision_30d,sample_count) VALUES (?,?,?,?)",
                (eid, new_t, prec, stats["n"])
            )
            print(f"  {eid}: {base:.2f} → {new_t:.2f}  {status}")

        self.conn.commit()


# ═══════════════════════════════════════════════════════
# SECTION 9: CLI — TRAIN + SMOKE TEST
# ═══════════════════════════════════════════════════════

def smoke_test(model_path: Optional[str] = None):
    """Run a set of representative items through the hybrid classifier."""
    clf = APEXClassifier(model_path=model_path)

    test_items = [
        # ── Structured fast-path cases ─────────────────────────────────────
        {"source": "EIA Petroleum", "text": "EIA weekly petroleum report",
         "actual_mmb": -5.2, "consensus_mmb": -1.8},
        {"source": "EIA Petroleum", "text": "EIA weekly petroleum report",
         "actual_mmb": 4.1, "consensus_mmb": 0.9},
        {"source": "Fed RSS",
         "text": "Federal Reserve votes to reduce the target range for the federal funds "
                 "rate to support economic activity and address downside risks"},
        {"source": "Fed RSS",
         "text": "FOMC: ongoing increases in the target range will be appropriate, "
                 "inflation remains elevated, higher for longer"},
        {"source": "FDA MedWatch",
         "text": "FDA approves Sierra Pharma (SRPH) ramozumab for treatment of NSCLC"},
        {"source": "FDA MedWatch",
         "text": "FDA issues complete response letter to NexGen Corp (NXGN) for xanosite NDA"},
        {"source": "EDGAR 8-K",
         "text": "8-K: NexGen Corp announces definitive merger agreement with Apollo Capital"},
        {"source": "EDGAR 8-K",
         "text": "8-K: Atlas Energy — substantial doubt about ability to continue as going concern"},
        {"source": "Whale Alert",
         "text": "Transfer", "amount_usd": 95_000_000,
         "from_owner_type": "unknown", "to_owner_type": "exchange"},
        {"source": "Whale Alert",
         "text": "Transfer", "amount_usd": 72_000_000,
         "from_owner_type": "exchange", "to_owner_type": "unknown"},
        {"source": "PACER Federal Courts",
         "text": "Atlas Energy (ATLS) files voluntary petition for Chapter 11 "
                 "bankruptcy protection in Southern District of New York"},
        {"source": "Stablecoin Mints",
         "text": "Tether minted 800M USDT", "amount_usd": 800_000_000},
        {"source": "X Key Accounts",
         "text": "Strategic Bitcoin reserve is a great idea, america will be the crypto capital"},
        {"source": "Coast Guard/AIS",
         "text": "USCG maritime broadcast: tanker seized near Strait of Hormuz by naval forces, "
                 "shipping diverted, transit blocked"},
        {"source": "PHMSA Pipeline Alerts",
         "text": "Major crude oil pipeline rupture reported in Permian Basin Texas, "
                 "emergency shutdown declared, hazmat response deployed"},
        # ── Unstructured → transformer ─────────────────────────────────────
        {"source": "GDELT Event Stream",
         "text": "Armed conflict escalates near Basra oil fields, Iranian forces "
                 "mobilizing toward Persian Gulf, crude supply routes at risk"},
        {"source": "Reddit WSB/Crypto",
         "text": "GME mention count surged from 8 to 450 in last 5 minutes — short squeeze!"},
        # ── No-event noise ─────────────────────────────────────────────────
        {"source": "EIA Petroleum", "text": "EIA routine report",
         "actual_mmb": -0.8, "consensus_mmb": -0.5},   # Small delta, below threshold
        {"source": "Fed RSS",
         "text": "Federal Reserve publishes Beige Book, modest and gradual growth reported"},
    ]

    print(f"\n{'SOURCE':<28} {'EVENT':<14} {'CONF':>6} {'PATH':<13} {'MS':>7}  TRADEABLE")
    print("─" * 82)
    for item in test_items:
        r = clf.classify(item)
        t = "✓" if r.is_tradeable() else "✗"
        tk = f"[{r.ticker}]" if r.ticker else ""
        print(f"{item['source']:<28} {r.event_id + tk:<14} "
              f"{r.confidence:>6.3f} {r.path:<13} {r.latency_ms:>6.2f}ms  {t}")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="APEX Classifier — train or test")
    p.add_argument("--train",     action="store_true", help="Fine-tune DistilBERT")
    p.add_argument("--test",      action="store_true", help="Run smoke test")
    p.add_argument("--model-dir", default="./apex_model")
    p.add_argument("--epochs",    type=int, default=3)
    p.add_argument("--batch",     type=int, default=16)
    args = p.parse_args()

    if args.train:
        print("Training APEX DistilBERT classifier...")
        results = train_classifier(
            output_dir=args.model_dir,
            epochs=args.epochs,
            batch_size=args.batch,
        )
        print(f"\nTraining complete: {results}")

    if args.test:
        model = args.model_dir if (args.train and os.path.exists(args.model_dir)) else None
        smoke_test(model_path=model)
    
    if not args.train and not args.test:
        print("Usage: python apex_classifier.py --train --test")
        print("       python apex_classifier.py --test   (keyword-only mode)")
