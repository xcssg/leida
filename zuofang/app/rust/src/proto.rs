//! TGCP v1 —— 线格式、密钥派生与 AEAD。
//!
//! 与 `PROTOCOL.md` 严格一致；`relay/` 的 Python 实现必须逐字节对齐。

use hmac::{Hmac, Mac};
use rand::RngCore;
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

pub const MAGIC: [u8; 4] = *b"TGCP";
pub const VER: u8 = 1;
pub const DIR_C2S: u8 = 0x01;
pub const DIR_S2C: u8 = 0x02;
pub const FLAG_CONTROL: u16 = 1 << 0;
pub const FLAG_CAPTURE: u16 = 1 << 1;

/// 明文头长度。
pub const HEADER_LEN: usize = 26;
/// tag 长度。
pub const TAG_LEN: usize = 16;
/// 单包 payload 上限（pt + tag）。
pub const MAX_PAYLOAD: usize = 1424;
/// 单包最大总长。
pub const MAX_PACKET: usize = HEADER_LEN + MAX_PAYLOAD;

/// 客户端 hello 固定部分：pub(32) + nonce(32) + session_id(8) + token_len(1)
pub const HELLO_C2S_FIXED: usize = 32 + 32 + 8 + 1;
/// 服务端 hello：status(1) + pub(32) + nonce(32) + session_id(8) + expires_at(8)
pub const HELLO_S2C_LEN: usize = 1 + 32 + 32 + 8 + 8;
/// hello 里 token 的最大长度。
pub const MAX_TOKEN_LEN: usize = 200;

pub const HS_OK: u8 = 0;
pub const HS_REJECTED: u8 = 1;
pub const HS_EXPIRED: u8 = 2;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Header {
    pub ver: u8,
    pub dir: u8,
    pub flags: u16,
    pub session_id: u64,
    pub seq: u64,
    /// 密文长度（含 tag）。
    pub len: u16,
}

impl Header {
    pub fn encode(&self) -> [u8; HEADER_LEN] {
        let mut b = [0u8; HEADER_LEN];
        b[0..4].copy_from_slice(&MAGIC);
        b[4] = self.ver;
        b[5] = self.dir;
        b[6..8].copy_from_slice(&self.flags.to_be_bytes());
        b[8..16].copy_from_slice(&self.session_id.to_be_bytes());
        b[16..24].copy_from_slice(&self.seq.to_be_bytes());
        b[24..26].copy_from_slice(&self.len.to_be_bytes());
        b
    }

    pub fn decode(b: &[u8]) -> Option<Header> {
        if b.len() < HEADER_LEN || b[0..4] != MAGIC {
            return None;
        }
        let ver = b[4];
        if ver != VER {
            return None;
        }
        Some(Header {
            ver,
            dir: b[5],
            flags: u16::from_be_bytes([b[6], b[7]]),
            session_id: u64::from_be_bytes(b[8..16].try_into().ok()?),
            seq: u64::from_be_bytes(b[16..24].try_into().ok()?),
            len: u16::from_be_bytes([b[24], b[25]]),
        })
    }
}

/// 由 X25519 共享密钥派生双向密钥。
pub struct SessionKeys {
    pub c2s: [u8; 32],
    pub s2c: [u8; 32],
}

impl SessionKeys {
    pub fn derive(
        shared: &[u8; 32],
        client_nonce: &[u8; 32],
        server_nonce: &[u8; 32],
        session_id: u64,
    ) -> SessionKeys {
        let mut salt = Vec::with_capacity(64);
        salt.extend_from_slice(client_nonce);
        salt.extend_from_slice(server_nonce);

        let prk = hmac_sha256(&salt, shared);

        let mut c2s_in = Vec::with_capacity(18);
        c2s_in.extend_from_slice(b"tgcp c2s");
        c2s_in.extend_from_slice(&session_id.to_be_bytes());
        let c2s = hmac_sha256(&prk, &c2s_in);

        let mut s2c_in = Vec::with_capacity(18);
        s2c_in.extend_from_slice(b"tgcp s2c");
        s2c_in.extend_from_slice(&session_id.to_be_bytes());
        let s2c = hmac_sha256(&prk, &s2c_in);

        SessionKeys { c2s, s2c }
    }

    pub fn for_dir(&self, dir: u8) -> &[u8; 32] {
        if dir == DIR_C2S {
            &self.c2s
        } else {
            &self.s2c
        }
    }
}

