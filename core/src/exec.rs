//! Исполнение: котировка Jupiter -> swap -> подпись -> отправка (на несколько RPC) -> подтверждение.

use crate::{parser::WSOL, rpc::Rpc, types::Config, wallet::Signer};
use anyhow::{anyhow, bail, Result};
use serde_json::{json, Value};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::mpsc;

/// Сделку сознательно не делаем (цена ушла, слишком большой price impact на покупку): не авария.
#[derive(Debug)]
pub struct Skip(pub String);
impl std::fmt::Display for Skip {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}
impl std::error::Error for Skip {}

/// Транзакция отправлена, но подтверждение не пришло вовремя: она могла пройти позже.
#[derive(Debug)]
pub struct Unconfirmed {
    pub sig: String,
    pub secs: u64,
    pub out: String,
}
impl std::fmt::Display for Unconfirmed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "транзакция не подтвердилась за {} с (подпись {})", self.secs, self.sig)
    }
}
impl std::error::Error for Unconfirmed {}

const USDC: &str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";

pub struct SwapOut {
    pub out: String,
    pub sig: Option<String>,
    pub ms: u64,
}

pub struct Exec {
    pub cfg: Arc<Config>,
    pub rpc: Rpc,
    signer: Option<Signer>,
}

impl Exec {
    pub fn new(cfg: Arc<Config>, rpc: Rpc, signer: Option<Signer>) -> Self {
        Self { cfg, rpc, signer }
    }

    /// Добавляет ключ Jupiter, если он задан.
    fn jup_req(&self, rb: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        if self.cfg.jupiter_api_key.is_empty() {
            rb
        } else {
            rb.header("x-api-key", self.cfg.jupiter_api_key.as_str())
        }
    }

    fn check_status(status: u16) -> Result<()> {
        match status {
            401 | 403 => bail!("Jupiter отклонил запрос (HTTP {status}): для api.jup.ag нужен бесплатный ключ (portal.jup.ag)"),
            429 => bail!("Jupiter: слишком много запросов (HTTP 429), лимит тарифа исчерпан"),
            500..=599 => bail!("Jupiter: ошибка сервера (HTTP {status}), повторю позже"),
            _ => Ok(()),
        }
    }

    pub async fn quote(&self, in_mint: &str, out_mint: &str, amount: &str) -> Result<Value> {
        let slip = self.cfg.slippage_bps.to_string();
        let resp = self
            .jup_req(self.rpc.http.get(format!("{}/quote", self.cfg.jupiter_url)))
            .query(&[
                ("inputMint", in_mint),
                ("outputMint", out_mint),
                ("amount", amount),
                ("slippageBps", slip.as_str()),
            ])
            .send()
            .await?;
        Self::check_status(resp.status().as_u16())?;
        let q: Value = resp.json().await?;
        if q.get("outAmount").is_none() {
            bail!("нет маршрута: {}", q);
        }
        Ok(q)
    }

