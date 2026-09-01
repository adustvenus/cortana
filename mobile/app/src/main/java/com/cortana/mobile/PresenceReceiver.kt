package com.cortana.mobile

import android.bluetooth.BluetoothClass
import android.bluetooth.BluetoothDevice
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.location.Location
import android.location.LocationManager

/**
 * Every presence signal lands here: a coarse location fix (delivered to this
 * component's PendingIntent by LocationManager), the charger going in or out,
 * and the car stereo connecting or disconnecting.
 *
 * It is declared DISABLED in the manifest and enabled only while the presence
 * switch is on. An `if (off) return` guard would have been simpler and would
 * still have cost a process start on every Bluetooth connect for every user
 * who never turned presence on - the same class of idle burn as the poller
 * that spawned 24 systemctl processes a minute and had to be walked back.
 *
 * It is exported because the system's implicit broadcasts (ACL_CONNECTED,
 * ACTION_POWER_CONNECTED) are not delivered to non-exported receivers. That
 * means another app on the phone could forge one. The worst it can do is make
 * this phone claim to be driving, so the trade is accepted rather than hidden.
 */
class PresenceReceiver : BroadcastReceiver() {

    override fun onReceive(ctx: Context, intent: Intent) {
        // The component can still be enabled from a previous session after the
        // switch was turned off in another process, so re-check.
        if (!Prefs.presenceOn(ctx)) return
        // The POST runs on a worker thread, and a receiver's process may be
        // reclaimed the moment onReceive returns - so without goAsync() the
        // send is racing the process death that follows it. This is exactly
        // the case that would have looked like "presence works when the app is
        // open and not otherwise", with nothing in any log to say why.
        val pending = goAsync()
        val app = ctx.applicationContext
        val done = { pending.finish() }
        when (intent.action) {
            Intent.ACTION_POWER_CONNECTED, Intent.ACTION_POWER_DISCONNECTED ->
                Presence.onPower(app, done)
            BluetoothDevice.ACTION_ACL_CONNECTED ->
                if (isCar(intent)) Presence.onDriving(app, true, done) else done()
            BluetoothDevice.ACTION_ACL_DISCONNECTED ->
                if (isCar(intent)) Presence.onDriving(app, false, done) else done()
            else -> fix(app, intent, done)   // ACTION_FIX, and anything unlabelled
        }
    }

    private fun fix(ctx: Context, intent: Intent, done: () -> Unit) {
        val loc = try {
            @Suppress("DEPRECATION")
            intent.getParcelableExtra<Location>(LocationManager.KEY_LOCATION_CHANGED)
        } catch (e: Exception) { null }
        if (loc == null) done() else Presence.onFix(ctx, loc, done)
    }

    /**
     * Car audio or a hands-free unit connecting is the cheapest "am I driving"
     * signal a phone has - no motion sensors, no activity recognition library,
     * no polling.
     *
     * The class is read from the broadcast's own EXTRA_CLASS rather than from
     * the device object, because asking the DEVICE for its class needs
     * BLUETOOTH_CONNECT to have been granted at that instant while the extra is
     * already in hand. (The permission is still declared: from API 31 it is
     * required to RECEIVE this broadcast at all.)
     */
    private fun isCar(intent: Intent): Boolean {
        val cls = try {
            @Suppress("DEPRECATION")
            intent.getParcelableExtra<BluetoothClass>(BluetoothDevice.EXTRA_CLASS)
        } catch (e: Exception) { null } ?: return false
        return try {
            when (cls.deviceClass) {
                BluetoothClass.Device.AUDIO_VIDEO_CAR_AUDIO,
                BluetoothClass.Device.AUDIO_VIDEO_HANDSFREE -> true
                else -> false
            }
        } catch (e: Exception) { false }
    }

    companion object {
        const val ACTION_FIX = "com.cortana.mobile.action.PRESENCE_FIX"
    }
}
