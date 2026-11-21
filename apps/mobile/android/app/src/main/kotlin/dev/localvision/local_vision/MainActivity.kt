package dev.localvision.local_vision

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import android.os.StatFs
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "dev.localvision/device")
            .setMethodCallHandler { call, result ->
                if (call.method == "getDeviceStatus") result.success(deviceStatus())
                else result.notImplemented()
            }
        EventChannel(flutterEngine.dartExecutor.binaryMessenger, "dev.localvision/device_events")
            .setStreamHandler(object : EventChannel.StreamHandler {
                override fun onListen(args: Any?, sink: EventChannel.EventSink) {
                    val r = object : BroadcastReceiver() {
                        override fun onReceive(c: Context, intent: Intent) = sink.success(battery())
                    }
                    val f = IntentFilter().apply {
                        addAction(Intent.ACTION_BATTERY_CHANGED)
                        addAction(Intent.ACTION_POWER_CONNECTED)
                        addAction(Intent.ACTION_POWER_DISCONNECTED)
                    }
                    registerReceiver(r, f)
                    batteryReceiver = r
                }

                override fun onCancel(args: Any?) {
                    batteryReceiver?.let { unregisterReceiver(it) }
                    batteryReceiver = null
                }
            })
    }

    override fun onDestroy() {
        batteryReceiver?.let { unregisterReceiver(it) }
        batteryReceiver = null
        super.onDestroy()
    }

    private var batteryReceiver: BroadcastReceiver? = null

    // Sticky ACTION_BATTERY_CHANGED is current; BatteryManager.isCharging lags plug events.
    private fun battery(): Map<String, Any?> {
        val i = registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            ?: return mapOf("batteryPercent" to null, "charging" to null, "plugged" to null)
        val level = i.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = i.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
        val plugged = i.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0
        val status = i.getIntExtra(BatteryManager.EXTRA_STATUS, BatteryManager.BATTERY_STATUS_UNKNOWN)
        val charging = when (status) {
            BatteryManager.BATTERY_STATUS_CHARGING -> true
            BatteryManager.BATTERY_STATUS_FULL -> plugged
            BatteryManager.BATTERY_STATUS_DISCHARGING, BatteryManager.BATTERY_STATUS_NOT_CHARGING -> false
            else -> null
        }
        return mapOf(
            "batteryPercent" to (if (level >= 0 && scale > 0) level * 100 / scale else null),
            "charging" to charging,
            "plugged" to plugged,
        )
    }

    private fun deviceStatus(): Map<String, Any?> {
        val thermal = if (Build.VERSION.SDK_INT >= 29) {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            when (pm.currentThermalStatus) {
                PowerManager.THERMAL_STATUS_NONE -> "none"
                PowerManager.THERMAL_STATUS_LIGHT -> "light"
                PowerManager.THERMAL_STATUS_MODERATE -> "moderate"
                PowerManager.THERMAL_STATUS_SEVERE -> "severe"
                PowerManager.THERMAL_STATUS_CRITICAL -> "critical"
                PowerManager.THERMAL_STATUS_EMERGENCY -> "emergency"
                PowerManager.THERMAL_STATUS_SHUTDOWN -> "shutdown"
                else -> null
            }
        } else null
        val fs = StatFs(filesDir.absolutePath)
        return battery() + mapOf(
            "model" to Build.MODEL,
            "manufacturer" to Build.MANUFACTURER,
            "osRelease" to Build.VERSION.RELEASE,
            "sdkInt" to Build.VERSION.SDK_INT,
            "thermal" to thermal,
            "storageFreeBytes" to fs.availableBytes,
            "storageTotalBytes" to fs.totalBytes,
        )
    }
}
