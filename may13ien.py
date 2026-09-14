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
        token_profiles.append({"symbol": symbol, "mint": asset.get("id"), "balance": balance, "txVolume": ""})

    # Native SOL
    lamports = result.get("nativeBalance", {}).get("lamports", 0)
    if lamports:
        token_profiles.append({"symbol": "SOL", "mint": "SOL", "balance": lamports / 1e9, "txVolume": ""})

    # staking SOL
    staked = await get_stake_accounts(address)
    if staked > 0:
        token_profiles.append({"symbol": "stakedSOL", "mint": "stakedSOL", "balance": staked, "txVolume": ""})

    return token_profiles

# Transaction and signature parsing
BASE_URL = "https://api.helius.xyz/v0/addresses"

# "Meaningful" is decided by what moved and who moved it, not by how much SOL moved.
# The old filter (native SOL < 0.1) silently discarded every stablecoin transfer,
# because a USDC/USDT transfer moves ~0 native SOL.
DUST_SOL_THRESHOLD = 0.001          # native SOL below this, with no token movement, is dust
STABLECOIN_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}

def tx_touches(tx: dict, address: str):
    """Native SOL moved (in SOL) and token transfers that involve `address`."""
    native = sum(
        abs(x.get("amount", 0)) for x in tx.get("nativeTransfers", [])
        if address in (x.get("fromUserAccount"), x.get("toUserAccount"))
    ) / 1e9
    tokens = [
        t for t in tx.get("tokenTransfers", [])
        if address in (t.get("fromUserAccount"), t.get("toUserAccount"))
    ]
    return native, tokens

def is_meaningful(tx: dict, address: str) -> bool:
    native, tokens = tx_touches(tx, address)
    return bool(tokens) or native >= DUST_SOL_THRESHOLD

async def get_transactions(address: str, limit: int = 500):
    import time
    start_time = time.time()
    last_print = 0
    normal_txs, dust_count = [], 0
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
            if is_meaningful(tx, address):
                normal_txs.append(tx)
                if len(normal_txs) >= limit:
                    break
            else:
                dust_count += 1
        if len(normal_txs) >= limit or not (sig := data[-1].get("signature")):
            break
        before = sig
    return normal_txs, dust_count

async def fetch_parsed_signatures(sigs: list[str], batch_size: int = 20) -> list[dict]:
    results = []
    url = f"https://api.helius.xyz/v0/transactions?api-key={HELIUS_API_KEY}"
    for i in range(0, len(sigs), batch_size):
        batch = sigs[i:i+batch_size]
        data = await fetch_json(url, method="POST", json={"transactions": batch})
        results.extend(data)
        await asyncio.sleep(1)
    return results

# Flow profile: who produced each transaction (source), and who the address transacts with
def profile_flows(parsed: list[dict], address: str) -> dict:
    type_counts = Counter(tx.get("type", "UNKNOWN") for tx in parsed)
    source_counts = Counter(tx.get("source", "UNKNOWN") for tx in parsed)

    inbound, outbound = Counter(), Counter()
    stable = {"in_usd": 0.0, "out_usd": 0.0, "in_count": 0, "out_count": 0}
    timestamps = [tx["timestamp"] for tx in parsed if tx.get("timestamp")]

    for tx in parsed:
        for x in tx.get("nativeTransfers", []):
            src, dst = x.get("fromUserAccount"), x.get("toUserAccount")
            if src == address and dst:
                outbound[dst] += 1
            elif dst == address and src:
                inbound[src] += 1
        for t in tx.get("tokenTransfers", []):
            src, dst = t.get("fromUserAccount"), t.get("toUserAccount")
            amt = float(t.get("tokenAmount") or 0)
            is_stable = t.get("mint") in STABLECOIN_MINTS
            if src == address and dst:
                outbound[dst] += 1
                if is_stable:
                    stable["out_usd"] += amt
                    stable["out_count"] += 1
            elif dst == address and src:
                inbound[src] += 1
                if is_stable:
                    stable["in_usd"] += amt
                    stable["in_count"] += 1

    all_cp = inbound + outbound
    total_edges = sum(all_cp.values())
    top = all_cp.most_common(5)
    concentration = (top[0][1] / total_edges) if total_edges else 0.0
    active_days = (max(timestamps) - min(timestamps)) / 86400 if len(timestamps) > 1 else 0.0

    return {
        "type_counts": dict(type_counts),
        "source_counts": dict(source_counts),
        "counterparties": {
            "unique_inbound": len(inbound),
            "unique_outbound": len(outbound),
            "top": [{"address": a, "count": c} for a, c in top],
            "top1_concentration": round(concentration, 4),
        },
        "stablecoin_flow": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in stable.items()},
        "active_span_days": round(active_days, 1),
    }

