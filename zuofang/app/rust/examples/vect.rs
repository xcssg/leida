//! 打印固定输入的 TGCP 向量，供 Python 侧对齐测试使用。
//!
//! 跑法：cargo run --release --example vect
//! 输出是 `key=hex` 形式，Python 的 server/tests_align.py 直接按行解析比对。

use zuofang_native::proto as P;

fn hex(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for x in b {
        s.push_str(&format!("{:02x}", x));
    }
    s
}

fn main() {
    // ---- 1. 密钥派生
    let shared = [0x11u8; 32];
    let cn = [0x22u8; 32];
    let sn = [0x33u8; 32];
    let sid: u64 = 0x0102030405060708;
    let k = P::SessionKeys::derive(&shared, &cn, &sn, sid);
    println!("c2s={}", hex(&k.c2s));
    println!("s2c={}", hex(&k.s2c));

    // ---- 2. 加密（短包）
    let key = [0x07u8; 32];
    let pt = b"hello";
    println!("seal_short={}", hex(&P::seal(&key, 1, P::DIR_C2S, pt)));

    // ---- 3. 加密（跨 HMAC 块，40 字节 > 32）
    let pt2: Vec<u8> = (0u8..40).collect();
    println!("seal_long={}", hex(&P::seal(&key, 0x0102030405060708, P::DIR_S2C, &pt2)));

    // ---- 4. 空 payload
    println!("seal_empty={}", hex(&P::seal(&key, 9, P::DIR_C2S, b"")));

    // ---- 5. 头
    let h = P::Header {
        ver: P::VER,
        dir: P::DIR_C2S,
        flags: P::FLAG_CONTROL,
        session_id: 0x1122334455667788,
        seq: 42,
        len: 100,
    };
    println!("header={}", hex(&h.encode()));

    // ---- 6. X25519 固定标量
    let sk = [0x77u8; 32];
    let pk = x25519_dalek::StaticSecret::from(sk);
    use x25519_dalek::PublicKey;
    println!("pub={}", hex(PublicKey::from(&pk).as_bytes()));
}
