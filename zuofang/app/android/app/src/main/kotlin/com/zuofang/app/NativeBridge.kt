package com.zuofang.app

/**
 * 与 Rust 引擎（libzuofang_native.so）的唯一桥。
 *
 * 方法名、签名与上游 APK 完全一致，便于对照分析：
 *   Java_com_zuofang_app_NativeBridge_prepare
 *   Java_com_zuofang_app_NativeBridge_start
 *   Java_com_zuofang_app_NativeBridge_status
 *   Java_com_zuofang_app_NativeBridge_stop
 *
 * 返回值为 JSON 字符串，由 Dart 侧解析。
 */
object NativeBridge {

    @Volatile
    private var loaded = false

    init {
        try {
            System.loadLibrary("zuofang_native")
            loaded = true
        } catch (t: Throwable) {
            loaded = false
        }
    }

    val isLoaded: Boolean get() = loaded

    /**
     * 仅做参数校验与本地准备（不建立 TUN）。
     * 返回 {"state":"ready", ...} 或 {"state":"error","reason":"..."}
     */
    external fun prepare(service: ZfVpnService, configJson: String): String

    /**
     * 正式启动：从 [service] 取 TUN fd，进入握手，开始转发。
     * 返回 {"state":"running","session_id":...}
     */
    external fun start(service: ZfVpnService, configJson: String): String

    /** 运行态快照（RTT / 字节数 / 丢包 / 到期时间）。 */
    external fun status(): String

    /** 幂等停止。 */
    external fun stop(): String

    /** 抓包统计。 */
    external fun captureStatus(): String
}
