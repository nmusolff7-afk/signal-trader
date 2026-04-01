# TRAINING_WORKFLOW.md — Training Data Collection Pipeline

**Created:** 2026-04-01  
**Project:** APEX Signal Trader — DistilBERT fine-tuning data collection  
**Target:** 250–500 labeled examples per event class (90 total events)

---

## Overview

This document describes the workflow for collecting, labeling, and organizing training data for DistilBERT fine-tuning on the APEX taxonomy (E001–E090).

## Directory Structure

```
C:\Users\nmuso\Documents\signal-trader\training_data\
├── E001/  (E001: Crude Oil Drawdown)
│   ├── 001.txt
│   ├── 002.txt
│   ├── ...
│   └── NNN.txt (target: 250–500 files)
├── E002/  (E002: Crude Oil Build)
│   ├── 001.txt
│   └── ...
├── ...
└── E090/  (E090: Margin Call Spike)
    └── ...
```

## Labeling Workflow

### Step 1: Extract Events from Database

Source: `apex.db` `raw_events` table

For each event class (E001–E090):
1. Query database: `SELECT text, headline FROM raw_events WHERE event_id = 'E###'`
2. Export results to CSV or JSON
3. For each row, create text file: `training_data/E###/NNN.txt` containing the raw event text

### Step 2: Format Training Examples

Each `.txt` file contains:
- Raw event text from database OR
- Public source data (news article, RSS feed, etc.) formatted as narrative

Example (E032 — CPI release):
```
CPI release triggered market decline. Consumer Price Index rose 3.2% year-over-year,
exceeding expectations of 3.0%. The unexpected surge in inflation concerns led to a 1.5%
drop in broad market indices and a 45 basis point jump in 10-year Treasury yields.
```

### Step 3: Sourcing Strategy

For each event type, use these sources in order of preference:

1. **Live data (apex.db):** Events already classified and stored in database
2. **Historical archives:** Public feeds and RSS archives
   - BLS: https://www.bls.gov/news.release/archives.htm
   - NOAA: https://www.swpc.noaa.gov/
   - SEC: https://www.sec.gov/cgi-bin/browse-edgar
   - GDELT: http://data.gdeltproject.org/
3. **Synthetic generation (last resort):** Use LLM to generate realistic examples if insufficient real data

### Step 4: Validation Checklist

For each event class:
- [ ] Minimum 50 examples collected (proof of concept)
- [ ] Minimum 250 examples for final training
- [ ] Each example is 1–5 sentences describing the event
- [ ] No duplicates (deduplicate before storing)
- [ ] Event class label (E###) is accurate

## Event Classes (Priority Order)

### Proof of Concept (Cycle 22)
Target 50+ examples for each:
1. **E032:** BLS CPI Release (economic data)
2. **E050:** FDA Drug Rejection (regulatory)
3. **E070:** GDELT Diplomatic Crisis (geopolitical)
4. **E075:** Reddit Earnings Hype (retail sentiment)
5. **E090:** Coinglass Margin Call (crypto derivatives)

### Expansion (Cycle 23+)
Scale to 250+ per class across all 90 events.

## Automated Collection

For live data, use Python script (to be implemented):

```python
# Pseudocode for automated collection
import sqlite3

db = sqlite3.connect('apex.db')
cursor = db.cursor()

for event_id in ['E001', 'E002', ..., 'E090']:
    cursor.execute(
        "SELECT text FROM raw_events WHERE event_id = ? LIMIT 500",
        (event_id,)
    )
    results = cursor.fetchall()
    
    # Save each result to training_data/E###/NNN.txt
    for idx, (text,) in enumerate(results, 1):
        filename = f'training_data/{event_id}/{idx:03d}.txt'
        with open(filename, 'w') as f:
            f.write(text)
```

## Deduplication

Before training:
1. Hash each example (MD5)
2. Keep only unique hashes
3. Log duplicates found (optional)

## File Naming Convention

- Naming: `NNN.txt` where NNN is zero-padded sequence number (001, 002, ..., 500)
- Content: Plain text, no JSON or metadata wrappers
- Encoding: UTF-8
- Max length: No hard limit, but keep reasonable (1–1000 words typical)

## Quality Metrics

Track per event class:
- **Total examples:** Count of files
- **Average length:** Mean words per example
- **Duplication rate:** % of duplicates before deduplication
- **Source breakdown:** % from DB vs. historical vs. synthetic

## Timeline

- **Cycle 22:** Infrastructure + 50 examples for 5 key classes
- **Cycle 23:** Expand to 250+ per class across all 90 events
- **Cycle 24:** Deduplication, quality check, ready for Phase 5 (DistilBERT training)

---

## Commands

Count examples per class:
```bash
cd C:\Users\nmuso\Documents\signal-trader\training_data
for i in E*; do echo "$i: $(ls $i 2>/dev/null | wc -l) examples"; done
```

Count total:
```bash
find training_data -name "*.txt" | wc -l
```

---

_This workflow will be automated in Phase 4. Manual collection for POC._