    pub async fn swap(
        &self,
        in_mint: &str,
        out_mint: &str,
        amount: &str,
        min_out: Option<u128>,
        check_impact: bool,
    ) -> Result<SwapOut> {
        let t0 = Instant::now();
        let q = self.quote(in_mint, out_mint, amount).await?;
        let out = q["outAmount"].as_str().unwrap_or("0").to_string();
        let impact = q["priceImpactPct"]
            .as_str()
            .and_then(|s| s.parse::<f64>().ok())
            .unwrap_or(0.0)
            * 100.0;
        // лимит price impact — только для покупок: из позиции выйти важнее
        if check_impact && impact > self.cfg.max_price_impact_pct {
            return Err(Skip(format!("price impact {:.2}% больше лимита", impact)).into());
        }
        if let Some(m) = min_out {
            let got: u128 = out.parse().unwrap_or(0);
            if got < m {
                return Err(Skip(format!("цена уже хуже цены цели (получим {}, нужно не меньше {})", got, m)).into());
            }
        }
        if self.cfg.dry_run {
            return Ok(SwapOut {
                out,
                sig: None,
                ms: t0.elapsed().as_millis() as u64,
            });
        }

        let signer = self.signer.as_ref().ok_or_else(|| anyhow!("нет ключа для подписи"))?;
        let fee = if self.cfg.jito_tip_lamports > 0 {
            json!({"jitoTipLamports": self.cfg.jito_tip_lamports})
        } else {
            json!({"priorityLevelWithMaxLamports": {
                "priorityLevel": "high",
                "maxLamports": self.cfg.max_priority_fee_lamports
            }})
        };
        let body = json!({
            "quoteResponse": q,
            "userPublicKey": signer.pubkey,
            "dynamicComputeUnitLimit": true,
            "prioritizationFeeLamports": fee
        });
        let resp = self
            .jup_req(self.rpc.http.post(format!("{}/swap", self.cfg.jupiter_url)))
            .json(&body)
            .send()
            .await?;
        Self::check_status(resp.status().as_u16())?;
        let sw: Value = resp.json().await?;
        let tx_b64 = sw["swapTransaction"]
            .as_str()
            .ok_or_else(|| anyhow!("Jupiter не вернул транзакцию: {}", sw))?;
        let signed = signer.sign_tx_b64(tx_b64)?;
        let sig = self.send_all(&signed).await?;
        self.confirm(&sig, &out).await?;
        Ok(SwapOut {
            out,
            sig: Some(sig),
            ms: t0.elapsed().as_millis() as u64,
        })
    }

    /// Отправка на основной RPC и все `send_rpcs` параллельно; берём первый успешный ответ.
    async fn send_all(&self, signed_b64: &str) -> Result<String> {
        let mut urls = vec![self.rpc.url.clone()];
        urls.extend(self.cfg.send_rpcs.iter().cloned());
        let params = json!([signed_b64, {"encoding": "base64", "skipPreflight": true, "maxRetries": 3}]);
        let (tx, mut rx) = mpsc::unbounded_channel::<Result<String>>();
        let n = urls.len();
        for url in urls {
            let rpc = self.rpc.clone();
            let p = params.clone();
            let tx = tx.clone();
            tokio::spawn(async move {
                let r = rpc
                    .call_url(&url, "sendTransaction", p)
                    .await
                    .and_then(|v| v.as_str().map(|s| s.to_string()).ok_or_else(|| anyhow!("пустой ответ")));
                let _ = tx.send(r);
            });
        }
        drop(tx);
        let mut last = anyhow!("не удалось отправить транзакцию");
        for _ in 0..n {
            match rx.recv().await {
                Some(Ok(sig)) => return Ok(sig),
                Some(Err(e)) => last = e,
                None => break,
            }
        }
        Err(last)
    }

    async fn confirm(&self, sig: &str, out: &str) -> Result<()> {
        let deadline = Instant::now() + Duration::from_secs(self.cfg.confirm_timeout_sec);
        while Instant::now() < deadline {
            if let Ok(v) = self
                .rpc
                .call("getSignatureStatuses", json!([[sig], {"searchTransactionHistory": false}]))
                .await
            {
                if let Some(st) = v["value"][0].as_object() {
                    if st.get("err").map(|e| !e.is_null()).unwrap_or(false) {
                        bail!("транзакция не прошла в сети: {}", st["err"]);
                    }
                    match st.get("confirmationStatus").and_then(|x| x.as_str()) {
                        Some("confirmed") | Some("finalized") => return Ok(()),
                        _ => {}
                    }
                }
            }
            tokio::time::sleep(Duration::from_millis(300)).await;
        }
        Err(Unconfirmed {
            sig: sig.to_string(),
            secs: self.cfg.confirm_timeout_sec,
            out: out.to_string(),
        }
        .into())
    }
}

/// Держит соединения «тёплыми», чтобы первая реальная сделка не тратила время на TLS-рукопожатие.
pub async fn warmup(ex: Arc<Exec>) {
    let every = ex.cfg.warmup_sec;
    if every == 0 {
        return;
    }
    loop {
        tokio::time::sleep(Duration::from_secs(every)).await;
        let _ = ex.rpc.call("getSlot", json!([])).await;
        let _ = ex.quote(WSOL, USDC, "1000000").await;
    }
}
