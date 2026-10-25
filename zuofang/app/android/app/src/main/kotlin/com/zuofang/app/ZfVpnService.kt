package com.zuofang.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.net.VpnService
import android.os.Build
import android.os.ParcelFileDescriptor
import android.util.Log
import androidx.annotation.Keep
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 本地流量转发服务。
 *
 * 状态机：
 *   START -> 建通知 -> startForeground -> 后台线程 establish() TUN -> NativeBridge.start(fd)
 *   STOP / onRevoke / onDestroy -> NativeBridge.stop() + 关闭 fd（幂等）
 *
 * 分流由 Dart 下发的 `split_mode` / `split_apps` 决定：
 *   all  —— 不加任何 allowed/disallowed，全量走隧道
 *   apps —— 只允许 split_apps 里的包名 + 自身，其余直连
 */
class ZfVpnService : VpnService() {

    companion object {
        private const val TAG = "ZfVpnService"
        private const val CHANNEL_ID = "zuofang-capture"
        private const val CHANNEL_NAME = "ZUOFANG 采集"
        private const val NOTIF_ID = 0x4E56 // "NV"

        const val ACTION_STOP = "STOP"
        const val EXTRA_CONFIG = "config"

        private const val FALLBACK_PACKAGE = "com.tencent.tmgp.dfm"
        private const val LOCAL_V4 = "10.66.0.2"
        private const val LOCAL_V6 = "fd00:66:66::2"
        private const val DNS_PRIMARY = "1.1.1.1"
        private const val MTU = 1400

        @Volatile
        var status: String = "idle"
            private set

        @Volatile
        var lastReason: String = ""
            private set

        fun isActive(): Boolean = status == "starting" || status == "running"
    }

    private val running = AtomicBoolean(false)
    private val worker = Executors.newSingleThreadExecutor { r ->
        Thread(r, "zuofang-vpn-worker").apply { isDaemon = true }
    }
    private val lock = Any()
    private var tun: ParcelFileDescriptor? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action

        if (ACTION_STOP == action) {
            stopEngine("用户停止")
            return START_NOT_STICKY
        }
        if (isActive()) {
            return START_NOT_STICKY
        }

        status = "starting"
        lastReason = ""
        running.set(true)

        ensureChannel()
        startForegroundCompat()

        val config = intent?.getStringExtra(EXTRA_CONFIG) ?: "{}"
        worker.execute { bringUp(config) }
        return START_NOT_STICKY
    }

    /**
     * 解析分流名单。
     *
     * 返回 null 表示「全量」，否则返回要加入 allowedApplication 的包名集合。
     * 自身包名永远在集合内，否则应用无法和本地中继通信。
     */
    private fun resolveAllowedApps(configJson: String): List<String>? {
        val cfg = try {
            JSONObject(configJson)
        } catch (t: Throwable) {
            Log.w(TAG, "配置解析失败: ${t.message}")
            return listOf(packageName)
        }

        val mode = cfg.optString("split_mode", "apps")
        if (mode == "all") return null

        val out = LinkedHashSet<String>()
        out.add(packageName)
        val arr = cfg.optJSONArray("split_apps")
        if (arr != null) {
            for (i in 0 until arr.length()) {
                val p = arr.optString(i, "").trim()
                if (p.isNotEmpty()) out.add(p)
            }
        }
        // 没配白名单时按上游默认只代理目标游戏，避免全量转发吓到用户
        if (out.size == 1) {
            out.add(FALLBACK_PACKAGE)
        }
        return out.toList()
    }

    private fun bringUp(configJson: String) {
        try {
            if (!NativeBridge.isLoaded) {
                stopEngine("原生库加载失败")
                return
            }

            val prepared = NativeBridge.prepare(this, configJson)
            Log.i(TAG, "prepare -> $prepared")
            if (prepared.contains("\"state\":\"error\"")) {
                stopEngine("配置无效")
                return
            }

            val builder = Builder()
                .setSession("ZUOFANG")
                .setMtu(MTU)
                .addAddress(LOCAL_V4, 32)
                .addAddress(LOCAL_V6, 128)
                .addRoute("0.0.0.0", 0)
                .addRoute("::", 0)
                .addDnsServer(DNS_PRIMARY)

            val allowed = resolveAllowedApps(configJson)
            if (allowed != null) {
                for (pkg in allowed) {
                    try {
                        builder.addAllowedApplication(pkg)
                    } catch (e: Exception) {
                        Log.w(TAG, "addAllowedApplication($pkg) 失败: ${e.message}")
                    }
                }
            } else {
                Log.i(TAG, "分流模式 all：全量走隧道")
            }

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                builder.setMetered(false)
            }

            val pfd = builder.establish()
            if (pfd == null) {
                stopEngine("TUN 建立失败（授权缺失）")
                return
            }

            synchronized(lock) { tun = pfd }

            val started = NativeBridge.start(this, configJson)
            Log.i(TAG, "start -> $started")
            if (started.contains("\"state\":\"error\"")) {
                stopEngine("引擎启动失败")
                return
            }
            status = "running"
        } catch (t: Throwable) {
            Log.e(TAG, "bringUp 异常", t)
            stopEngine(t.message ?: "未知错误")
        }
    }

    private fun stopEngine(reason: String) {
        if (!running.compareAndSet(true, false)) {
            return
        }
        status = "stopped"
        lastReason = reason
        try {
            if (NativeBridge.isLoaded) NativeBridge.stop()
        } catch (t: Throwable) {
            Log.w(TAG, "NativeBridge.stop 异常: ${t.message}")
        }
        synchronized(lock) {
            try {
                tun?.close()
            } catch (_: Throwable) {
            }
            tun = null
        }
        stopForegroundCompat()
        stopSelf()
    }

    override fun onRevoke() {
        stopEngine("VPN 授权已撤销")
        super.onRevoke()
    }

    override fun onDestroy() {
        stopEngine("服务销毁")
        worker.shutdownNow()
        super.onDestroy()
    }

    // ---------- 供 Rust (JNI) 调用 ----------

    /**
     * 把 TUN fd 交给 `NativeBridge.start` 使用。
     * Rust 侧只做 read/write，不接管所有权。
     */
    @Keep
    fun tunFd(): Int = synchronized(lock) { tun?.fd ?: -1 }

    /**
     * 让 socket 走物理网卡，避免隧道自身流量回环进 TUN。
     * 由 Rust 的 `protect_via` 通过 JNI 调用。
     */
    @Keep
    fun protectSocket(fd: Int): Boolean = try {
        protect(fd)
    } catch (t: Throwable) {
        Log.w(TAG, "protect($fd) 失败: ${t.message}")
        false
    }

    // ---------- 通知 ----------

    private fun ensureChannel() {
        val mgr = getSystemService(NotificationManager::class.java)
        if (mgr.getNotificationChannel(CHANNEL_ID) == null) {
            mgr.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, CHANNEL_NAME, NotificationManager.IMPORTANCE_LOW)
                    .apply {
                        description = "本地流量转发运行状态"
                        setShowBadge(false)
                    }
            )
        }
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, ZfVpnService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("ZUOFANG")
            .setContentText("本地流量转发中")
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(0, "停止", stop)
            .build()
    }

    private fun startForegroundCompat() {
        val n = buildNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(
                NOTIF_ID, n,
                android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
            )
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION")
            stopForeground(true)
        }
    }
}
