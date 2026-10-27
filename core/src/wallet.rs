//! Подпись транзакции Jupiter без тяжёлого solana-sdk:
//! формат = [shortvec: число подписей][подписи по 64 байта][message].
//! Подписываем message и вставляем подпись на место первой (fee payer).

use anyhow::{anyhow, bail, Result};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use ed25519_dalek::{Signer as _, SigningKey};

pub struct Signer {
    key: SigningKey,
    pub pubkey: String,
}

impl Signer {
    pub fn from_env() -> Result<Self> {
        let s = std::env::var("PRIVATE_KEY").map_err(|_| anyhow!("переменная PRIVATE_KEY не задана"))?;
        Self::from_b58(&s)
    }

    pub fn from_b58(s: &str) -> Result<Self> {
        let bytes = bs58::decode(s.trim())
            .into_vec()
            .map_err(|e| anyhow!("ключ не в формате base58: {e}"))?;
        if bytes.len() != 64 {
            bail!("ключ должен быть 64 байта в base58, получено {}", bytes.len());
        }
        let seed: [u8; 32] = bytes[..32].try_into().map_err(|_| anyhow!("плохой ключ"))?;
        let key = SigningKey::from_bytes(&seed);
        let public = key.verifying_key().to_bytes();
        if bytes[32..] != public[..] {
            bail!("ключ повреждён: публичная часть не совпадает с секретной");
        }
        Ok(Self {
            key,
            pubkey: bs58::encode(public).into_string(),
        })
    }

    pub fn sign_tx_b64(&self, tx_b64: &str) -> Result<String> {
        let mut raw = STANDARD
            .decode(tx_b64)
            .map_err(|e| anyhow!("транзакция не base64: {e}"))?;
        let (n_sigs, off) = read_shortvec(&raw)?;
        let sig_end = off + n_sigs * 64;
        if n_sigs == 0 || raw.len() <= sig_end {
            bail!("неожиданный формат транзакции");
        }
        let message = raw[sig_end..].to_vec();
        let sig = self.key.sign(&message);
        raw[off..off + 64].copy_from_slice(&sig.to_bytes());
        Ok(STANDARD.encode(raw))
    }
}

fn read_shortvec(b: &[u8]) -> Result<(usize, usize)> {
    let mut val: usize = 0;
    let mut shift = 0;
    for (i, &byte) in b.iter().enumerate().take(3) {
        val |= ((byte & 0x7f) as usize) << shift;
        if byte & 0x80 == 0 {
            return Ok((val, i + 1));
        }
        shift += 7;
    }
    bail!("битая длина подписей")
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::Verifier;

    fn test_key() -> String {
        let seed = [7u8; 32];
        let sk = SigningKey::from_bytes(&seed);
        let mut full = seed.to_vec();
        full.extend_from_slice(&sk.verifying_key().to_bytes());
        bs58::encode(full).into_string()
    }

    #[test]
    fn shortvec_decodes() {
        assert_eq!(read_shortvec(&[1, 0]).unwrap(), (1, 1));
        assert_eq!(read_shortvec(&[0x80, 0x01]).unwrap(), (128, 2));
    }

    #[test]
    fn signs_message_and_keeps_layout() {
        let s = Signer::from_b58(&test_key()).unwrap();
        let mut tx = vec![1u8];
        tx.extend_from_slice(&[0u8; 64]);
        tx.extend_from_slice(b"hello message");
        let out = STANDARD
            .decode(s.sign_tx_b64(&STANDARD.encode(&tx)).unwrap())
            .unwrap();
        assert_eq!(out.len(), tx.len());
        assert_eq!(&out[65..], b"hello message");
        let sig = ed25519_dalek::Signature::from_slice(&out[1..65]).unwrap();
        s.key.verifying_key().verify(b"hello message", &sig).unwrap();
    }

    #[test]
    fn broken_key_is_rejected() {
        assert!(Signer::from_b58("not-a-key").is_err());
    }
}
