use crate::parser::TxResult;
use anyhow::{anyhow, Result};
use serde_json::{json, Value};
use std::time::Duration;

/// JSON-RPC клиент с постоянными соединениями (keep-alive) — ради этого ядро и нужно.
#[derive(Clone)]
pub struct Rpc {
    pub http: reqwest::Client,
    pub url: String,
}

impl Rpc {
    pub fn new(url: String) -> Result<Self> {
        let http = reqwest::Client::builder()
            .tcp_nodelay(true)
            .pool_idle_timeout(Duration::from_secs(120))
            .pool_max_idle_per_host(8)
            .timeout(Duration::from_secs(12))
            .build()?;
        Ok(Self { http, url })
    }

    pub async fn call_url(&self, url: &str, method: &str, params: Value) -> Result<Value> {
        let body = json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params});
        let resp: Value = self.http.post(url).json(&body).send().await?.json().await?;
        if let Some(e) = resp.get("error") {
            return Err(anyhow!("{}: {}", method, e));
        }
        Ok(resp.get("result").cloned().unwrap_or(Value::Null))
    }

    pub async fn call(&self, method: &str, params: Value) -> Result<Value> {
        self.call_url(&self.url, method, params).await
    }

    pub async fn get_transaction(&self, sig: &str) -> Result<Option<TxResult>> {
        let v = self
            .call(
                "getTransaction",
                json!([sig, {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0
                }]),
            )
            .await?;
        if v.is_null() {
            return Ok(None);
        }
        Ok(Some(serde_json::from_value(v)?))
    }
}
