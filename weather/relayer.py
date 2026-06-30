"""
Gasless on-chain operations for a Polymarket V2 deposit wallet, via Polymarket's
relayer (py-builder-relayer-client). One module for wrap / approve / redeem /
unwrap — the deposit wallet calls each op; the relayer pays gas.

All batches auto-read the wallet's on-chain nonce() (the relayer rejects a stale
nonce). Builder creds come from the env (BUILDER_API_KEY/SECRET/PASS_PHRASE) — one
set authorises the relayer request for any wallet; each batch is authorised by the
owner pk signature.

Consolidates the proven logic from spike_wrap/approve/redeem/sell scripts.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

from . import polymarket_v2 as pm


# ── read helpers (public RPC, fallback) ──────────────────────────────────────
def _rpc(method: str, params: list):
    for ep in pm.RPCS:
        try:
            req = urllib.request.Request(ep, data=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                o = json.loads(r.read())
            if o.get("result") is not None:
                return o["result"]
        except Exception:
            pass
    return None


def onchain_nonce(wallet: str) -> int:
    r = _rpc("eth_call", [{"to": wallet, "data": pm.SEL_NONCE}, "latest"])
    return int(r, 16) if r and r != "0x" else 0


def pusd_balance(wallet: str) -> float:
    r = _rpc("eth_call", [{"to": pm.PUSD,
             "data": "0x70a08231" + wallet.lower().replace("0x", "").zfill(64)}, "latest"])
    return int(r, 16) / 10 ** pm.PUSD_DECIMALS if r and r != "0x" else 0.0


def is_deployed(wallet: str) -> bool:
    code = _rpc("eth_getCode", [wallet, "latest"]) or "0x"
    return code not in ("0x", "0x0")


def derive_deposit_wallet(eoa: str) -> str | None:
    """The deterministic V2 deposit-wallet address for an owner EOA, via the
    factory's predictWalletAddress(bytes32) read fn (authoritative — equals the
    CREATE2 result). Pure RPC, no builder creds. Returns checksum-less lower hex."""
    wallet_id = eoa.lower().replace("0x", "").zfill(64)
    r = _rpc("eth_call", [{"to": pm.DEPOSIT_WALLET_FACTORY,
             "data": pm.SEL_PREDICT_WALLET + wallet_id}, "latest"])
    if not r or r == "0x" or int(r, 16) == 0:
        return None
    return "0x" + r[-40:]


# ── calldata builders ────────────────────────────────────────────────────────
def _approve(spender: str, amount: int) -> str:
    return pm.SEL_APPROVE + encode(["address", "uint256"],
                                   [to_checksum_address(spender), amount]).hex()


def _set_approval_for_all(operator: str, approved: bool) -> str:
    return pm.SEL_SET_APPROVAL_FOR_ALL + encode(["address", "bool"],
                                                [to_checksum_address(operator), approved]).hex()


def _wrap(asset: str, to: str, amount: int) -> str:
    return pm.SEL_WRAP + encode(["address", "address", "uint256"],
                                [to_checksum_address(asset), to_checksum_address(to), amount]).hex()


def _unwrap(asset: str, to: str, amount: int) -> str:
    return pm.SEL_UNWRAP + encode(["address", "address", "uint256"],
                                  [to_checksum_address(asset), to_checksum_address(to), amount]).hex()


def _ctf_redeem(condition_id: str) -> str:
    return pm.SEL_CTF_REDEEM + encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [to_checksum_address(pm.PUSD), pm.ZERO32,
         bytes.fromhex(condition_id.replace("0x", "")), [1, 2]]).hex()


def _negrisk_redeem(condition_id: str, amounts: list[int]) -> str:
    return pm.SEL_NEGRISK_REDEEM + encode(
        ["bytes32", "uint256[]"],
        [bytes.fromhex(condition_id.replace("0x", "")), amounts]).hex()


class RelayerClient:
    """Thin wrapper over py-builder-relayer-client for one deposit wallet."""

    def __init__(self, pk: str, *, relayer_url: str | None = None,
                 builder_key: str | None = None, builder_secret: str | None = None,
                 builder_passphrase: str | None = None):
        self._pk = pk
        self._relayer_url = relayer_url or os.getenv("RELAYER_URL", pm.MAINNET_RELAYER)
        self._bkey = builder_key or os.getenv("BUILDER_API_KEY")
        self._bsecret = builder_secret or os.getenv("BUILDER_SECRET")
        self._bpass = builder_passphrase or os.getenv("BUILDER_PASS_PHRASE")
        self._client = None

    def _relay(self):
        if self._client is None:
            from py_builder_relayer_client.client import RelayClient
            from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds
            if not (self._bkey and self._bsecret and self._bpass):
                raise RuntimeError("Missing BUILDER_API_KEY/SECRET/PASS_PHRASE for the relayer")
            self._client = RelayClient(
                self._relayer_url, pm.CHAIN_ID, self._pk,
                BuilderConfig(local_builder_creds=BuilderApiKeyCreds(
                    key=self._bkey, secret=self._bsecret, passphrase=self._bpass)))
        return self._client

    def _batch(self, wallet: str, datas: list[tuple[str, str]]):
        """Submit (target, calldata) pairs as one gasless batch from `wallet`."""
        from py_builder_relayer_client.models import DepositWalletCall
        calls = [DepositWalletCall(target=to_checksum_address(t), value="0",
                                   data=d if d.startswith("0x") else "0x" + d)
                 for (t, d) in datas]
        resp = self._relay().execute_deposit_wallet_batch(
            calls=calls, wallet_address=to_checksum_address(wallet),
            nonce=str(onchain_nonce(wallet)),
            deadline=str(int(time.time()) + 240))
        try:
            return resp.wait()
        except Exception:
            return resp

    # ── high-level ops ───────────────────────────────────────────────────────
    def wrap_usdce_to_pusd(self, wallet: str, amount_raw: int):
        return self._batch(wallet, [
            (pm.USDCE, _approve(pm.ONRAMP, amount_raw)),
            (pm.ONRAMP, _wrap(pm.USDCE, wallet, amount_raw)),
        ])

    def approve_exchanges(self, wallet: str):
        datas = [(pm.PUSD, _approve(s, pm.MAX_UINT)) for s in pm.EXCHANGES]
        datas += [(pm.CTF, _set_approval_for_all(s, True)) for s in pm.EXCHANGES]
        return self._batch(wallet, datas)

    def unwrap_pusd_to_usdce(self, wallet: str, amount_raw: int, to: str):
        """Withdraw: pUSD -> USDC.e sent to `to` (external address or EOA)."""
        return self._batch(wallet, [
            (pm.PUSD, _approve(pm.OFFRAMP, amount_raw)),
            (pm.OFFRAMP, _unwrap(pm.USDCE, to, amount_raw)),
        ])

    def redeem_positions(self, wallet: str, grouped: list[dict]):
        """`grouped`: [{'condition_id','neg_risk','amounts':[yes,no]}]. Routes
        CTF (binary) vs NegRiskAdapter (neg-risk). Caller pre-groups by condition."""
        datas = []
        for g in grouped:
            if g["neg_risk"]:
                datas.append((pm.NEG_RISK_ADAPTER, _negrisk_redeem(g["condition_id"], g["amounts"])))
            else:
                datas.append((pm.CTF, _ctf_redeem(g["condition_id"])))
        return self._batch(wallet, datas) if datas else None
