#!/usr/bin/env python3
"""Quick test: Verify EIA API key works."""

import os
import asyncio
import aiohttp
from dotenv import load_dotenv

load_dotenv()
key = os.environ.get('EIA_API_KEY')

if not key:
    print("ERROR: EIA_API_KEY not found in .env")
    exit(1)

print(f"Using EIA key: {key[:10]}...")

async def test_eia():
    url = f'https://api.eia.gov/v2/seriesid/PET.WCRSTUS1.W?api_key={key}&length=2'
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    records = data.get('response', {}).get('data', [])
                    print(f"SUCCESS: EIA API is working!")
                    print(f"Got {len(records)} records")
                    if records:
                        print(f"Latest: {records[0]}")
                else:
                    print(f"ERROR: HTTP {resp.status}")
    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == '__main__':
    asyncio.run(test_eia())
