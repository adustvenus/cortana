package com.cortana.mobile

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/**
 * Mirrors this phone's notifications to the workstation, so Cortana can answer
 * "what did I miss" without the phone being in your hand.
 *
 * Notification access is the most privileged thing this app asks for - it can
 * read every notification on the phone, including other people's messages -
 * so it is gated twice: the system grant (which only the user can give, in
 * system Settings, and which no app may request programmatically) AND the
 * app's own switch, default off. Turning the switch off stops the mirror
 * immediately even though the system grant is still in place.
 *
 * Android binds this service as soon as the grant exists, whether or not the
 * switch is on. Everything therefore has to be decided in onNotificationPosted;
 * there is no "don't run" to arrange.
 */
class NotificationMirror : NotificationListenerService() {

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        if (!Prefs.commsNotifications(this)) return
        // Never mirror our own: an announcement notification posted by
        // LinkService would be read back to the workstation that sent it.
        if (sbn.packageName == packageName) return
        val notif = sbn.notification ?: return
        // Ongoing rows are media players, downloads and other apps' foreground
        // services - state, not events - and they re-post constantly. Group
        // summaries are duplicates of the children by construction.
        if ((notif.flags and Notification.FLAG_ONGOING_EVENT) != 0) return
        if ((notif.flags and Notification.FLAG_GROUP_SUMMARY) != 0) return
        val extras = notif.extras ?: return
        val title = try {
            extras.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: ""
        } catch (e: Exception) { "" }
        val text = try {
            extras.getCharSequence(Notification.EXTRA_TEXT)?.toString() ?: ""
        } catch (e: Exception) { "" }
        if (title.isEmpty() && text.isEmpty()) return
        Comms.mirror(this, appLabel(sbn.packageName), title, text, sbn.postTime)
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification) {
        // Dismissals are deliberately not mirrored. The workstation shows a
        // recent-events list, not a live copy of the shade, and sending both
        // halves would double the traffic for no readable difference.
    }

    /** The app's display name, because "com.whatsapp" is not something anyone
     *  wants read aloud. Falls back to the package name if the label is gone
     *  (the app was uninstalled between the post and this call). */
    private fun appLabel(pkg: String): String = try {
        val pm = packageManager
        pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
    } catch (e: Exception) { pkg }
}