# Grading rules — same three grades and thresholds that used to live in the LLM prompt,
# now deterministic and auditable. Only "high frequency SWAP" needed a concrete definition.
SOL_HIGH, SOL_MEDIUM = 10.0, 1.0
TRANSFER_SHARE_HIGH, TRANSFER_SHARE_MEDIUM = 0.5, 0.3
SWAP_HIGH_SHARE = 0.5
DUST_RATIO_LOW = 0.8

def grade_credit(sol_total: float, transfer_share: float, swap_share: float, dust_ratio: float):
    reasons = []
    risky = False
    if swap_share > SWAP_HIGH_SHARE:
        risky = True
        reasons.append(f"SWAP share {swap_share:.0%} > {SWAP_HIGH_SHARE:.0%}")
    if dust_ratio > DUST_RATIO_LOW:
        risky = True
        reasons.append(f"dust ratio {dust_ratio:.0%} > {DUST_RATIO_LOW:.0%}")

    if sol_total > SOL_HIGH and transfer_share > TRANSFER_SHARE_HIGH and not risky:
        grade = "High"
        reasons.append(f"SOL+staked {sol_total:.2f} > {SOL_HIGH}, TRANSFER share {transfer_share:.0%} > {TRANSFER_SHARE_HIGH:.0%}")
    elif sol_total >= SOL_MEDIUM and transfer_share > TRANSFER_SHARE_MEDIUM and not risky:
        grade = "Medium"
        reasons.append(f"SOL+staked {sol_total:.2f} >= {SOL_MEDIUM}, TRANSFER share {transfer_share:.0%} > {TRANSFER_SHARE_MEDIUM:.0%}")
    else:
        grade = "Low"
        if not reasons:
            reasons.append(f"SOL+staked {sol_total:.2f} or TRANSFER share {transfer_share:.0%} below Medium thresholds")
    return grade, reasons

def liquidity_bucket(symbol: str) -> str:
    if symbol == "SOL":
        return "High"
    if symbol in ("stakedSOL", "mSOL"):
        return "Medium"
    return "Low"

