//! 引擎主体：握手 + 双向转发 + 心跳 + 统计。
//!
//! 线程模型（无 async runtime，纯 std）：
//!   handshake  ->  在 `start()` 调用线程同步完成（5s 超时）
//!   tun-reader ->  TUN 出包 -> seal -> UDP send
//!   udp-reader ->  UDP recv -> open -> TUN 入包 / 控制帧处理
//!   heartbeat  ->  每 10s ping，30s 无包判定 idle timeout

use std::collections::VecDeque;
use std::io;
use std::net::{SocketAddr, ToSocketAddrs, UdpSocket};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use x25519_dalek::{PublicKey, StaticSecret};

use crate::config::Config;
use crate::proto::{
    self, Header, SessionKeys, DIR_C2S, DIR_S2C, FLAG_CAPTURE, FLAG_CONTROL, MAX_PACKET,
};
use crate::tun::TunDevice;

pub const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(5);
pub const IDLE_TIMEOUT: Duration = Duration::from_secs(30);
pub const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);
const MAX_PENDING_BANKS: usize = 8;

#[derive(Default)]
pub struct Stats {
    pub seq: AtomicU64,
    pub rx_bytes: AtomicU64,
    pub tx_bytes: AtomicU64,
    pub dropped: AtomicU64,
    pub rtt_us: AtomicU64,
    pub packets_in: AtomicU64,
    pub packets_out: AtomicU64,
    pub capture_packets: AtomicU64,
    pub capture_bytes: AtomicU64,
    pub capture_banks: AtomicU64,
    pub last_rx_ms: AtomicU64,
}

impl Stats {
    fn touch_rx(&self) {
        self.last_rx_ms.store(now_ms(), Ordering::Relaxed);
    }

    fn idle_ms(&self) -> u64 {
        let last = self.last_rx_ms.load(Ordering::Relaxed);
        if last == 0 {
            0
        } else {
            now_ms().saturating_sub(last)
        }
    }
}

pub struct Engine {
    cfg: Config,
    tun: TunDevice,
    socket: Arc<UdpSocket>,
    relay: SocketAddr,
    keys: SessionKeys,
    session_id: u64,
    expires_at: u64,
    stop: Arc<AtomicBool>,
    stats: Arc<Stats>,
    handles: Mutex<Vec<std::thread::JoinHandle<()>>>,
    reason: Mutex<String>,
    started_at: Instant,
}

/// 全局单例。`NativeBridge` 的四个方法都操作它。
static ENGINE: Mutex<Option<Arc<Engine>>> = Mutex::new(None);

pub fn prepare(cfg_raw: &str) -> String {
    match Config::parse(cfg_raw) {
        Ok(cfg) => json!({
            "state": "ready",
            "relay": cfg.relay_addr(),
            "mtu": cfg.mtu,
            "dns": cfg.dns,
            "capture": cfg.capture,
        })
        .to_string(),
        Err(e) => json!({ "state": "error", "reason": e }).to_string(),
    }
}

