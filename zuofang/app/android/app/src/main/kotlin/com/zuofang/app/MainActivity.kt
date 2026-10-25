package com.zuofang.app

import android.app.Activity
import android.content.Intent
import android.net.VpnService
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.util.concurrent.Executors

/**
 * Flutter 宿主。
 *
 * 职责（与上游一致）：
 *  1. 处理 VpnService 授权（requestCode 771）；
 *  2. 把 Dart 的 start/stop 转发成 [ZfVpnService] 的启停；
 *  3. status/captureStatus 直接读 [NativeBridge]（只读，不建隧道）。
 *
 * 真正的 TUN 建立与 Rust 启动在 [ZfVpnService.bringUp] 里完成。
 */
class MainActivity : FlutterActivity() {

    companion object {
        private const val CHANNEL = "com.zuofang.app/engine"
        private const val REQ_VPN = 771
        private const val START_WAIT_MS = 6000L
    }

    private val poller = Executors.newSingleThreadExecutor { r ->
        Thread(r, "zuofang-status-poll").apply { isDaemon = true }
    }
    private var pendingResult: MethodChannel.Result? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "prepare" -> requestVpnPermission(result)

                    "start" -> {
                        val config = call.argument<String>("config") ?: "{}"
                        val i = Intent(this, ZfVpnService::class.java)
                            .putExtra(ZfVpnService.EXTRA_CONFIG, config)
                        startForegroundService(i)
                        awaitRunning(result)
                    }

                    // 原生库缺失时（例如还没交叉编译 libzuofang_native.so）也要能跑 UI
                    "status" -> result.success(
                        if (NativeBridge.isLoaded) NativeBridge.status()
                        else "{\"state\":\"stopped\",\"reason\":\"原生库未加载\"}"
                    )

                    "stop" -> {
                        val i = Intent(this, ZfVpnService::class.java)
                            .setAction(ZfVpnService.ACTION_STOP)
                        startService(i)
                        result.success(
                            if (NativeBridge.isLoaded) NativeBridge.stop()
                            else "{\"state\":\"stopped\"}"
                        )
                    }

                    "captureStatus" -> result.success(
                        if (NativeBridge.isLoaded) NativeBridge.captureStatus()
                        else "{}"
                    )

                    "listApps" -> listApps(result)

                    else -> result.notImplemented()
                }
            }
    }

    /** 启动服务是异步的，这里轮询到 running/error 再回给 Dart。 */
    private fun awaitRunning(result: MethodChannel.Result) {
        poller.execute {
            val deadline = System.currentTimeMillis() + START_WAIT_MS
            var last = "{}"
            while (System.currentTimeMillis() < deadline) {
                last = if (NativeBridge.isLoaded) NativeBridge.status() else "{\"state\":\"error\",\"reason\":\"原生库加载失败\"}"
                if (last.contains("\"state\":\"running\"") ||
                    last.contains("\"state\":\"error\"") ||
                    last.contains("\"state\":\"stopped\"")
                ) break
                Thread.sleep(120)
            }
            runOnUiThread { result.success(last) }
        }
    }

    /**
     * 列出可作为分流目标的应用。
     *
     * 自己必须排除：把自身包名塞进白名单是没意义的（Service 里已经强制加），
     * 而让用户去勾自己只会混淆。
     */
    private fun listApps(result: MethodChannel.Result) {
        poller.execute {
            val arr = org.json.JSONArray()
            try {
                val pm = packageManager
                val seen = HashSet<String>()
                for (info in pm.getInstalledPackages(0)) {
                    val pkg = info.packageName ?: continue
                    if (pkg == packageName || !seen.add(pkg)) continue
                    val isSystem = (info.applicationInfo?.flags ?: 0) and
                        android.content.pm.ApplicationInfo.FLAG_SYSTEM != 0
                    val label = runCatching {
                        pm.getApplicationLabel(info.applicationInfo!!).toString()
                    }.getOrDefault(pkg)
                    arr.put(
                        org.json.JSONObject()
                            .put("package", pkg)
                            .put("label", label)
                            .put("system", isSystem)
                    )
                }
            } catch (t: Throwable) {
                runOnUiThread { result.error("listApps", t.message, null) }
                return@execute
            }
            runOnUiThread { result.success(arr.toString()) }
        }
    }

    private fun requestVpnPermission(result: MethodChannel.Result) {
        val intent = VpnService.prepare(this)
        if (intent == null) {
            result.success(true)
            return
        }
        pendingResult = result
        @Suppress("DEPRECATION")
        startActivityForResult(intent, REQ_VPN)
    }

    @Deprecated("VpnService 授权回调")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_VPN) return
        val granted = resultCode == Activity.RESULT_OK
        pendingResult?.success(granted)
        pendingResult = null
    }

    override fun onDestroy() {
        pendingResult = null
        poller.shutdownNow()
        super.onDestroy()
    }
}
