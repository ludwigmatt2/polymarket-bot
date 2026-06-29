# Decisive deposit-wallet experiment — runbook

Goal: settle whether Polymarket V2 deposit-wallet go-live is achievable, by
deploying our deposit wallet and testing whether the CLOB server accepts an
ERC-1271 ClobAuth signature on `/auth/api-key`. Cost: ~$0 (relayer pays deploy
gas; no USDC needed for the auth probe).

Derived deposit wallet: `0xcee18163eeb650177161a7174b760cf71d45bc8a`
(EOA `0x9201…1C84`). Verdict-deciding step is #4.

## Step 1 — Generate builder creds (YOU, on the website) ~5 min
- Go to **polymarket.com/settings?tab=builder** (Builders tab), generate API creds.
- Self-serve, no partnership/approval. Starts at "Unverified" tier — fine for this.

## Step 2 — Configure (YOU) ~2 min
Add to `/opt/polymarket-bot/.env` (VPS) — or local `.env` for deploy:
```
BUILDER_API_KEY=...
BUILDER_SECRET=...
BUILDER_PASS_PHRASE=...
```
Then install the relayer client into the venv:
```
venv/bin/pip install py-builder-relayer-client
```

## Step 3 — Deploy the wallet  (not geoblocked; local or VPS)
```
venv/bin/python spike_deploy_deposit_wallet.py
```
Expect: cross-checks the derived address == `0xcee1…bc8a`, submits a relayer
deploy, waits for the tx. Re-run `spike_deposit_wallet.py` to confirm
`Deployed: yes`.

## Step 4 — THE decisive probe  (run from the Finland VPS — CLOB host geoblock)
```
venv/bin/python spike_clobauth_1271.py
```
It (a) builds a Solady ERC-7739-wrapped ClobAuth sig, (b) self-checks it on-chain
via `isValidSignature` (proves the sig is correct independent of the server),
then (c) POSTs L1 headers (`POLY_ADDRESS` = deposit wallet) to `/auth/api-key`.

### Reading the result
| isValidSignature | server `/auth/api-key` | Meaning |
|---|---|---|
| ✅ magic 0x1626ba7e | accepts | **Auth WORKS.** Build `createL1HeadersWrapped1271` patch → live order next. |
| ✅ magic 0x1626ba7e | rejects | Server doesn't support ERC-1271 auth. **Blocked upstream; stay paper.** (Expected.) |
| ❌ | — | Our wrapping is wrong; fix the signature before trusting the server result. |

## If GREEN (auth works) — remaining work to go live
1. ~50-line SDK patch: `createL1HeadersWrapped1271` (POLY_ADDRESS=funder, 7739 ClobAuth) in py-clob-client-v2 L1/L2 header path.
2. Fund the deposit wallet with a small USDC amount; set USDC→V2-exchange allowance (relayer `execute_deposit_wallet_batch` approve).
3. From the VPS, place a tiny `post_only` then a real ~$1 order (sig_type=3, funder=deposit wallet). Verify fill.
4. Wire sig_type=3 + funder + patched auth into `weather/live_trader.py` `_make_clob_client` + `weather/secrets.py`; migrate `users.json`; flip paper→live behind existing guards.

## If RED (rejected) — stop
Record the verdict, keep paper running, point the Monday fix-watcher at
py-clob-client-v2 #91/#70 and TS clob-client-v2 #65 (the server ERC-1271 question).
