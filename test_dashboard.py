#!/usr/bin/env python3
"""Test dashboard API responses"""

import requests
import json

try:
    resp = requests.get('http://localhost:8000/api/status', timeout=5)
    data = resp.json()
    
    print('Dashboard API Test:')
    print(f'  Total Events: {data.get("event_count", 0)}')
    print(f'  Classified: {data.get("classified_count", 0)}')
    print(f'  Tradeable: {data.get("tradeable_count", 0)}')
    print(f'  Trades: {data.get("trade_count", 0)}')
    print(f'  Phase: {data.get("current_phase", 0)}')
    print(f'  Sources: {len(data.get("source_health", []))}')
    
    for s in data.get('source_health', []):
        print(f'    - {s["source"]}: {s["total"]} events')
    
    print('\nDashboard is working!')
    
except Exception as e:
    print(f'Error: {e}')