/// `start` 需要 TUN fd（由 Kotlin 从 ParcelFileDescriptor 取出）与 protect 回调。
pub fn start(
    cfg_raw: &str,
    tun_fd: i32,
    protect: &dyn Fn(i32) -> bool,
) -> String {
    if ENGINE.lock().unwrap().is_some() {
        return json!({ "state": "error", "reason": "capture already running" }).to_string();
    }

    let cfg = match Config::parse(cfg_raw) {
        Ok(c) => c,
        Err(e) => return json!({ "state": "error", "reason": e }).to_string(),
    };

    let tun = match TunDevice::from_fd(tun_fd) {
        Some(t) => t,
        None => return json!({ "state": "error", "reason": "TUN descriptor required" }).to_string(),
    };

    let relay = match resolve(&cfg.relay_addr()) {
        Ok(a) => a,
        Err(e) => return json!({ "state": "error", "reason": e }).to_string(),
    };

    let socket = match UdpSocket::bind(("0.0.0.0", 0)) {
        Ok(s) => s,
        Err(e) => return json!({ "state": "error", "reason": format!("UDP bind 失败: {e}") }).to_string(),
    };

    if let Err(e) = crate::tun::protect_socket(crate::tun::socket_fd(&socket), protect) {
        return json!({ "state": "error", "reason": e }).to_string();
    }
    let _ = socket.set_read_timeout(Some(Duration::from_millis(400)));
    let _ = socket.set_write_timeout(Some(Duration::from_millis(800)));

    // ---- 握手 ----
    let session_id = cfg.session_u64();
    let handshake = match do_handshake(&socket, relay, session_id, &cfg.token) {
        Ok(h) => h,
        Err(e) => return json!({ "state": "error", "reason": e }).to_string(),
    };

    let engine = Arc::new(Engine {
        cfg,
        tun,
        socket: Arc::new(socket),
        relay,
        keys: handshake.keys,
        session_id,
        expires_at: handshake.expires_at,
        stop: Arc::new(AtomicBool::new(false)),
        stats: Arc::new(Stats::default()),
        handles: Mutex::new(Vec::new()),
        reason: Mutex::new(String::new()),
        started_at: Instant::now(),
    });
    engine.stats.touch_rx();

    // ---- 线程 ----
    let mut handles = Vec::new();
    {
        let e = engine.clone();
        handles.push(std::thread::spawn(move || e.tun_loop()));
    }
    {
        let e = engine.clone();
        handles.push(std::thread::spawn(move || e.udp_loop()));
    }
    {
        let e = engine.clone();
        handles.push(std::thread::spawn(move || e.heartbeat_loop()));
    }
    *engine.handles.lock().unwrap() = handles;

    let status = json!({
        "state": "running",
        "session_id": format!("{}", session_id),
        "relay": relay.to_string(),
        "expires_at": handshake.expires_at,
        "server_time": now_s(),
    })
    .to_string();

    *ENGINE.lock().unwrap() = Some(engine);
    status
}

pub fn status() -> String {
    let guard = ENGINE.lock().unwrap();
    let Some(e) = guard.as_ref() else {
        return json!({ "state": "stopped", "reason": "" }).to_string();
    };
    let s = &e.stats;
    json!({
        "state": if e.stop.load(Ordering::Relaxed) { "stopping" } else { "running" },
        "session_id": format!("{}", e.session_id),
        "relay": e.relay.to_string(),
        "rtt_us": s.rtt_us.load(Ordering::Relaxed),
        "latency_ms": s.rtt_us.load(Ordering::Relaxed) / 1000,
        "rx_bytes": s.rx_bytes.load(Ordering::Relaxed),
        "tx_bytes": s.tx_bytes.load(Ordering::Relaxed),
        "dropped": s.dropped.load(Ordering::Relaxed),
        "packets_in": s.packets_in.load(Ordering::Relaxed),
        "packets_out": s.packets_out.load(Ordering::Relaxed),
        "expires_at": e.expires_at,
        "uptime_ms": e.started_at.elapsed().as_millis() as u64,
        "reason": e.reason.lock().unwrap().clone(),
    })
    .to_string()
}

pub fn capture_status() -> String {
    let guard = ENGINE.lock().unwrap();
    let Some(e) = guard.as_ref() else {
        return json!({ "active": false, "packets": 0, "bytes": 0, "banks": 0 })
            .to_string();
    };
    let s = &e.stats;
    json!({
        "active": e.cfg.capture && !e.stop.load(Ordering::Relaxed),
        "packets": s.capture_packets.load(Ordering::Relaxed),
        "bytes": s.capture_bytes.load(Ordering::Relaxed),
        "banks": s.capture_banks.load(Ordering::Relaxed),
        "expires_at": e.expires_at,
    })
    .to_string()
}

pub fn stop() -> String {
    let taken = ENGINE.lock().unwrap().take();
    let Some(e) = taken else {
        return json!({ "state": "stopped", "reason": "already stopped" }).to_string();
    };
    e.stop.store(true, Ordering::SeqCst);
    // 发一个 bye 控制帧，让中继及时释放会话
    e.send_control(json!({ "t": "bye", "reason": "user" }));

    let handles: Vec<_> = e.handles.lock().unwrap().drain(..).collect();
    for h in handles {
        let _ = h.join();
    }

    let s = &e.stats;
    json!({
        "state": "stopped",
        "session_id": format!("{}", e.session_id),
        "rx_bytes": s.rx_bytes.load(Ordering::Relaxed),
        "tx_bytes": s.tx_bytes.load(Ordering::Relaxed),
        "dropped": s.dropped.load(Ordering::Relaxed),
        "reason": "user",
    })
    .to_string()
}

