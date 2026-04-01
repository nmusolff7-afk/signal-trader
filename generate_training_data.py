#!/usr/bin/env python
"""
generate_training_data.py — Create initial training examples for DistilBERT fine-tuning
Cycle 22, Phase 4 proof of concept

This script generates 50+ representative training examples for 5 key event classes.
In production, this would pull from apex.db and historical archives.
"""

import os
import json

# Training data examples for 5 representative event classes
TRAINING_EXAMPLES = {
    "E032": [  # CPI Release (BLS)
        "CPI released at 3.2% YoY, exceeding expectations. Inflation concerns trigger market sell-off.",
        "Bureau of Labor Statistics reports Consumer Price Index surge to 3.5%, highest in 18 months.",
        "Unexpected CPI spike of 3.8% YoY drives Treasury yields higher and equity futures lower.",
        "CPI misses on the downside: 2.7% vs expected 3.0%. Inflation cooling narrative supported.",
        "Core CPI inflation accelerates to 3.1% YoY as energy and food prices surge unexpectedly.",
        "Monthly CPI increase of 0.4% fuels stagflation fears and triggers defensive positioning.",
        "CPI report disappoints: headline inflation at 3.4% vs consensus 3.1%. Fed tightening bets rise.",
        "Consumer prices rise at fastest pace since February. CPI at 3.6% YoY stokes inflation debate.",
        "BLS CPI data shows persistent inflation above Fed target. Market volatility spikes on release.",
        "CPI remains sticky at 3.3% despite Fed rate hikes. Soft landing narrative questioned.",
        "Core CPI inflation at 3.2%, above expectations. Long-duration equities sell off on higher rates.",
        "CPI drops to 2.9% YoY, indicating inflation cooling. Equity rally follows strong data.",
        "Bureau of Labor Statistics: CPI down to 2.8%, below consensus. Disinflation trend confirmed.",
        "Headline CPI at 3.0%, matching expectations perfectly. Fed may pause rate hikes soon.",
        "CPI inflation moderates to 2.6% as base effects fade. Market sentiment improves on release.",
    ],
    "E050": [  # FDA Drug Rejection (FDA MedWatch)
        "FDA rejects drug application for Alzheimer's treatment citing insufficient efficacy data.",
        "FDA issues complete response letter for new diabetes drug. Company stock drops 12% on rejection.",
        "Federal Drug Administration denies approval for cancer therapy after advisory committee vote.",
        "FDA regulatory decision: approvability concerns on cardiovascular drug. Clinical trial questions.",
        "Drug application refused by FDA for autoimmune disorder treatment. Efficacy bar not met.",
        "FDA rejection of biologics license application for biotech company. Shares plummet 15%.",
        "Complete response letter from FDA on rare disease drug. Development timeline extends years.",
        "FDA denies NDA for pain management therapy citing safety profile concerns.",
        "Regulatory setback: FDA issues refuse-to-file letter for new indication. Clinical program halted.",
        "FDA rejection triggers 18% stock decline for pharmaceutical company. Efficacy data insufficient.",
        "Approvability concerns: FDA unlikely to approve migraine drug in current form.",
        "FDA safety review recommends against approval of cardiac therapy. Long-term data gaps cited.",
        "Regulatory pathway blocked: FDA advisors vote against approval for Parkinson's treatment.",
        "Drug candidate withdrawn by company after FDA rejection signals low approval likelihood.",
        "FDA decision letter: additional Phase 3 trials required. Development program extended 3 years.",
    ],
    "E070": [  # GDELT Diplomatic Crisis
        "US imposes new sanctions on Russia amid escalating Ukraine tensions. Oil prices spike 5%.",
        "China demands immediate diplomatic resolution amid Taiwan Strait military exercises.",
        "European Union threatens tariffs in response to trade violations. Currency markets react.",
        "India-Pakistan border tensions escalate. Military buildups reported by intelligence agencies.",
        "Middle Eastern diplomatic crisis emerges: Israel-Iran tensions spike. Geopolitical risk premium rises.",
        "North Korea conducts missile test amid escalating US-Korea diplomatic standoff.",
        "Trade war tensions resurface: US threatens additional tariffs on Chinese imports.",
        "Diplomatic breakdown: Two major economies recall ambassadors over treaty disagreement.",
        "Iran nuclear negotiations stall. JCPOA withdrawal threats renewed by multiple parties.",
        "Venezuela-Guyana border dispute escalates militarily. Regional instability feared.",
        "Saudi Arabia-Qatar relations deteriorate sharply. OPEC cohesion questioned.",
        "Russian-NATO tensions surge after military incidents in Eastern Europe.",
        "Philippines-China South China Sea dispute reaches critical point. Military standoff.",
        "African debt crisis triggers diplomatic tensions between debtor nations and creditors.",
        "Brexit-related negotiations collapse. UK-EU relations hit new low.",
    ],
    "E075": [  # Reddit Earnings Hype (Reddit Velocity)
        "r/wallstreetbets buzz on upcoming earnings: mentions of AMD surge 400% overnight.",
        "Reddit mentions of Tesla spike ahead of quarterly earnings report. Options volume surges.",
        "WSB earnings hype: mentions of NVIDIA jump 500% as earnings date approaches.",
        "Earnings euphoria on Reddit: mentions of Meta stock jump 300% in 24 hours.",
        "Retail investor fervor on Reddit for Microsoft earnings. Call volume at all-time high.",
        "Reddit sentiment spike for Apple earnings: bullish posts outnumber bears 10-to-1.",
        "WSB earnings play: mentions of Google surge as investors position ahead of report.",
        "Earnings anticipation on Reddit drives volume surge for Intel options.",
        "Reddit board erupts with earnings predictions for Amazon. Bullish thesis dominant.",
        "r/investing hype cycle for earnings season: mentions spike 250% for major cap stocks.",
        "Earnings-driven Reddit frenzy: small cap stock mentions surge as report nears.",
        "Options discussion on Reddit driven by earnings catalysts. IV skew shifts dramatically.",
        "Retail trader positioning on Reddit ahead of Netflix earnings. Speculative interest peaks.",
        "Earnings season Reddit activity: call spread positioning at extreme levels.",
        "WSB earnings calendar alerts trigger mention spikes across Reddit investing communities.",
    ],
    "E090": [  # Coinglass Margin Call (Coinglass)
        "Coinglass: margin call spike detected. BTC funding rates exceed 0.15% per 8 hours.",
        "Crypto derivatives market stress: liquidations exceed $200 million in 24 hours.",
        "Funding rates in crypto perpetuals surge to extreme levels. Long liquidation cascade risk.",
        "Coinglass data: liquidation cascades across exchanges. BTC/ETH margin positions underwater.",
        "Open interest spike in crypto derivatives. Leverage rushes signal volatility ahead.",
        "Binance and OKX funding rates indicate extreme long crowding. Liquidation risk elevated.",
        "Crypto derivatives report: $150M liquidated in single price move. Margin calls mount.",
        "Funding rate extremes on bybit perpetuals. Borrow costs surge for leverage traders.",
        "Long/short imbalance on crypto exchanges reaches 70/30 split. Positioning risk extreme.",
        "Coinglass alert: liquidation volume at $300M+ daily. Exchange reserve movements signal selling.",
        "Margin interest rates spike 2% across major crypto lending platforms.",
        "Ethereum perpetuals funding rates above 0.1% per 8h. Leverage unwinding pressure builds.",
        "Bitcoin open interest drops 40% after liquidation cascade. Leverage resets market.",
        "Crypto derivatives exchange data shows record margin calls. Borrow costs peak.",
        "Solana and Altcoin perpetuals see funding rates above 0.15%. Risk premium spike.",
    ],
}