# Main program
async def main():
    print("🔍 Solana Wallet Analyzer CLI - Credit Assessment")
    while True:
        addr = input("Please enter address or exit to quit: ").strip()
        if addr.lower() in ("exit", "quit"):
            break

        txs, dust_tx_count = await get_transactions(addr)
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
        flows = profile_flows(parsed, addr)
        count = flows["type_counts"]

        # Per-token transaction ratio, matched by mint (the old code matched a
        # non-existent top-level "tokenSymbol" field, so this was always 0.00%).
        for profile in token_profiles:
            mint = profile["mint"]
            if mint == "SOL":
                n = sum(1 for tx in parsed if tx_touches(tx, addr)[0] > 0)
            elif mint == "stakedSOL":
                n = 0
            else:
                n = sum(1 for tx in parsed if any(t.get("mint") == mint for t in tx_touches(tx, addr)[1]))
            profile["txVolume"] = f"{n/total:.2%}" if total else "0.00%"

        # Deterministic grade — the rules that used to be in the LLM prompt
        dust_ratio = dust_tx_count / (total + dust_tx_count) if (total + dust_tx_count) else 0.0
        transfer_share = count.get("TRANSFER", 0) / total if total else 0.0
        swap_share = count.get("SWAP", 0) / total if total else 0.0
        sol_total = sum(p["balance"] for p in token_profiles if p["symbol"] in ("SOL", "stakedSOL"))
        grade, reasons = grade_credit(sol_total, transfer_share, swap_share, dust_ratio)

        report = {
            "Summary": {
                "Total Transactions": total,
                "Dust Transactions": dust_tx_count,
                "Dust Transaction Ratio": f"{dust_ratio:.2%}",
                "Active Span Days": flows["active_span_days"],
                "Credit Grade": grade,
                "Grade Reasons": reasons,
            },
            "Asset Overview": [
                {"Token": p["symbol"], "Balance": p["balance"], "Liquidity": liquidity_bucket(p["symbol"]), "Transaction Ratio": p["txVolume"]}
                for p in token_profiles
            ],
            "Behavior Analysis": [
                {"Type": t, "Count": c, "Ratio": f"{c/total:.2%}"} for t, c in sorted(count.items(), key=lambda kv: -kv[1])
            ],
            "Source Histogram": dict(sorted(flows["source_counts"].items(), key=lambda kv: -kv[1])),
            "Counterparties": flows["counterparties"],
            "Stablecoin Flow": flows["stablecoin_flow"],
            "Risk": {
                "Dust Attack": "flag" if dust_ratio > DUST_RATIO_LOW else "ok",
                "High-frequency Swap": "flag" if swap_share > SWAP_HIGH_SHARE else "ok",
                "Low Liquidity Tokens": "flag" if any(liquidity_bucket(p["symbol"]) == "Low" for p in token_profiles) else "ok",
            },
        }

        print("\n📝 Asset Overview:")
        for profile in token_profiles:
            print(f"{profile['symbol']}: {profile['balance']} (Transaction Ratio: {profile['txVolume']})")

        print("\n📊 Summary:")
        print(f"Analyzed Meaningful Transactions: {total}, Dust Transactions: {dust_tx_count}")
        print(f"Transaction Types: {count}")
        print(f"Sources: {report['Source Histogram']}")
        print(f"Counterparties: {flows['counterparties']['unique_inbound']} in / {flows['counterparties']['unique_outbound']} out, top-1 concentration {flows['counterparties']['top1_concentration']:.0%}")
        print(f"Stablecoin flow: {flows['stablecoin_flow']}")
        print(f"Credit Grade: {grade} — {'; '.join(reasons)}")

        # LLM writes the narrative only; it does not decide the grade.
        prompt = f"""You are a Solana on-chain data analysis expert, specializing in credit assessment for lending protocols (such as Solend).
The credit grade below has already been computed by fixed rules. Do NOT change it. Based on the report, write a concise narrative in JSON format, in English only, output complete JSON only, no extra explanation.

Report:
{json.dumps(report, ensure_ascii=False)}

Output requirements:
- Each suggestion should not exceed 15 words, and the conclusion should not exceed 25 words.
- Comment on the source histogram (which programs produced the activity), counterparty concentration, and stablecoin flow.

Format:
{{
  "Suggestions": [string],
  "Credit Conclusion": string
}}
"""

        analysis = {}
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
            text = chat_resp.choices[0].message.content.strip()

            # Clean up possible Markdown markup
            text = re.sub(r'^```json\n|```$', '', text).strip()

            # Validate and format JSON
            try:
                analysis = json.loads(text)
            except json.JSONDecodeError as e:
                print(f"⚠️ OpenAI returned invalid JSON: {text}")
                print(f"Parse error: {e}")
        except Exception as e:
            print(f"⚠️ OpenAI analysis failed: {e}")

        report["Suggestions"] = analysis.get("Suggestions", [])
        report["Credit Conclusion"] = analysis.get("Credit Conclusion", "")
        print("\n📝 Credit Analysis:")
        print(json.dumps(report, indent=2, ensure_ascii=False))

        # Optional: Save to file
        save = input("Save analysis to file? (y/n): ").strip().lower()
        if save == "y":
            with open(f"credit_analysis_{addr}.json", "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)

        print("-" * 50)

if __name__ == "__main__":
    asyncio.run(main())