// ---------------- 内部实现 ----------------

struct Handshake {
    keys: SessionKeys,
    expires_at: u64,
}

fn do_handshake(
    sock: &UdpSocket,
    relay: SocketAddr,
    session_id: u64,
    token: &str,
) -> Result<Handshake, String> {
    let secret = StaticSecret::random_from_rng(rand::thread_rng());
    let public = PublicKey::from(&secret);
    let client_nonce = proto::random32();

    let token_bytes = token.as_bytes();
    if token_bytes.len() > proto::MAX_TOKEN_LEN {
        return Err("token 过长".into());
    }

    let mut hello = Vec::with_capacity(proto::HELLO_C2S_FIXED + token_bytes.len());
    hello.extend_from_slice(public.as_bytes());
    hello.extend_from_slice(&client_nonce);
    hello.extend_from_slice(&session_id.to_be_bytes());
    hello.push(token_bytes.len() as u8);
    hello.extend_from_slice(token_bytes);

    let deadline = Instant::now() + HANDSHAKE_TIMEOUT;
    let mut buf = [0u8; 2048];
    let mut attempts = 0;

    loop {
        if Instant::now() >= deadline {
            return Err("TGCP handshake timeout".into());
        }
        sock.send_to(&hello, relay)
            .map_err(|e| format!("hello 发送失败: {e}"))?;
        attempts += 1;

        match sock.recv_from(&mut buf) {
            Ok((n, from)) if from == relay && n == proto::HELLO_S2C_LEN => {
                let status = buf[0];
                let server_pub: [u8; 32] = buf[1..33].try_into().unwrap();
                let server_nonce: [u8; 32] = buf[33..65].try_into().unwrap();
                let sid = u64::from_be_bytes(buf[65..73].try_into().unwrap());
                let expires_at = u64::from_be_bytes(buf[73..81].try_into().unwrap());

                match status {
                    proto::HS_OK => {}
                    proto::HS_EXPIRED => return Err("session expired".into()),
                    _ => return Err("invalid capture token".into()),
                }
                if sid != session_id {
                    return Err("bad hello: session mismatch".into());
                }
                let shared = secret.diffie_hellman(&PublicKey::from(server_pub));
                if shared.as_bytes().iter().all(|b| *b == 0) {
                    return Err("invalid DH secret".into());
                }
                let keys = SessionKeys::derive(
                    shared.as_bytes(),
                    &client_nonce,
                    &server_nonce,
                    session_id,
                );
                return Ok(Handshake { keys, expires_at });
            }
            Ok(_) => {
                // 长度/来源不符，忽略（对应上游 duplicate hello / bad hello 的静默丢弃）
                if attempts >= 8 {
                    return Err("bad hello".into());
                }
            }
            Err(_) => {
                if attempts >= 3 {
                    return Err("TGCP handshake timeout".into());
                }
            }
        }
    }
}

impl Engine {
    fn tun_loop(self: &Arc<Self>) {
        let mut buf = vec![0u8; self.cfg.mtu as usize];
        let mut seq = 0u64;
        while !self.stop.load(Ordering::Relaxed) {
            let n = match self.tun.read_packet(&mut buf) {
                Ok(0) => break,
                Ok(n) => n,
                Err(_) => {
                    std::thread::sleep(Duration::from_millis(20));
                    continue;
                }
            };
            if n == 0 || n > buf.len() {
                continue;
            }
            seq = seq.wrapping_add(1);
            self.stats.seq.store(seq, Ordering::Relaxed);

            let ct = proto::seal(&self.keys.c2s, seq, DIR_C2S, &buf[..n]);
            let mut pkt = Vec::with_capacity(proto::HEADER_LEN + ct.len());
            pkt.extend_from_slice(
                &Header {
                    ver: proto::VER,
                    dir: DIR_C2S,
                    flags: 0,
                    session_id: self.session_id,
                    seq,
                    len: ct.len() as u16,
                }
                .encode(),
            );
            pkt.extend_from_slice(&ct);

            match self.socket.send_to(&pkt, self.relay) {
                Ok(m) if m == pkt.len() => {
                    self.stats.tx_bytes.fetch_add(n as u64, Ordering::Relaxed);
                    self.stats.packets_out.fetch_add(1, Ordering::Relaxed);
                }
                _ => {
                    self.stats.dropped.fetch_add(1, Ordering::Relaxed);
                }
            }
        }
        self.set_reason("TUN 已关闭");
    }