def generate_training_data():
    """Generate initial training data for proof of concept."""
    
    base_path = r"C:\Users\nmuso\Documents\signal-trader\training_data"
    
    for event_id, examples in TRAINING_EXAMPLES.items():
        event_dir = os.path.join(base_path, event_id)
        os.makedirs(event_dir, exist_ok=True)
        
        for idx, example in enumerate(examples, 1):
            filename = os.path.join(event_dir, f"{idx:03d}.txt")
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(example)
        
        print(f"[OK] {event_id}: Created {len(examples)} training examples")
    
    # Create summary
    summary = {
        "total_events": len(TRAINING_EXAMPLES),
        "total_examples": sum(len(v) for v in TRAINING_EXAMPLES.values()),
        "examples_per_class": {k: len(v) for k, v in TRAINING_EXAMPLES.items()},
        "status": "Proof of concept complete - ready for expansion"
    }
    
    summary_file = os.path.join(base_path, "TRAINING_SUMMARY.json")
    with open(summary_file, 'w') as f:
        json.dump(summary, f, indent=2)
    
    print(f"\n[INFO] Summary saved to TRAINING_SUMMARY.json")
    print(f"   Total: {summary['total_examples']} examples across {summary['total_events']} event classes")
    print(f"   Average per class: {summary['total_examples'] // summary['total_events']} examples")

if __name__ == "__main__":
    generate_training_data()
