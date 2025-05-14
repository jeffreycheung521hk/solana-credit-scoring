#!/usr/bin/env python3
import os
import asyncio
from dotenv import load_dotenv
import httpx
from openai import OpenAI
from collections import Counter
import json
import re
import time

# Environment settings
load_dotenv()
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not HELIUS_API_KEY:
    raise EnvironmentError("HELIUS_API_KEY not found in environment")
if not OPENAI_API_KEY:
    raise EnvironmentError("OPENAI_API_KEY not found in environment")

client = OpenAI(api_key=OPENAI_API_KEY)

# Universal JSON request function
async def fetch_json(url: str, method: str = "GET", json: dict = None, retries: int = 3, timeout: int = 30) -> dict:
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
                resp = await (client.post(url, json=json) if method == "POST" else client.get(url))
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            if attempt == retries - 1:
                raise Exception(f"Request failed: {e}")
            await asyncio.sleep(1)

# Query stake accounts
async def get_stake_accounts(address: str) -> float:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getProgramAccounts",
        "params": [
            "Stake11111111111111111111111111111111111111",
            {
                "encoding": "base64",
                "filters": [
                    {"memcmp": {"offset": 44, "bytes": address}}
                ]
            }
        ]
    }
    try:
        data = await fetch_json("https://api.mainnet-beta.solana.com", method="POST", json=payload)
        return sum(account.get("account", {}).get("lamports", 0) for account in data.get("result", [])) / 1e9
    except Exception as e:
        print(f"⚠️ Stake accounts fetch failed: {e}")
        return 0.0

# Query token assets
async def fetch_token_profiles_das(address: str) -> list:
    payload = {
        "jsonrpc": "2.0",
        "id": "fetch-assets",
        "method": "getAssetsByOwner",
        "params": {
            "ownerAddress": address,
            "page": 1,
            "limit": 100,
            "options": {
                "showUnverifiedCollections": False,
                "showCollectionMetadata": False,
                "showGrandTotal": False,
                "showFungible": True,
                "showNativeBalance": True,
                "showInscription": False,
                "showZeroBalance": False
            }
        }
    }
    data = await fetch_json(f"https://rpc.helius.xyz/?api-key={HELIUS_API_KEY}", method="POST", json=payload)
    result = data.get("result", {})

    token_profiles = []
    for asset in result.get("items", []):
        token_info = asset.get("token_info", {})
        symbol = token_info.get("symbol", "Unknown")
        if symbol == "Unknown":
            continue
        decimals = token_info.get("decimals", 0)
        balance = float(token_info.get("balance", 0)) / 10**decimals
        if balance == 0:
            continue
        token_profiles.append({"symbol": symbol, "balance": balance, "txVolume": ""})

    # Native SOL
    lamports = result.get("nativeBalance", {}).get("lamports", 0)
    if lamports:
        token_profiles.append({"symbol": "SOL", "balance": lamports / 1e9, "txVolume": ""})

    # staking SOL
    staked = await get_stake_accounts(address)
    if staked > 0:
        token_profiles.append({"symbol": "stakedSOL", "balance": staked, "txVolume": ""})

    return token_profiles

# Transaction and signature parsing
BASE_URL = "https://api.helius.xyz/v0/addresses"
SMALL_TX_THRESHOLD = int(0.1 * 1e9)

async def get_transactions(address: str, limit: int = 500):
    import time
    start_time = time.time()
    last_print = 0
    normal_txs, small_count = [], 0
    before = None
    while True:
        url = f"{BASE_URL}/{address}/transactions?limit=100&api-key={HELIUS_API_KEY}&includeTransactionDetails=true"
        if before:
            url += f"&before={before}"
        data = await fetch_json(url)
        if not data:
            break
        elapsed = time.time() - start_time
        if elapsed - last_print >= 3:
            print(f"Fetching data {int(elapsed)}s *")
            last_print = elapsed

        for tx in data:
            lam = sum(abs(x.get("amount", 0)) for x in tx.get("nativeTransfers", []))
            if lam < SMALL_TX_THRESHOLD:
                small_count += 1
            else:
                normal_txs.append(tx)
                if len(normal_txs) >= limit:
                    break
        if len(normal_txs) >= limit or not (sig := data[-1].get("signature")):
            break
        before = sig
    return normal_txs, small_count

async def fetch_parsed_signatures(sigs: list[str], batch_size: int = 20) -> list[dict]:
    results = []
    url = f"https://api.helius.xyz/v0/transactions?api-key={HELIUS_API_KEY}"
    for i in range(0, len(sigs), batch_size):
        batch = sigs[i:i+batch_size]
        data = await fetch_json(url, method="POST", json={"transactions": batch})
        results.extend(data)
        await asyncio.sleep(1)
    return results

