"""
Optional, read-only wallet lookups for the agent's public address.
No signing. No private key is ever read. Set AGENT_WALLET_ADDRESS to enable.
"""

import os
from typing import Optional

# Public RPC endpoints
ETH_RPC = os.environ.get("ETH_RPC_URL", "https://eth.llamarpc.com")
BASE_RPC = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")

WALLET_ADDRESS = os.environ.get("AGENT_WALLET_ADDRESS", "")


def get_eth_balance() -> Optional[dict]:
    """Get the agent's ETH balance on mainnet and Base."""
    if not WALLET_ADDRESS:
        return None
    try:
        from web3 import Web3

        results = {}

        # Mainnet
        try:
            w3_eth = Web3(Web3.HTTPProvider(ETH_RPC))
            if w3_eth.is_connected():
                bal_wei = w3_eth.eth.get_balance(WALLET_ADDRESS)
                results["mainnet_eth"] = float(Web3.from_wei(bal_wei, "ether"))
        except Exception as e:
            print(f"[wallet] mainnet balance check failed: {e}")
            results["mainnet_eth"] = None

        # Base
        try:
            w3_base = Web3(Web3.HTTPProvider(BASE_RPC))
            if w3_base.is_connected():
                bal_wei = w3_base.eth.get_balance(WALLET_ADDRESS)
                results["base_eth"] = float(Web3.from_wei(bal_wei, "ether"))
        except Exception as e:
            print(f"[wallet] base balance check failed: {e}")
            results["base_eth"] = None

        results["address"] = WALLET_ADDRESS
        return results

    except ImportError:
        print("[wallet] web3 not installed")
        return None
    except Exception as e:
        print(f"[wallet] balance check failed: {e}")
        return None


def get_wallet_address() -> str:
    """Return the agent's public wallet address."""
    return WALLET_ADDRESS


def get_nft_holdings() -> Optional[list]:
    """Fetch the agent's NFT holdings via OpenSea API (no key needed for basic lookups)."""
    if not WALLET_ADDRESS:
        return None
    try:
        import httpx

        url = f"https://api.opensea.io/api/v2/chain/ethereum/account/{WALLET_ADDRESS}/nfts"
        resp = httpx.get(url, timeout=15, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            print(f"[wallet] OpenSea NFT fetch failed: HTTP {resp.status_code}")
            return None

        data = resp.json()
        nfts = []
        for nft in data.get("nfts", []):
            nfts.append({
                "name": nft.get("name", "Unknown"),
                "collection": nft.get("collection", ""),
                "token_id": nft.get("identifier", ""),
                "image_url": nft.get("image_url", ""),
            })
        return nfts

    except Exception as e:
        print(f"[wallet] NFT holdings check failed: {e}")
        return None


def get_transaction(tx_hash: str) -> Optional[dict]:
    """Fetch transaction details from mainnet."""
    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(ETH_RPC))
        if not w3.is_connected():
            return None

        tx = w3.eth.get_transaction(tx_hash)
        receipt = w3.eth.get_transaction_receipt(tx_hash)

        return {
            "hash": tx_hash,
            "from": tx.get("from", ""),
            "to": tx.get("to", ""),
            "value_eth": float(Web3.from_wei(tx.get("value", 0), "ether")),
            "status": "success" if receipt.get("status") == 1 else "failed",
            "block": receipt.get("blockNumber", 0),
        }

    except Exception as e:
        print(f"[wallet] transaction lookup failed: {e}")
        return None