    fn udp_loop(self: &Arc<Self>) {
        let mut buf = vec![0u8; MAX_PACKET * 2];
        let mut bank_tracker = BankTracker::new();

        while !self.stop.load(Ordering::Relaxed) {
            let (n, from) = match self.socket.recv_from(&mut buf) {
                Ok(v) => v,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock
                    || e.kind() == io::ErrorKind::TimedOut =>
                {
                    if self.stats.idle_ms() > IDLE_TIMEOUT.as_millis() as u64 {
                        self.set_reason("TGCP idle timeout");
                        break;
                    }
                    continue;
                }
                Err(_) => continue,
            };
            if from != self.relay {
                self.stats.dropped.fetch_add(1, Ordering::Relaxed);
                continue;
            }
            self.stats.touch_rx();

            let Some(h) = Header::decode(&buf[..n]) else {
                self.stats.dropped.fetch_add(1, Ordering::Relaxed);
                continue;
            };
            if h.dir != DIR_S2C || h.session_id != self.session_id {
                self.stats.dropped.fetch_add(1, Ordering::Relaxed);
                continue;
            }
            let end = proto::HEADER_LEN + h.len as usize;
            if end > n || h.len as usize > proto::MAX_PAYLOAD {
                self.stats.dropped.fetch_add(1, Ordering::Relaxed);
                continue;
            }
            let Some(pt) = proto::open(&self.keys.s2c, h.seq, DIR_S2C, &buf[proto::HEADER_LEN..end])
            else {
                self.stats.dropped.fetch_add(1, Ordering::Relaxed);
                continue;
            };

            if h.flags & FLAG_CONTROL != 0 {
                self.on_control(&pt);
                continue;
            }
            if h.flags & FLAG_CAPTURE != 0 {
                self.on_capture(&pt, &mut bank_tracker);
                continue;
            }

            match self.tun.write_packet(&pt) {
                Ok(_) => {
                    self.stats.rx_bytes.fetch_add(pt.len() as u64, Ordering::Relaxed);
                    self.stats.packets_in.fetch_add(1, Ordering::Relaxed);
                }
                Err(_) => {
                    self.stats.dropped.fetch_add(1, Ordering::Relaxed);
                }
            }
        }
    }

    fn heartbeat_loop(self: &Arc<Self>) {
        while !self.stop.load(Ordering::Relaxed) {
            std::thread::sleep(HEARTBEAT_INTERVAL);
            if self.stop.load(Ordering::Relaxed) {
                break;
            }
            let ts = now_ms();
            self.send_control(json!({ "t": "ping", "ts": ts }));
            if self.stats.idle_ms() > IDLE_TIMEOUT.as_millis() as u64 {
                self.set_reason("TGCP idle timeout");
                self.stop.store(true, Ordering::SeqCst);
                break;
            }
        }
    }

    fn on_control(&self, pt: &[u8]) {
        let Ok(v) = serde_json::from_slice::<Value>(pt) else {
            return;
        };
        match v.get("t").and_then(|t| t.as_str()) {
            Some("pong") => {
                if let Some(ts) = v.get("ts").and_then(|t| t.as_u64()) {
                    let rtt = now_ms().saturating_sub(ts) * 1000;
                    self.stats.rtt_us.store(rtt, Ordering::Relaxed);
                }
            }
            Some("ack") => { /* bank ACK 由 relay 侧记账，客户端只做统计 */ }
            Some("config") => { /* 预留：中继可动态下发参数 */ }
            _ => {}
        }
    }

