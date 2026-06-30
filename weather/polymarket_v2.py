"""
Polymarket V2 on-chain constants — single source of truth (Polygon, chain 137).

All addresses verified on-chain June 2026; selectors verified against live bytecode.
Replaces the per-spike copies (spike_*.py) — import from here.
"""

from __future__ import annotations

CHAIN_ID = 137

# ── Tokens / collateral ──────────────────────────────────────────────────────
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"            # Polymarket USD (V2 collateral, 6 dec)
USDCE = "0x2791Bca1f2de4661ED88A30C99a7a9449Aa84174"           # USDC.e (what pUSD wraps)
PUSD_DECIMALS = 6

# ── Exchanges / settlement ───────────────────────────────────────────────────
EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"             # ConditionalTokens (ERC-1155)
EXCHANGES = [EXCHANGE_V2, NEG_RISK_EXCHANGE_V2, NEG_RISK_ADAPTER]

# ── Wrap / unwrap (USDC.e <-> pUSD) ──────────────────────────────────────────
ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"          # CollateralOnramp (wrap)
OFFRAMP = "0x2957922Eb93258b93368531d39fAcCA3B4dC5854"         # CollateralOfframp (unwrap)

# ── Deposit-wallet factory ───────────────────────────────────────────────────
DEPOSIT_WALLET_FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"

# ── Function selectors (bytecode-verified) ───────────────────────────────────
SEL_APPROVE = "0x095ea7b3"             # approve(address,uint256)
SEL_SET_APPROVAL_FOR_ALL = "0xa22cb465"  # setApprovalForAll(address,bool)
SEL_WRAP = "0x62355638"                # CollateralOnramp.wrap(address,address,uint256)
SEL_UNWRAP = "0x8cc7104f"              # CollateralOfframp.unwrap(address,address,uint256)
SEL_CTF_REDEEM = "0x01b7037c"          # CTF.redeemPositions(address,bytes32,bytes32,uint256[])
SEL_NEGRISK_REDEEM = "0xdbeccb23"      # NegRiskAdapter.redeemPositions(bytes32,uint256[])
SEL_NONCE = "0xaffed0e0"               # DepositWallet.nonce()
SEL_PREDICT_WALLET = "0x04f1d3c7"      # factory.predictWalletAddress(bytes32)

MAX_UINT = (1 << 256) - 1
ZERO32 = b"\x00" * 32

# Public Polygon RPCs (read-only, with fallback)
RPCS = [
    "https://polygon.llamarpc.com",
    "https://1rpc.io/matic",
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
]

MAINNET_RELAYER = "https://relayer-v2.polymarket.com/"
