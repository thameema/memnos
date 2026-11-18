//! Encrypted Secret Vault — 100% bit-for-bit AES-256-GCM compatible with Python `core/vault.py`.
//! Uses 32-byte `base64(MEMNOS_SECRET_KEY)`, 12-byte random nonce, and `name.as_bytes()` as AAD.

use aes_gcm::{
    aead::{Aead, KeyInit, Payload},
    Aes256Gcm, Nonce,
};
use base64::{
    engine::general_purpose::{STANDARD as B64, STANDARD_NO_PAD, URL_SAFE, URL_SAFE_NO_PAD},
    Engine as _,
};
use chrono::{DateTime, Utc};
use rand::RngCore;
use serde::Serialize;
use std::env;
use thiserror::Error;
use tokio_postgres::Client;

pub const REF_PREFIX: &str = "secret://";

#[derive(Debug, Error)]
pub enum VaultError {
    #[error("vault locked: {0}")]
    Locked(String),
    #[error("bad secret value: {0}")]
    BadValue(String),
    #[error("crypto error: {0}")]
    Crypto(String),
    #[error("database error: {0}")]
    Db(#[from] tokio_postgres::Error),
}

#[derive(Debug, Clone, Serialize)]
pub struct SecretMeta {
    pub name: String,
    pub description: Option<String>,
    pub created_at: Option<DateTime<Utc>>,
    pub updated_at: Option<DateTime<Utc>>,
}

fn master_key() -> Result<[u8; 32], VaultError> {
    let raw = env::var("MEMNOS_SECRET_KEY").unwrap_or_default();
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err(VaultError::Locked(
            "MEMNOS_SECRET_KEY not set — run `memnos secret keygen`".to_string(),
        ));
    }
    decode_key_b64(trimmed)
}

fn decode_key_b64(b64: &str) -> Result<[u8; 32], VaultError> {
    let s = b64.trim();
    let bytes = B64
        .decode(s)
        .or_else(|_| URL_SAFE.decode(s))
        .or_else(|_| STANDARD_NO_PAD.decode(s))
        .or_else(|_| URL_SAFE_NO_PAD.decode(s))
        .map_err(|e| VaultError::Locked(format!("MEMNOS_SECRET_KEY is not valid base64: {}", e)))?;
    if bytes.len() != 32 {
        return Err(VaultError::Locked(
            "MEMNOS_SECRET_KEY must decode to 32 bytes (AES-256)".to_string(),
        ));
    }
    let mut arr = [0u8; 32];
    arr.copy_from_slice(&bytes);
    Ok(arr)
}

pub struct Vault;

impl Vault {
    pub fn available() -> bool {
        master_key().is_ok()
    }

    pub fn keygen() -> String {
        let mut key = [0u8; 32];
        rand::thread_rng().fill_bytes(&mut key);
        B64.encode(key)
    }

    pub fn encrypt_raw(key: &[u8; 32], name: &str, plaintext: &str) -> Result<([u8; 12], Vec<u8>), VaultError> {
        if plaintext.starts_with(REF_PREFIX) {
            return Err(VaultError::BadValue(format!(
                "refusing to store {:?} as plaintext for secret '{}' — that is a {} reference",
                plaintext, name, REF_PREFIX
            )));
        }
        let cipher = Aes256Gcm::new_from_slice(key)
            .map_err(|e| VaultError::Crypto(e.to_string()))?;
        let mut nonce_bytes = [0u8; 12];
        rand::thread_rng().fill_bytes(&mut nonce_bytes);
        let nonce = Nonce::from_slice(&nonce_bytes);
        let ct = cipher
            .encrypt(
                nonce,
                Payload {
                    msg: plaintext.as_bytes(),
                    aad: name.as_bytes(),
                },
            )
            .map_err(|e| VaultError::Crypto(e.to_string()))?;
        Ok((nonce_bytes, ct))
    }

    pub fn decrypt_raw(key: &[u8; 32], name: &str, nonce: &[u8], ciphertext: &[u8]) -> Result<String, VaultError> {
        if nonce.len() != 12 {
            return Err(VaultError::Crypto("invalid 12-byte AES-GCM nonce".to_string()));
        }
        let cipher = Aes256Gcm::new_from_slice(key)
            .map_err(|e| VaultError::Crypto(e.to_string()))?;
        let pt = cipher
            .decrypt(
                Nonce::from_slice(nonce),
                Payload {
                    msg: ciphertext,
                    aad: name.as_bytes(),
                },
            )
            .map_err(|_| VaultError::Crypto("AES-GCM authentication tag mismatch".to_string()))?;
        String::from_utf8(pt).map_err(|e| VaultError::Crypto(e.to_string()))
    }