# Main program
async def main():
    print("🔍 Solana Wallet Analyzer CLI - Credit Assessment")
    while True:
        addr = input("Please enter address or exit to quit: ").strip()
        if addr.lower() in ("exit", "quit"):
            break

        txs, small_tx_count = await get_transactions(addr)
        token_profiles = await fetch_token_profiles_das(addr)

        sigs = [tx["signature"] for tx in txs][:100]
        start_time = time.time()
        last_print = 0
        parsed = []
        for i in range(0, len(sigs), 20):
            batch = sigs[i:i+20]
            data = await fetch_json(f"https://api.helius.xyz/v0/transactions?api-key={HELIUS_API_KEY}", method="POST", json={"transactions": batch})
            parsed.extend(data)
            elapsed = time.time() - start_time
            if elapsed - last_print >= 3:
                print(f"Calculating credit score {int(elapsed)}s *")
                last_print = elapsed
            await asyncio.sleep(1)

        total = len(parsed)
        count = Counter(tx.get("type", "UNKNOWN") for tx in parsed)

        for profile in token_profiles:
            sym = profile["symbol"]
            n = sum(1 for tx in parsed if tx.get("tokenSymbol", "") == sym)
            profile["txVolume"] = f"{n/total:.2%}" if total else "0.00%"

        print("\n📝 Asset Overview:")
        for profile in token_profiles:
            print(f"{profile['symbol']}: {profile['balance']} (Transaction Ratio: {profile['txVolume']})")

        print("\n📊 Summary:")
        print(f"Analyzed Normal Transactions (excluding small): {total}, Small Transactions (<0.1 SOL): {small_tx_count}")
        print(f"Transaction Types: {dict(count)}")

        prompt = f"""System:
You are a Solana on-chain data analysis expert, specializing in credit assessment for lending protocols (such as Solend). Based on the following data, generate a concise analysis report in JSON format, in English only, output complete JSON only, no extra explanation.
- Total transactions: {total}
- Small transactions (<0.1 SOL): {small_tx_count}
- Transaction types: {dict(count)}
- Token data: {json.dumps(token_profiles, ensure_ascii=False)}

Output requirements:
- Each analysis field should not exceed 15 characters, and the conclusion should not exceed 25 characters.
- Include summary, asset overview, behavior analysis, risks, suggestions, and credit conclusion.
- Summary must contain credit grade (High, Medium, Low), based on the following rules:
  - High: Large SOL/stakedSOL (>10 SOL), stable transfers (TRANSFER > 50%), no high risk.
  - Medium: Medium SOL/stakedSOL (1-10 SOL), stable transfers (TRANSFER > 30%), low risk.
  - Low: Little SOL/stakedSOL (<1 SOL), high frequency SWAP or small transactions ratio >80%.
- Asset overview must include liquidity (High: SOL; Medium: stakedSOL, mSOL; Low: other tokens).
- Behavior analysis only includes SWAP, TRANSFER, OTHER.
- Calculate small transaction ratio as smallCount/(totalCount+smallCount).
- Ensure single JSON, no duplicates.

Format:
{{
  "Summary": {{
    "Total Transactions": number,
    "Small Transactions": number,
    "Small Transaction Ratio": string,
    "Credit Grade": string
  }},
  "Asset Overview": [
    {{"Token": string, "Balance": number, "Liquidity": string, "Risk": string}},
    ...
  ],
  "Behavior Analysis": [
    {{"Type": string, "Count": number, "Ratio": string, "Assessment": string}},
    ...
  ],
  "Risk": {{
    "Dust Attack": string,
    "High-frequency Arbitrage": string,
    "Low Liquidity Tokens": string
  }},
  "Suggestions": [string],
  "Credit Conclusion": string
}}
"""

        try:
            chat_resp = client.chat.completions.create(
                model="gpt-4.1-nano-2025-04-14",  # Assume using gpt-4.1nano, confirm availability
                messages=[
                    {"role": "system", "content": "You are a Solana on-chain data analysis expert. Output concise JSON in English only, for credit assessment."},
                    {"role": "user", "content": prompt},
                ],
                max_completion_tokens=800,
                temperature=0.5
            )
            analysis = chat_resp.choices[0].message.content.strip()

            # Clean up possible Markdown markup
            analysis = re.sub(r'^```json\n|```$', '', analysis).strip()

            # Validate and format JSON
            try:
                parsed = json.loads(analysis)
                print("\n📝 Credit Analysis:")
                print(json.dumps(parsed, indent=2, ensure_ascii=False))
            except json.JSONDecodeError as e:
                print(f"⚠️ OpenAI returned invalid JSON: {analysis}")
                print(f"Parse error: {e}")
                analysis = "{}"
        except Exception as e:
            print(f"⚠️ OpenAI analysis failed: {e}")
            analysis = "{}"

        # Optional: Save to file
        save = input("Save analysis to file? (y/n): ").strip().lower()
        if save == "y":
            with open(f"credit_analysis_{addr}.json", "w", encoding="utf-8") as f:
                json.dump(json.loads(analysis), f, indent=2, ensure_ascii=False)

        print("-" * 50)

if __name__ == "__main__":
    asyncio.run(main())
