use serde_json::{json, Value};
use tokio::sync::mpsc::UnboundedSender;

/// Канал в stdout: одна JSON-строка = одно сообщение для Python.
#[derive(Clone)]
pub struct Out(pub UnboundedSender<String>);

impl Out {
    pub fn send(&self, v: Value) {
        let _ = self.0.send(v.to_string());
    }

    pub fn log(&self, level: &str, msg: impl Into<String>) {
        self.send(json!({"t": "log", "level": level, "msg": msg.into()}));
    }
}