    pub async fn set(
        client: &Client,
        name: &str,
        plaintext: &str,
        description: Option<&str>,
    ) -> Result<(), VaultError> {
        let key = master_key()?;
        let (nonce, ct) = Self::encrypt_raw(&key, name, plaintext)?;
        client
            .execute(
                "INSERT INTO memnos_control.secrets(name, nonce, ciphertext, description)
                 VALUES ($1, $2, $3, $4)
                 ON CONFLICT (name) DO UPDATE SET
                   nonce = EXCLUDED.nonce,
                   ciphertext = EXCLUDED.ciphertext,
                   description = COALESCE(EXCLUDED.description, memnos_control.secrets.description),
                   updated_at = now()",
                &[&name, &&nonce[..], &ct, &description],
            )
            .await?;
        Ok(())
    }

    pub async fn get(client: &Client, name: &str) -> Result<Option<String>, VaultError> {
        let row = client
            .query_opt(
                "SELECT nonce, ciphertext FROM memnos_control.secrets WHERE name = $1",
                &[&name],
            )
            .await?;
        let Some(r) = row else {
            return Ok(None);
        };
        let key = master_key()?;
        let nonce: Vec<u8> = r.get("nonce");
        let ct: Vec<u8> = r.get("ciphertext");
        let pt = Self::decrypt_raw(&key, name, &nonce, &ct)?;
        Ok(Some(pt))
    }

    pub async fn list(client: &Client) -> Result<Vec<SecretMeta>, VaultError> {
        let rows = client
            .query(
                "SELECT name, description, created_at, updated_at
                 FROM memnos_control.secrets ORDER BY name",
                &[],
            )
            .await?;
        Ok(rows
            .into_iter()
            .map(|r| SecretMeta {
                name: r.get("name"),
                description: r.get("description"),
                created_at: r.get("created_at"),
                updated_at: r.get("updated_at"),
            })
            .collect())
    }

    pub async fn delete(client: &Client, name: &str) -> Result<bool, VaultError> {
        let n = client
            .execute("DELETE FROM memnos_control.secrets WHERE name = $1", &[&name])
            .await?;
        Ok(n > 0)
    }

    pub async fn resolve(client: &Client, value: &str) -> Result<Option<String>, VaultError> {
        if let Some(secret_name) = value.strip_prefix(REF_PREFIX) {
            Self::get(client, secret_name).await
        } else {
            Ok(Some(value.to_string()))
        }
    }

    pub async fn rotate_key(
        client: &Client,
        old_key_b64: &str,
        new_key_b64: &str,
    ) -> Result<(usize, Vec<String>), VaultError> {
        let old_k = decode_key_b64(old_key_b64)?;
        let new_k = decode_key_b64(new_key_b64)?;
        let rows = client
            .query("SELECT name, nonce, ciphertext FROM memnos_control.secrets", &[])
            .await?;
        let mut skipped = Vec::new();
        let mut rotated = 0usize;
        for r in rows {
            let name: String = r.get("name");
            let nonce: Vec<u8> = r.get("nonce");
            let ct: Vec<u8> = r.get("ciphertext");
            match Self::decrypt_raw(&old_k, &name, &nonce, &ct) {
                Ok(pt) => {
                    let (new_nonce, new_ct) = Self::encrypt_raw(&new_k, &name, &pt)?;
                    client
                        .execute(
                            "UPDATE memnos_control.secrets SET nonce = $1, ciphertext = $2, updated_at = now()
                             WHERE name = $3",
                            &[&&new_nonce[..], &new_ct, &name],
                        )
                        .await?;
                    rotated += 1;
                }
                Err(_) => skipped.push(name),
            }
        }
        Ok((rotated, skipped))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_aes256_gcm_roundtrip_and_aad_binding() {
        let k_b64 = Vault::keygen();
        let k = decode_key_b64(&k_b64).unwrap();
        let (nonce, ct) = Vault::encrypt_raw(&k, "openai", "sk-test-value-123").unwrap();
        let pt = Vault::decrypt_raw(&k, "openai", &nonce, &ct).unwrap();
        assert_eq!(pt, "sk-test-value-123");
        // Different secret name (AAD mismatch) must fail authentication
        assert!(Vault::decrypt_raw(&k, "other_name", &nonce, &ct).is_err());
        // Reject storing secret:// reference
        assert!(matches!(
            Vault::encrypt_raw(&k, "openai", "secret://openai"),
            Err(VaultError::BadValue(_))
        ));
    }
}