fn hmac_sha256(key: &[u8], data: &[u8]) -> [u8; 32] {
    let mut m = HmacSha256::new_from_slice(key).expect("hmac key");
    m.update(data);
    let out = m.finalize().into_bytes();
    let mut r = [0u8; 32];
    r.copy_from_slice(&out);
    r
}

/// 生成 `pt_len + TAG_LEN` 的密文（keystream 加密 + 16 字节 HMAC tag）。
pub fn seal(key: &[u8; 32], seq: u64, dir: u8, pt: &[u8]) -> Vec<u8> {
    let nonce = make_nonce(seq, dir);
    let ks = keystream(key, &nonce, pt.len());
    let mut ct = Vec::with_capacity(pt.len() + TAG_LEN);
    for (i, b) in pt.iter().enumerate() {
        ct.push(b ^ ks[i]);
    }
    let mut tag_in = Vec::with_capacity(12 + ct.len());
    tag_in.extend_from_slice(&nonce);
    tag_in.extend_from_slice(&ct);
    let tag = hmac_sha256(key, &tag_in);
    ct.extend_from_slice(&tag[..TAG_LEN]);
    ct
}

/// 返回明文；校验失败返回 None（不区分原因，避免探测）。
pub fn open(key: &[u8; 32], seq: u64, dir: u8, ct: &[u8]) -> Option<Vec<u8>> {
    if ct.len() < TAG_LEN {
        return None;
    }
    let (body, tag) = ct.split_at(ct.len() - TAG_LEN);
    let nonce = make_nonce(seq, dir);

    let mut tag_in = Vec::with_capacity(12 + body.len());
    tag_in.extend_from_slice(&nonce);
    tag_in.extend_from_slice(body);
    let expect = hmac_sha256(key, &tag_in);
    if !ct_eq(&expect[..TAG_LEN], tag) {
        return None;
    }

    let ks = keystream(key, &nonce, body.len());
    Some(
        body.iter()
            .enumerate()
            .map(|(i, b)| b ^ ks[i])
            .collect::<Vec<u8>>(),
    )
}

fn make_nonce(seq: u64, dir: u8) -> [u8; 12] {
    let mut n = [0u8; 12];
    n[..8].copy_from_slice(&seq.to_be_bytes());
    n[8] = dir;
    n
}

/// HMAC 计数器模式 keystream：`HMAC(k, nonce || counter_le32)` 依次拼接。
fn keystream(key: &[u8; 32], nonce: &[u8; 12], len: usize) -> Vec<u8> {
    let mut out = Vec::with_capacity(len + 32);
    let mut counter: u32 = 0;
    while out.len() < len {
        let mut buf = [0u8; 16];
        buf[..12].copy_from_slice(nonce);
        buf[12..].copy_from_slice(&counter.to_le_bytes());
        out.extend_from_slice(&hmac_sha256(key, &buf));
        counter = counter.wrapping_add(1);
    }
    out.truncate(len);
    out
}

fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut d = 0u8;
    for i in 0..a.len() {
        d |= a[i] ^ b[i];
    }
    d == 0
}

/// 随机 32 字节。
pub fn random32() -> [u8; 32] {
    let mut b = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut b);
    b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_roundtrip() {
        let h = Header {
            ver: VER,
            dir: DIR_C2S,
            flags: FLAG_CONTROL,
            session_id: 0x1122334455667788,
            seq: 42,
            len: 100,
        };
        let b = h.encode();
        assert_eq!(Header::decode(&b).unwrap(), h);
    }

    #[test]
    fn seal_open_roundtrip() {
        let key = [7u8; 32];
        let pt = b"hello tgcp, this is a payload longer than one hmac block 0123456789";
        let ct = seal(&key, 1, DIR_C2S, pt);
        assert_eq!(ct.len(), pt.len() + TAG_LEN);
        assert_eq!(open(&key, 1, DIR_C2S, &ct).unwrap(), pt);
        // 篡改应失败
        let mut bad = ct.clone();
        bad[0] ^= 1;
        assert!(open(&key, 1, DIR_C2S, &bad).is_none());
        // 错误 seq 应失败
        assert!(open(&key, 2, DIR_C2S, &ct).is_none());
    }

    #[test]
    fn header_rejects_garbage() {
        assert!(Header::decode(b"NOPE\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0").is_none());
        assert!(Header::decode(b"TGCP").is_none());
    }
}