    fn on_capture(&self, pt: &[u8], banks: &mut BankTracker) {
        if pt.len() < 2 {
            self.stats.dropped.fetch_add(1, Ordering::Relaxed);
            return;
        }
        let hlen = u16::from_be_bytes([pt[0], pt[1]]) as usize;
        if hlen + 2 > pt.len() {
            self.stats.dropped.fetch_add(1, Ordering::Relaxed);
            return;
        }
        let Ok(h) = serde_json::from_slice::<Value>(&pt[2..2 + hlen]) else {
            self.stats.dropped.fetch_add(1, Ordering::Relaxed);
            return;
        };
        let Some(bank) = h.get("bank").and_then(|b| b.as_u64()) else {
            self.stats.dropped.fetch_add(1, Ordering::Relaxed);
            return;
        };
        let last = h.get("last").and_then(|b| b.as_u64()).unwrap_or(0) == 1;

        self.stats.capture_packets.fetch_add(1, Ordering::Relaxed);
        self.stats
            .capture_bytes
            .fetch_add((pt.len() - 2 - hlen) as u64, Ordering::Relaxed);

        if banks.push(bank) {
            self.stats.capture_banks.fetch_add(1, Ordering::Relaxed);
        }
        if last {
            self.send_control(json!({ "t": "ack", "bank": bank }));
        }
    }

    fn send_control(&self, v: Value) {
        let pt = v.to_string();
        let seq = self.stats.seq.fetch_add(1, Ordering::Relaxed) + 1;
        let ct = proto::seal(&self.keys.c2s, seq, DIR_C2S, pt.as_bytes());
        let mut pkt = Vec::with_capacity(proto::HEADER_LEN + ct.len());
        pkt.extend_from_slice(
            &Header {
                ver: proto::VER,
                dir: DIR_C2S,
                flags: FLAG_CONTROL,
                session_id: self.session_id,
                seq,
                len: ct.len() as u16,
            }
            .encode(),
        );
        pkt.extend_from_slice(&ct);
        let _ = self.socket.send_to(&pkt, self.relay);
    }

    fn set_reason(&self, r: &str) {
        let mut g = self.reason.lock().unwrap();
        if g.is_empty() {
            *g = r.to_string();
        }
    }

    /// 供 `capture` 上行使用（预留：把本地 TUN 的包镜像到中继）。
    #[allow(dead_code)]
    pub fn send_capture_frame(&self, bank: u32, last: bool, packet: &[u8]) {
        if !self.cfg.capture {
            return;
        }
        let h = json!({ "bank": bank, "last": if last { 1 } else { 0 }, "ts": now_ms(), "proto": "ip" })
            .to_string();
        let mut pt = Vec::with_capacity(2 + h.len() + packet.len());
        pt.extend_from_slice(&(h.len() as u16).to_be_bytes());
        pt.extend_from_slice(h.as_bytes());
        pt.extend_from_slice(packet);

        let seq = self.stats.seq.fetch_add(1, Ordering::Relaxed) + 1;
        let ct = proto::seal(&self.keys.c2s, seq, DIR_C2S, &pt);
        let mut pkt = Vec::with_capacity(proto::HEADER_LEN + ct.len());
        pkt.extend_from_slice(
            &Header {
                ver: proto::VER,
                dir: DIR_C2S,
                flags: FLAG_CAPTURE,
                session_id: self.session_id,
                seq,
                len: ct.len() as u16,
            }
            .encode(),
        );
        pkt.extend_from_slice(&ct);
        let _ = self.socket.send_to(&pkt, self.relay);
    }
}

/// 未 ACK 的 bank 队列，超过上限丢最旧的（对应 `too many pending banks`）。
struct BankTracker {
    pending: VecDeque<u64>,
}

impl BankTracker {
    fn new() -> Self {
        BankTracker { pending: VecDeque::new() }
    }

    /// 返回 true 表示是新 bank。
    fn push(&mut self, bank: u64) -> bool {
        if self.pending.contains(&bank) {
            return false;
        }
        self.pending.push_back(bank);
        while self.pending.len() > MAX_PENDING_BANKS {
            self.pending.pop_front();
        }
        true
    }
}

fn resolve(addr: &str) -> Result<SocketAddr, String> {
    addr.to_socket_addrs()
        .map_err(|e| format!("中继地址解析失败({addr}): {e}"))?
        .next()
        .ok_or_else(|| format!("中继地址无结果: {addr}"))
}

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

pub fn now_s() -> u64 {
    now_ms() / 1000
}
