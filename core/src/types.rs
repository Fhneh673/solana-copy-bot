use serde::Deserialize;

fn d_confirm() -> u64 {
    30
}
fn d_warm() -> u64 {
    20
}

/// Настройки, которые Python присылает первой строкой (`{"cmd":"init", ...}`).
/// Приватный ключ сюда НЕ передаётся: ядро читает его из переменной окружения PRIVATE_KEY.
#[derive(Deserialize, Clone)]
pub struct Config {
    pub rpc_http: String,
    pub rpc_ws: String,
    pub targets: Vec<String>,
    pub jupiter_url: String,
    /// Ключ Jupiter (заголовок x-api-key); пусто = без ключа (lite-api).
    #[serde(default)]
    pub jupiter_api_key: String,
    pub slippage_bps: u32,
    pub max_price_impact_pct: f64,
    pub max_priority_fee_lamports: u64,
    pub dry_run: bool,
    /// Дополнительные RPC: транзакция отправляется на все сразу (выше шанс попасть в блок).
    #[serde(default)]
    pub send_rpcs: Vec<String>,
    #[serde(default = "d_confirm")]
    pub confirm_timeout_sec: u64,
    /// Чаевые Jito в лампортах (0 = не использовать Jito).
    #[serde(default)]
    pub jito_tip_lamports: u64,
    /// Как часто «прогревать» соединения (0 = выключить).
    #[serde(default = "d_warm")]
    pub warmup_sec: u64,
}
