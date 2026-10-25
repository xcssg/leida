//! 启动配置（由 Dart 通过 JSON 下发，Kotlin 原样透传）。

use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    /// `udp://host:port` 或 `host:port`
    pub relay: String,
    pub session_id: String,
    #[serde(default)]
    pub token: String,
    #[serde(default = "default_dns")]
    pub dns: String,
    #[serde(default = "default_mtu")]
    pub mtu: u16,
    #[serde(default)]
    pub capture: bool,
    #[serde(default)]
    pub game_package: String,
}

fn default_dns() -> String {
    "1.1.1.1".to_string()
}

fn default_mtu() -> u16 {
    1400
}

impl Config {
    /// 解析失败时给出中文原因，直接回给 Dart。
    pub fn parse(raw: &str) -> Result<Config, String> {
        let cfg: Config = serde_json::from_str(raw).map_err(|e| format!("配置解析失败: {e}"))?;
        if cfg.relay.trim().is_empty() {
            return Err("缺少 relay 地址".into());
        }
        if cfg.mtu < 576 || cfg.mtu > 1500 {
            return Err(format!("MTU 超出范围: {}", cfg.mtu));
        }
        Ok(cfg)
    }

    /// 把 `udp://1.2.3.4:9000` 归一化成 `1.2.3.4:9000`。
    pub fn relay_addr(&self) -> String {
        let s = self.relay.trim();
        let s = s.strip_prefix("udp://").unwrap_or(s);
        let s = s.strip_prefix("tcp://").unwrap_or(s);
        s.trim_end_matches('/').to_string()
    }

    /// 把任意 session_id 字符串稳定映射成 u64（协议字段是定长 8 字节）。
    pub fn session_u64(&self) -> u64 {
        if let Ok(v) = self.session_id.parse::<u64>() {
            return v;
        }
        // FNV-1a 64
        let mut h: u64 = 0xcbf2_9ce4_8422_2325;
        for b in self.session_id.as_bytes() {
            h ^= *b as u64;
            h = h.wrapping_mul(0x0000_0100_0000_01b3);
        }
        h
    }
}
