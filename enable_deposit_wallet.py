"""
Wire a user onto the V2 deposit-wallet flow: derive their deposit wallet from the
stored pk, store it as funder_address + signature_type=3 (POLY_1271).

Usage (VPS):
  venv/bin/python enable_deposit_wallet.py            # uses ADMIN_ID from env
  venv/bin/python enable_deposit_wallet.py <uid>

Only wires creds. The deposit wallet must be DEPLOYED + FUNDED + APPROVED
separately (spike_deploy/wrap/approve, or the onboarding flow) before live orders
fill. Prints the deployed state so you know what's left.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("ADMIN_ID", "0"))
    if not uid:
        print("❌ No uid given and ADMIN_ID not set."); return

    from weather.secrets import enable_deposit_wallet, get_user_creds
    if not get_user_creds(uid):
        print(f"❌ No stored creds for uid={uid}. Onboard first."); return

    res = enable_deposit_wallet(uid)
    print(f"✅ uid={uid} wired to deposit wallet:")
    print(f"   funder_address : {res['funder_address']}")
    print(f"   signature_type : {res['signature_type']} (POLY_1271)")
    print(f"   deployed       : {'yes' if res['deployed'] else 'NO — deploy + fund + approve before live'}")
    print(f"   clob L2 creds  : {'ready' if res.get('clob_ready') else 'NOT derived — run derive_and_store_clob_creds'}")


if __name__ == "__main__":
    main()
