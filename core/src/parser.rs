//! Разбор транзакции цели: простой своп SOL <-> один токен.
//! Логика 1-в-1 как в Python (copybot/strategy/parser.py).

use serde::Deserialize;
use std::collections::HashMap;

pub const WSOL: &str = "So11111111111111111111111111111111111111112";
const LAMPORTS: f64 = 1_000_000_000.0;

#[derive(Deserialize)]
pub struct TxResult {
    pub transaction: TxBody,
    pub meta: Option<Meta>,
    #[serde(rename = "blockTime", default)]
    pub block_time: Option<i64>,
}

#[derive(Deserialize)]
pub struct TxBody {
    pub message: TxMessage,
}

#[derive(Deserialize)]
pub struct TxMessage {
    #[serde(rename = "accountKeys")]
    pub account_keys: Vec<AccountKey>,
}

#[derive(Deserialize)]
pub struct AccountKey {
    pub pubkey: String,
}

#[derive(Deserialize)]
pub struct Meta {
    pub err: Option<serde_json::Value>,
    pub fee: u64,
    #[serde(rename = "preBalances")]
    pub pre_balances: Vec<u64>,
    #[serde(rename = "postBalances")]
    pub post_balances: Vec<u64>,
    #[serde(rename = "preTokenBalances", default)]
    pub pre_token_balances: Option<Vec<TokenBal>>,
    #[serde(rename = "postTokenBalances", default)]
    pub post_token_balances: Option<Vec<TokenBal>>,
}

#[derive(Deserialize)]
pub struct TokenBal {
    pub mint: String,
    #[serde(default)]
    pub owner: Option<String>,
    #[serde(rename = "uiTokenAmount")]
    pub ui: UiAmount,
}

#[derive(Deserialize)]
pub struct UiAmount {
    pub amount: String,
}

pub struct Swap {
    pub side: &'static str,
    pub mint: String,
    pub sol: f64,
    pub frac: Option<f64>,
    /// Сколько токенов (в минимальных единицах) купила/продала цель.
    pub tokens: String,
}

fn owned(bals: &Option<Vec<TokenBal>>, wallet: &str) -> HashMap<String, i128> {
    let mut m: HashMap<String, i128> = HashMap::new();
    if let Some(list) = bals {
        for b in list {
            if b.owner.as_deref() == Some(wallet) {
                let amt = b.ui.amount.parse::<i128>().unwrap_or(0);
                *m.entry(b.mint.clone()).or_insert(0) += amt;
            }
        }
    }
    m
}

pub fn parse_swap(tx: &TxResult, wallet: &str) -> Option<Swap> {
    let meta = tx.meta.as_ref()?;
    if meta.err.is_some() {
        return None;
    }
    let idx = tx
        .transaction
        .message
        .account_keys
        .iter()
        .position(|k| k.pubkey == wallet)?;
    let pre_b = *meta.pre_balances.get(idx)? as i128;
    let post_b = *meta.post_balances.get(idx)? as i128;
    let mut sol: i128 = post_b - pre_b;
    if idx == 0 {
        sol += meta.fee as i128; // fee payer: убираем комиссию из дельты
    }

    let pre = owned(&meta.pre_token_balances, wallet);
    let post = owned(&meta.post_token_balances, wallet);

    // mint -> (изменение, баланс до)
    let mut deltas: HashMap<String, (i128, i128)> = HashMap::new();
    for mint in pre.keys().chain(post.keys()) {
        let before = *pre.get(mint).unwrap_or(&0);
        let after = *post.get(mint).unwrap_or(&0);
        if after != before {
            deltas.insert(mint.clone(), (after - before, before));
        }
    }
    if let Some((d, _)) = deltas.remove(WSOL) {
        sol += d; // wrapped SOL
    }
    if deltas.len() != 1 {
        return None; // несколько токенов / стейбл-ноги / не своп
    }
    let (mint, (d, before)) = deltas.into_iter().next()?;

    if d > 0 && sol < 0 {
        Some(Swap {
            side: "buy",
            mint,
            sol: (-sol) as f64 / LAMPORTS,
            frac: None,
            tokens: d.to_string(),
        })
    } else if d < 0 && sol > 0 {
        let frac = if before > 0 {
            ((-d) as f64 / before as f64).min(1.0)
        } else {
            1.0
        };
        Some(Swap {
            side: "sell",
            mint,
            sol: sol as f64 / LAMPORTS,
            frac: Some(frac),
            tokens: (-d).to_string(),
        })
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn tx(pre_tok: serde_json::Value, post_tok: serde_json::Value, pre: u64, post: u64) -> TxResult {
        serde_json::from_value(json!({
            "transaction": {"message": {"accountKeys": [{"pubkey": "W"}]}},
            "meta": {"err": null, "fee": 5000, "preBalances": [pre], "postBalances": [post],
                     "preTokenBalances": pre_tok, "postTokenBalances": post_tok}
        }))
        .unwrap()
    }

    #[test]
    fn buy_is_detected() {
        let t = tx(
            json!([]),
            json!([{"mint": "MINT", "owner": "W", "uiTokenAmount": {"amount": "500"}}]),
            2_000_000_000,
            999_995_000,
        );
        let s = parse_swap(&t, "W").unwrap();
        assert_eq!(s.side, "buy");
        assert_eq!(s.mint, "MINT");
        assert!((s.sol - 1.0).abs() < 1e-9);
        assert_eq!(s.tokens, "500");
    }

    #[test]
    fn sell_fraction_is_computed() {
        let t = tx(
            json!([{"mint": "MINT", "owner": "W", "uiTokenAmount": {"amount": "1000"}}]),
            json!([{"mint": "MINT", "owner": "W", "uiTokenAmount": {"amount": "400"}}]),
            1_000_000_000,
            1_999_995_000,
        );
        let s = parse_swap(&t, "W").unwrap();
        assert_eq!(s.side, "sell");
        assert!((s.frac.unwrap() - 0.6).abs() < 1e-9);
        assert!((s.sol - 1.0).abs() < 1e-9);
    }

    #[test]
    fn foreign_wallet_is_ignored() {
        let t = tx(json!([]), json!([]), 1, 1);
        assert!(parse_swap(&t, "X").is_none());
    }
}
