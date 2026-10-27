//! WebSocket-слежение за кошельками (logsSubscribe) + мгновенный разбор свопа.

use crate::{out::Out, parser::parse_swap, rpc::Rpc, types::Config};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio_tungstenite::{connect_async, tungstenite::Message};

type Seen = Arc<Mutex<HashSet<String>>>;
/// кошелёк -> последняя увиденная подпись (чтобы после обрыва WebSocket догнать пропущенное)
type LastSig = Arc<Mutex<HashMap<String, String>>>;

const CATCHUP_LIMIT: u32 = 25;
const CATCHUP_MAX_BUY_AGE_SEC: i64 = 60; // старые покупки при догоне не копируем: цена уже ушла

pub async fn run(cfg: Arc<Config>, rpc: Rpc, out: Out) {
    let seen: Seen = Arc::new(Mutex::new(HashSet::new()));
    let last_sig: LastSig = Arc::new(Mutex::new(HashMap::new()));
    loop {
        if let Err(e) = session(&cfg, &rpc, &out, &seen, &last_sig).await {
            out.log("warn", format!("WebSocket упал ({e}), переподключаюсь через 3с"));
        }
        tokio::time::sleep(Duration::from_secs(3)).await;
    }
}

async fn session(cfg: &Arc<Config>, rpc: &Rpc, out: &Out, seen: &Seen, last_sig: &LastSig) -> anyhow::Result<()> {
    let (ws, _) = connect_async(cfg.rpc_ws.as_str()).await?;
    let (mut write, mut read) = ws.split();

    let mut req_wallet: HashMap<u64, String> = HashMap::new();
    for (n, w) in cfg.targets.iter().enumerate() {
        let id = n as u64 + 1;
        let req = json!({
            "jsonrpc": "2.0", "id": id, "method": "logsSubscribe",
            "params": [{"mentions": [w]}, {"commitment": "confirmed"}]
        });
        write.send(Message::Text(req.to_string())).await?;
        req_wallet.insert(id, w.clone());
    }
    out.log("info", format!("слежу за {} кошельками", req_wallet.len()));

    // после переподключения: что цель успела сделать, пока соединения не было
    let known: Vec<(String, String)> = {
        let m = last_sig.lock().unwrap();
        m.iter().map(|(w, s)| (w.clone(), s.clone())).collect()
    };
    for (w, until) in known {
        let (rpc2, out2, seen2, ls2) = (rpc.clone(), out.clone(), seen.clone(), last_sig.clone());
        tokio::spawn(async move {
            catch_up(rpc2, out2, seen2, ls2, w, until).await;
        });
    }

    let mut sub_wallet: HashMap<u64, String> = HashMap::new();
    let mut ping = tokio::time::interval(Duration::from_secs(20));
    let mut last_rx = Instant::now();

    loop {
        tokio::select! {
            msg = read.next() => {
                let msg = match msg {
                    Some(m) => m?,
                    None => return Err(anyhow::anyhow!("соединение закрыто")),
                };
                let t0 = Instant::now();
                last_rx = t0; // любое сообщение (в т.ч. pong) = соединение живое
                match msg {
                    Message::Text(txt) => {
                        let v: Value = match serde_json::from_str(&txt) {
                            Ok(v) => v,
                            Err(_) => continue,
                        };
                        let confirm = (
                            v.get("id").and_then(|x| x.as_u64()),
                            v.get("result").and_then(|x| x.as_u64()),
                        );
                        if let (Some(id), Some(sub)) = confirm {
                            // ответ на logsSubscribe: запоминаем, какой подписке какой кошелёк
                            if let Some(w) = req_wallet.get(&id) {
                                sub_wallet.insert(sub, w.clone());
                            }
                        } else if v.get("method").and_then(|m| m.as_str()) == Some("logsNotification") {
                            let p = &v["params"];
                            let sub = p["subscription"].as_u64().unwrap_or(0);
                            let val = &p["result"]["value"];
                            if !val["err"].is_null() {
                                continue;
                            }
                            let sig = match val["signature"].as_str() {
                                Some(s) => s.to_string(),
                                None => continue,
                            };
                            if let Some(w) = sub_wallet.get(&sub) {
                                last_sig.lock().unwrap().insert(w.clone(), sig.clone());
                                let (rpc2, out2, seen2, w2) = (rpc.clone(), out.clone(), seen.clone(), w.clone());
                                tokio::spawn(async move {
                                    handle(rpc2, out2, seen2, w2, sig, t0, false).await;
                                });
                            }
                        }
                    }
                    Message::Close(_) => return Err(anyhow::anyhow!("сервер закрыл соединение")),
                    _ => {}
                }
            }
            _ = ping.tick() => {
                if last_rx.elapsed() > Duration::from_secs(70) {
                    return Err(anyhow::anyhow!("от сервера нет данных 70 с"));
                }
                write.send(Message::Ping(Vec::new())).await?;
            }
        }
    }
}

