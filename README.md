# Solana Credit Scoring Prototype

A Python-based command-line tool for on-chain credit assessment of Solana wallets.  
Designed for DeFi lending protocols and risk analytics.

## Features

- Fetches and parses Solana wallet transactions and token balances using [Helius API](https://www.helius.dev/).
- Filters out micro-transactions (less than 0.1 SOL) to focus on meaningful user behavior.
- Aggregates up to 100 normal transactions per address for robust analysis.
- Analyzes on-chain behavior, asset composition, and risk factors.
- Outputs credit scoring results as structured JSON.
- Integrates with OpenAI API to generate human-readable risk reports.
- Easy to use and extend, with future support for protocol-specific customization.

## Usage

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
Set up your environment variables
Create a .env file with your API keys:
HELIUS_API_KEY=your_helius_api_key
OPENAI_API_KEY=your_openai_api_key
Run the CLI
python main.py
Enter a Solana wallet address when prompted.
The tool will fetch and analyze the latest on-chain activity and output a JSON credit report.
Technical Highlights

Built with Python asyncio for efficient data fetching.
Leverages Helius API for reliable, decoded Solana data.
Filters out transactions below 0.1 SOL to reduce noise from micro-transactions.
Supports staked SOL and other token assets.
Modular and ready for integration or expansion.
Coming Soon

Protocol-specific credit scoring customization
Web dashboard & batch analysis
RWA (Real World Asset) support
