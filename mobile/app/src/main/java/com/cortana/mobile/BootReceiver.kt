package com.cortana.mobile

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * A foreground service does not survive a reboot or an app update on its own,
 * and the entire point of the background link is that an announcement reaches
 * the phone without anyone opening the app. Without this, the first reboot
 * silently turns the feature off and the switch keeps saying it is on.
 *
 * Both preferences are re-checked here rather than assumed: this fires for
 * every install, including the ones where nothing was ever switched on.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(ctx: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,
            "android.intent.action.QUICKBOOT_POWERON" -> {}
            else -> return
        }
        // BOOT_COMPLETED is one of the states from which a foreground service
        // start is still allowed on Android 12+, so this is not the refused
        // background-start case.
        if (Prefs.background(ctx)) LinkService.start(ctx)
        if (Prefs.presenceOn(ctx)) Presence.start(ctx)
    }
}