async fn catch_up(rpc: Rpc, out: Out, seen: Seen, last_sig: LastSig, wallet: String, until: String) {
    let short = wallet.get(..6).unwrap_or(wallet.as_str()).to_string();
    let res = rpc
        .call(
            "getSignaturesForAddress",
            json!([wallet, {"limit": CATCHUP_LIMIT, "until": until, "commitment": "confirmed"}]),
        )
        .await;
    let rows = match res {
        Ok(v) => v,
        Err(e) => {
            out.log("warn", format!("догон {}..: {}", short, e));
            return;
        }
    };
    let mut sigs: Vec<String> = match rows.as_array() {
        Some(a) => a
            .iter()
            .filter(|r| r["err"].is_null())
            .filter_map(|r| r["signature"].as_str().map(|s| s.to_string()))
            .collect(),
        None => return,
    };
    if sigs.is_empty() {
        return;
    }
    out.log("info", format!("догон {}..: пропущено транзакций: {}", short, sigs.len()));
    sigs.reverse(); // от старых к новым
    for sig in sigs {
        last_sig.lock().unwrap().insert(wallet.clone(), sig.clone());
        let (rpc2, out2, seen2, w2) = (rpc.clone(), out.clone(), seen.clone(), wallet.clone());
        tokio::spawn(async move {
            handle(rpc2, out2, seen2, w2, sig, Instant::now(), true).await;
        });
    }
}

async fn handle(rpc: Rpc, out: Out, seen: Seen, wallet: String, sig: String, t0: Instant, catchup: bool) {
    {
        let mut s = seen.lock().unwrap();
        if !s.insert(format!("{wallet}:{sig}")) {
            return;
        }
        if s.len() > 5000 {
            s.clear();
        }
    }
    for attempt in 0..8u32 {
        match rpc.get_transaction(&sig).await {
            Ok(Some(tx)) => {
                if let Some(sw) = parse_swap(&tx, &wallet) {
                    if catchup && sw.side == "buy" {
                        if let Some(bt) = tx.block_time {
                            let now = SystemTime::now()
                                .duration_since(UNIX_EPOCH)
                                .map(|d| d.as_secs() as i64)
                                .unwrap_or(0);
                            if now - bt > CATCHUP_MAX_BUY_AGE_SEC {
                                return;
                            }
                        }
                    }
                    out.send(json!({
                        "t": "swap",
                        "wallet": wallet,
                        "sig": sig,
                        "side": sw.side,
                        "mint": sw.mint,
                        "sol": sw.sol,
                        "frac": sw.frac,
                        "tokens": sw.tokens,
                        "detect_ms": t0.elapsed().as_millis() as u64
                    }));
                }
                return;
            }
            Ok(None) => {}
            Err(e) => {
                if attempt == 7 {
                    let short = sig.get(..8).unwrap_or(sig.as_str());
                    out.log("debug", format!("getTransaction {}: {}", short, e));
                }
            }
        }
        let wait = if attempt == 0 { 120 } else { 300 };
        tokio::time::sleep(Duration::from_millis(wait)).await;
    }
}
