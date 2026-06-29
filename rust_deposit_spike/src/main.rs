//! Rust re-verification spike — does `rs-clob-client-v2` 0.6.0-canary.1 place a
//! Polymarket V2 deposit-wallet (POLY_1271) order, or hit the SAME wall as the
//! Python SDK?
//!
//! WHY
//! ---
//! Web research claimed the Rust client handles deposit-wallet L1 auth correctly;
//! a Jun 29 2026 hands-on test of rs-clob-client-v2 0.5.1 said it hit the same
//! bug (#70). This canary (0.6.0-canary.1) is NEWER than 0.5.1, so it must be
//! re-checked. Source reading already shows the bug persists:
//!   - L1 auth (`src/auth.rs::l1::create_headers`) sets POLY_ADDRESS =
//!     signer.address() UNCONDITIONALLY -> API key binds to the EOA.
//!   - For Poly1271 the order builder sets BOTH maker AND signer = funder
//!     (deposit wallet) (`src/clob/order_builder.rs::build_payload`).
//!   - So order.signer (deposit wallet) != API-key identity (EOA) -> the server's
//!     "order signer address has to be the address of the API KEY" rejection (#75).
//! This binary confirms that at RUNTIME against the live API.
//!
//! WHAT IT DOES
//!   1. Authenticate with funder = our derived deposit wallet + Poly1271.
//!      (create_or_derive_api_key binds the key to the EOA — that's the point.)
//!   2. Fetch a live token from sampling markets.
//!   3. Build + sign a POLY_1271 limit BUY, post_only, priced far below market so
//!      it can NEVER fill. Post it and classify the response/error.
//!
//! OUTWARD ACTION: this hits create-api-key and POST /order on Polymarket. The
//! order is post_only + far from market (won't fill) and is expected to be
//! rejected at the auth/maker layer. Run only when you intend to live-probe.
//!
//! Toolchain: needs Rust >= 1.88 (edition 2024). `rustup update stable` first.
//! Run: POLYMARKET_PRIVATE_KEY=0x... cargo run --release
//! Optional override: DEPOSIT_WALLET=0x... (defaults to our derived wallet).

use std::str::FromStr as _;

use alloy::signers::Signer as _;
use alloy::signers::local::LocalSigner;
use polymarket_client_sdk_v2::clob::types::{OrderType, Side, SignatureType};
use polymarket_client_sdk_v2::clob::{Client, Config};
use polymarket_client_sdk_v2::types::{Address, U256};
use polymarket_client_sdk_v2::{POLYGON, PRIVATE_KEY_VAR};
use rust_decimal_macros::dec;

