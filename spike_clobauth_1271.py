"""
Decisive experiment, step 3 — does the CLOB server accept an ERC-1271/7739
ClobAuth signature on /auth/api-key? This is THE unknown that decides whether
deposit-wallet go-live is achievable.

Approach (so the result is unambiguous):
  1. Build a Solady ERC-7739 `TypedDataSign`-wrapped ClobAuth signature for the
     deposit wallet — a faithful port of the order-side POLY_1271 signer
     (py_clob_client_v2.order_utils.exchange_order_builder_v2), with ClobAuth as
     the contents and the ClobAuthDomain as the app separator.
  2. SELF-CHECK on-chain: eth_call deposit_wallet.isValidSignature(digest, sig).
     If it returns the EIP-1271 magic 0x1626ba7e, our signature is provably
     correct — so any server rejection is the SERVER not supporting 1271 on
     /auth/api-key, not our bug.
  3. POST the L1 headers (POLY_ADDRESS = deposit wallet) to /auth/api-key and
     report the verbatim response.

VERDICT:
  - isValidSignature ✅ AND server accepts -> deposit-wallet auth WORKS. Build the
    ~50-line createL1HeadersWrapped1271 patch and go live.
  - isValidSignature ✅ AND server rejects -> server does NOT support 1271 auth.
    Blocked upstream; stay paper. (Expected outcome.)
  - isValidSignature ❌ -> our wrapping is wrong; fix before trusting the server result.

Requires the deposit wallet DEPLOYED (run spike_deploy_deposit_wallet.py first) —
ERC-1271 only works against deployed bytecode.

Run (from the Finland VPS to avoid geoblock on the CLOB host):
  venv/bin/python spike_clobauth_1271.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from dotenv import load_dotenv
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak

load_dotenv()

EXPECTED_WALLET = "0xcee18163eeb650177161a7174b760cf71d45bc8a"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
CREATE_API_KEY = "/auth/api-key"          # POST
DERIVE_API_KEY = "/auth/derive-api-key"   # GET
MAGIC_1271 = "1626ba7e"                    # EIP-1271 valid-signature magic value

# ClobAuth EIP-712 (matches py_clob_client_v2.signing.model.ClobAuth field order)
CLOBAUTH_TYPE_STRING = "ClobAuth(address address,string timestamp,uint256 nonce,string message)"
MSG_TO_SIGN = "This message attests that I control the given wallet"

# Solady TypedDataSign wrapper, ClobAuth variant (cf. order-side SOLADY_TYPE_STRING)
SOLADY_TYPE_STRING = (
    "TypedDataSign(ClobAuth contents,string name,string version,uint256 chainId,"
    "address verifyingContract,bytes32 salt)" + CLOBAUTH_TYPE_STRING
)
SOLADY_TYPE_HASH = keccak(text=SOLADY_TYPE_STRING)
DEPOSIT_WALLET_NAME_HASH = keccak(text="DepositWallet")
DEPOSIT_WALLET_VERSION_HASH = keccak(text="1")
ZERO_SALT = b"\x00" * 32

RPCS = [
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
    "https://1rpc.io/matic",
]


def _rpc(method: str, params: list):
    last = None
    for ep in RPCS:
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
            req = urllib.request.Request(ep, data=body, headers={
                "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (clobauth-spike)"})
            with urllib.request.urlopen(req, timeout=15) as r:
                out = json.loads(r.read())
            if out.get("error"):
                last = out["error"]; continue
            return out.get("result")
        except Exception as e:  # noqa: BLE001
            last = e; continue
    raise RuntimeError(f"all RPCs failed for {method}: {last}")


def _clobauth_hashes(address: str, timestamp: str, nonce: int):
    """Return (domain_separator, contents_hash) via poly_eip712_structs — the same
    code path the SDK uses for normal ClobAuth signing, so these are authoritative."""
    from poly_eip712_structs import make_domain
    from py_clob_client_v2.signing.model import ClobAuth
    domain = make_domain(name="ClobAuthDomain", version="1", chainId=CHAIN_ID)
    msg = ClobAuth(address=address, timestamp=timestamp, nonce=nonce, message=MSG_TO_SIGN)
    signable = msg.signable_bytes(domain)  # b"\x19\x01" + domainSep(32) + structHash(32)
    return signable[2:34], signable[34:66], keccak(signable)


def build_wrapped_signature(pk: str, deposit_wallet: str, timestamp: str, nonce: int):
    """Solady ERC-7739 TypedDataSign signature over ClobAuth for the deposit wallet."""
    domain_sep, contents_hash, eip712_digest = _clobauth_hashes(deposit_wallet, timestamp, nonce)

    tds_struct_hash = keccak(primitive=abi_encode(
        ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "address", "bytes32"],
        [SOLADY_TYPE_HASH, contents_hash, DEPOSIT_WALLET_NAME_HASH,
         DEPOSIT_WALLET_VERSION_HASH, CHAIN_ID, deposit_wallet, ZERO_SALT],
    ))
    # ERC-7739 final digest the EOA actually signs: 0x1901 || appDomainSep || tdsStructHash
    sign_digest = keccak(primitive=b"\x19\x01" + domain_sep + tds_struct_hash)
    inner = Account._sign_hash(sign_digest, private_key=pk).signature.hex()
    if inner.startswith("0x"):
        inner = inner[2:]

    contents_type = CLOBAUTH_TYPE_STRING.encode("utf-8").hex()
    contents_type_len = len(CLOBAUTH_TYPE_STRING).to_bytes(2, "big").hex()
    wrapped = "0x" + inner + domain_sep.hex() + contents_hash.hex() + contents_type + contents_type_len
    # The hash the wallet/server verifies is the standard ClobAuth EIP-712 digest.
    return wrapped, eip712_digest


def isvalid_onchain(wallet: str, digest: bytes, signature: str) -> str | None:
    """eth_call wallet.isValidSignature(bytes32,bytes); return raw result or None."""
    sig_bytes = bytes.fromhex(signature[2:] if signature.startswith("0x") else signature)
    selector = "0x1626ba7e"
    data = selector + abi_encode(["bytes32", "bytes"], [digest, sig_bytes]).hex()
    try:
        return _rpc("eth_call", [{"to": wallet, "data": data}, "latest"])
    except Exception as e:  # noqa: BLE001
        print(f"   isValidSignature call failed: {e}")
        return None


def post_create_api_key(wallet: str, signature: str, timestamp: str, nonce: int):
    headers = {
        "POLY_ADDRESS": wallet,
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_NONCE": str(nonce),
    }
    for method, path in (("POST", CREATE_API_KEY), ("GET", DERIVE_API_KEY)):
        try:
            req = urllib.request.Request(f"{CLOB_HOST}{path}", headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=15) as r:
                print(f"   {method} {path} -> {r.status}: {r.read().decode()[:400]}")
                return True
        except urllib.error.HTTPError as e:
            print(f"   {method} {path} -> {e.code}: {e.read().decode()[:400]}")
        except Exception as e:  # noqa: BLE001
            print(f"   {method} {path} -> error: {e}")
    return False


def run() -> None:
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk or pk in ("0x...", ""):
        print("❌ POLYMARKET_PRIVATE_KEY not set"); return
    wallet = os.environ.get("DEPOSIT_WALLET", EXPECTED_WALLET)

    print("\n=== DECISIVE PROBE — ERC-1271 ClobAuth on /auth/api-key ===\n")
    print(f"Deposit wallet : {wallet}")

    # Must be deployed for ERC-1271 to verify.
    code = _rpc("eth_getCode", [wallet, "latest"]) or "0x"
    if code in ("0x", "0x0"):
        print("❌ Deposit wallet NOT deployed — run spike_deploy_deposit_wallet.py first.")
        return
    print(f"   deployed: ✅ ({len(code)//2 - 1} bytes)")

    timestamp = str(int(time.time()))
    nonce = 0
    wrapped, digest = build_wrapped_signature(pk, wallet, timestamp, nonce)
    print(f"\n[1] Wrapped ClobAuth signature built ({len(wrapped)//2 - 1} bytes).")
    print(f"    ClobAuth EIP-712 digest: 0x{digest.hex()}")

    print("\n[2] On-chain self-check: isValidSignature(digest, sig)...")
    res = isvalid_onchain(wallet, digest, wrapped)
    sig_ok = bool(res) and MAGIC_1271 in (res or "").lower()
    print(f"    -> {res}   {'✅ VALID (magic 0x1626ba7e)' if sig_ok else '❌ not valid'}")

    print("\n[3] POSTing L1 headers to the CLOB (POLY_ADDRESS = deposit wallet)...")
    accepted = post_create_api_key(wallet, wrapped, timestamp, nonce)

    print("\n=== VERDICT ===")
    if not sig_ok:
        print("Our 7739 wrapping is not accepted by the wallet itself — FIX the signature")
        print("before trusting the server result (re-check field order / domain).")
    elif accepted:
        print("🎉 Signature valid on-chain AND server accepted it — deposit-wallet auth WORKS.")
        print("→ Build createL1HeadersWrapped1271 into the SDK and proceed to a live order.")
    else:
        print("Signature provably valid on-chain, but the CLOB server REJECTED it.")
        print("→ Server does not support ERC-1271 on /auth/api-key. Blocked upstream; stay paper.")
        print("  (This is the expected outcome — record it and watch py-clob #91/#70, TS #65.)")


if __name__ == "__main__":
    run()
