//! copybot-core: быстрое ядро. Говорит с Python через stdin/stdout (JSON, по одной строке).
//!
//! Python -> ядро:  {"cmd":"init", ...настройки}   (первая строка)
//!                  {"cmd":"quote","id":1,"in":"<mint>","out":"<mint>","amount":"123"}
//!                  {"cmd":"swap","id":2,"in":"<mint>","out":"<mint>","amount":"123","min_out":"456"}  (min_out необязателен)
//!                  {"cmd":"shutdown"}
//! ядро -> Python:  {"t":"ready","version":"..","pubkey":".."|null}
//!                  {"t":"swap","wallet":..,"side":"buy|sell","mint":..,"sol":..,"frac":..,"detect_ms":..}
//!                  {"t":"res","id":1,"ok":true,"out":"..","sig":..,"ms":..}  /  {"t":"res","id":1,"ok":false,"err":".."}
//!                  {"t":"log","level":"info","msg":".."}

mod exec;
mod out;
mod parser;
mod rpc;
mod types;
mod wallet;
mod watcher;

use anyhow::{anyhow, Result};
use serde::Deserialize;
use serde_json::json;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::mpsc;

#[derive(Deserialize)]
struct Cmd {
    cmd: String,
    #[serde(default)]
    id: u64,
    #[serde(default, rename = "in")]
    in_mint: String,
    #[serde(default)]
    out: String,
    #[serde(default)]
    amount: String,
    #[serde(default)]
    min_out: String,
    #[serde(default = "d_true")]
    check_impact: bool,
}

fn d_true() -> bool {
    true
}

#[tokio::main]
async fn main() -> Result<()> {
    // rustls 0.23 требует выбрать криптопровайдера; иначе при нескольких вариантах в сборке будет паника на TLS.
    let _ = rustls::crypto::ring::default_provider().install_default();

    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    let first = lines
        .next_line()
        .await?
        .ok_or_else(|| anyhow!("не получено init-сообщение"))?;
    let cfg: Arc<types::Config> = Arc::new(serde_json::from_str(&first)?);

    // единственный писатель в stdout
    let (tx, mut rx) = mpsc::unbounded_channel::<String>();
    let out = out::Out(tx);
    tokio::spawn(async move {
        let mut so = tokio::io::stdout();
        while let Some(line) = rx.recv().await {
            if so.write_all(line.as_bytes()).await.is_err() {
                break;
            }
            let _ = so.write_all(b"\n").await;
            let _ = so.flush().await;
        }
    });

    let rpc = rpc::Rpc::new(cfg.rpc_http.clone())?;
    let signer = if cfg.dry_run { None } else { Some(wallet::Signer::from_env()?) };
    let pubkey = signer.as_ref().map(|s| s.pubkey.clone());
    let exec = Arc::new(exec::Exec::new(cfg.clone(), rpc.clone(), signer));

    out.send(json!({"t": "ready", "version": env!("CARGO_PKG_VERSION"), "pubkey": pubkey}));

    tokio::spawn(watcher::run(cfg.clone(), rpc.clone(), out.clone()));
    tokio::spawn(exec::warmup(exec.clone()));

    // Конец stdin (Python закрылся) = завершаем работу, чтобы не оставаться «сиротой».
    while let Some(line) = lines.next_line().await? {
        let cmd: Cmd = match serde_json::from_str(&line) {
            Ok(c) => c,
            Err(e) => {
                out.log("warn", format!("не понял команду: {e}"));
                continue;
            }
        };
        let kind = cmd.cmd.clone();
        match kind.as_str() {
            "shutdown" => break,
            "quote" | "swap" => {
                let ex = exec.clone();
                let o = out.clone();
                tokio::spawn(async move {
                    handle_cmd(ex, o, cmd).await;
                });
            }
            other => out.log("warn", format!("неизвестная команда: {other}")),
        }
    }
    Ok(())
}

async fn handle_cmd(ex: Arc<exec::Exec>, out: out::Out, c: Cmd) {
    let res = if c.cmd == "quote" {
        ex.quote(&c.in_mint, &c.out, &c.amount)
            .await
            .map(|q| json!({"out": q["outAmount"], "impact": q["priceImpactPct"]}))
    } else {
        let min_out = c.min_out.parse::<u128>().ok().filter(|v| *v > 0);
        ex.swap(&c.in_mint, &c.out, &c.amount, min_out, c.check_impact)
            .await
            .map(|s| json!({"out": s.out, "sig": s.sig, "ms": s.ms}))
    };
    match res {
        Ok(mut v) => {
            v["t"] = json!("res");
            v["id"] = json!(c.id);
            v["ok"] = json!(true);
            out.send(v);
        }
        Err(e) => {
            let mut v = json!({"t": "res", "id": c.id, "ok": false, "err": e.to_string()});
            if e.downcast_ref::<exec::Skip>().is_some() {
                v["skip"] = json!(true);
            }
            if let Some(u) = e.downcast_ref::<exec::Unconfirmed>() {
                v["unconfirmed"] = json!(true);
                v["sig"] = json!(u.sig);
                v["out"] = json!(u.out);
            }
            if e.to_string().starts_with("нет маршрута") {
                v["no_route"] = json!(true);
            }
            out.send(v)
        }
    }
}
