package dev.localvision.local_vision

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log

/** Advertises the active foreground phone session. Pairing stays on HTTPS. */
internal class DiscoveryAdvertiser(context: Context) {
    private val nsd = context.getSystemService(NsdManager::class.java)
    private val main = Handler(Looper.getMainLooper())
    private var foreground = false
    private var requested: NsdServiceInfo? = null
    private var listener: NsdManager.RegistrationListener? = null

    fun configure(sessionPort: Int?, fingerprint: String?) {
        requested = if (sessionPort != null && fingerprint != null) {
            NsdServiceInfo().apply {
                serviceName = "Local Vision ${Build.MODEL}".take(40)
                serviceType = "_localvision._tcp"
                setPort(sessionPort)
                setAttribute("v", "2")
                setAttribute("sha256", fingerprint.replace(":", ""))
            }
        } else null
        reconcile()
    }

    fun setForeground(active: Boolean) {
        foreground = active
        reconcile()
    }

    private fun unregister(registration: NsdManager.RegistrationListener) {
        try {
            nsd?.unregisterService(registration)
        } catch (e: IllegalArgumentException) {
            Log.d("LVC", "Discovery registration already ended")
        }
    }

    private fun reconcile() {
        listener?.let(::unregister)
        listener = null
        val service = requested ?: return
        if (!foreground || nsd == null) return
        val registration = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(info: NsdServiceInfo) {
                main.post {
                    if (listener !== this) unregister(this)
                    else Log.i("LVC", "Local Vision discovery registered")
                }
            }

            override fun onRegistrationFailed(info: NsdServiceInfo, error: Int) {
                main.post {
                    if (listener === this) listener = null
                    Log.w("LVC", "Local Vision discovery registration failed: $error")
                }
            }

            override fun onServiceUnregistered(info: NsdServiceInfo) = Unit
            override fun onUnregistrationFailed(info: NsdServiceInfo, error: Int) {
                Log.d("LVC", "Local Vision discovery unregister failed: $error")
            }
        }
        listener = registration
        try {
            nsd.registerService(service, NsdManager.PROTOCOL_DNS_SD, registration)
        } catch (e: RuntimeException) {
            listener = null
            Log.w("LVC", "Local Vision discovery unavailable", e)
        }
    }
}