/// Our account's derived V2 deposit wallet (from spike_deposit_wallet.py).
const DEPOSIT_WALLET: &str = "0xcee18163eeb650177161a7174b760cf71d45bc8a";
const HOST: &str = "https://clob-v2.polymarket.com";
/// Sampling endpoints to try for a live token (host varies between deployments).
const SAMPLING_URLS: &[&str] = &[
    "https://clob-v2.polymarket.com/sampling-simplified-markets",
    "https://clob.polymarket.com/sampling-simplified-markets",
];

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let pk = match std::env::var(PRIVATE_KEY_VAR) {
        Ok(v) if !v.is_empty() => v,
        _ => {
            eprintln!("❌ Set {PRIVATE_KEY_VAR}=0x... before running.");
            return Ok(());
        }
    };
    let signer = LocalSigner::from_str(&pk)?.with_chain_id(Some(POLYGON));
    let eoa = signer.address();
    let dw = std::env::var("DEPOSIT_WALLET").unwrap_or_else(|_| DEPOSIT_WALLET.to_owned());
    let deposit = Address::from_str(&dw)?;

    println!("\n=== RUST DEPOSIT-WALLET RE-VERIFICATION (rs-clob-client-v2 0.6 canary) ===\n");
    println!("Owner EOA      : {eoa}");
    println!("Deposit wallet : {deposit}  (order maker + signer for POLY_1271)");

    // ── 1. Authenticate as POLY_1271 with the deposit wallet as funder ───────
    println!("\n[1/3] Authenticating (funder = deposit wallet, signature_type = Poly1271)...");
    let config = Config::builder().use_server_time(true).build();
    let client = match Client::new(HOST, config)?
        .authentication_builder(&signer)
        .funder(deposit)
        .signature_type(SignatureType::Poly1271)
        .authenticate()
        .await
    {
        Ok(c) => {
            println!("      ✅ authenticated — but note: the API key is bound to the EOA");
            println!("         ({eoa}), NOT the deposit wallet. That is the crux of the bug.");
            c
        }
        Err(e) => {
            println!("      ❌ authenticate() failed: {e}");
            classify(&e.to_string());
            return Ok(());
        }
    };

    // ── 2. Find a live token to price an order against ───────────────────────
    println!("\n[2/3] Fetching a live token from sampling markets...");
    let (token_id, market_price) = match fetch_active_token().await {
        Some(t) => t,
        None => {
            println!("      ❌ no live token found; cannot build an order. Aborting.");
            return Ok(());
        }
    };
    println!("      token={token_id}  market≈{market_price:.3f}");

    // ── 3. Build → sign → post a far-below-market, post-only BUY ─────────────
    println!("\n[3/3] Building + signing + posting a POLY_1271 limit BUY (post_only, won't fill)...");
    let order = client
        .limit_order()
        .token_id(U256::from_str(&token_id)?)
        .price(dec!(0.10)) // far below market (tokens chosen with market > 0.20)
        .size(dec!(5)) // small, at/above typical min order size
        .side(Side::Buy)
        .order_type(OrderType::GTC)
        .post_only(true)
        .build()
        .await?;
    let signed = client.sign(&signer, order).await?;

    match client.post_order(signed).await {
        Ok(r) => {
            println!(
                "      response: success={} status={:?} order_id={} error_msg={:?}",
                r.success, r.status, r.order_id, r.error_msg
            );
            if r.success {
                println!("\n🎉 UNEXPECTED: order accepted — Rust deposit-wallet path WORKS.");
                println!("   Cancel order_id={} in the UI. Re-evaluate going live via Rust.", r.order_id);
            } else {
                classify(r.error_msg.as_deref().unwrap_or(""));
            }
        }
        Err(e) => {
            println!("      post_order error: {e}");
            classify(&e.to_string());
        }
    }
    println!();
    Ok(())
}

/// Map the server message to a verdict on the conflict.
fn classify(msg: &str) {
    let m = msg.to_lowercase();
    println!("\n=== VERDICT ===");
    if m.contains("maker address not allowed") || m.contains("deposit wallet flow") {
        println!("SAME WALL AS PYTHON: V2 rejects the maker. Rust does not fix it.");
        println!("→ Conflict resolved in favour of the Jun 29 test. Stay on paper / wait for SDK.");
    } else if m.contains("signer") && m.contains("api key") {
        println!("EOA-BINDING BUG (#75): order.signer = deposit wallet but the API key is");
        println!("bound to the EOA. Rust 0.6-canary has the IDENTICAL bug to Python.");
        println!("→ Conflict resolved in favour of the Jun 29 test. Stay on paper / wait for SDK.");
    } else if m.contains("balance") || m.contains("allowance") || m.contains("not deployed")
        || m.contains("funds")
    {
        println!("PAST THE AUTH WALL — rejected on funding/deployment, not on signer binding.");
        println!("→ Rust auth MAY work. Next: deploy + fund the deposit wallet, then retry.");
    } else {
        println!("Unclassified server message (record it verbatim):");
        println!("  {msg}");
    }
}

/// GET sampling markets and return (token_id, price) for a token with a market
/// price > 0.20 (so a 0.10 bid is both below market and tick-valid).
async fn fetch_active_token() -> Option<(String, f64)> {
    let http = reqwest::Client::new();
    for url in SAMPLING_URLS {
        let Ok(resp) = http.get(*url).send().await else { continue };
        let Ok(json) = resp.json::<serde_json::Value>().await else { continue };
        let rows = json.get("data").and_then(|d| d.as_array()).cloned().unwrap_or_default();
        for mk in rows {
            let Some(tokens) = mk.get("tokens").and_then(|t| t.as_array()) else { continue };
            for t in tokens {
                let price = t
                    .get("price")
                    .and_then(|p| p.as_f64().or_else(|| p.as_str().and_then(|s| s.parse().ok())))
                    .unwrap_or(0.0);
                let token_id = t.get("token_id").and_then(|v| v.as_str()).unwrap_or("");
                if !token_id.is_empty() && (0.20..0.95).contains(&price) {
                    return Some((token_id.to_owned(), price));
                }
            }
        }
    }
    None
}
