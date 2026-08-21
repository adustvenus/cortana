package com.cortana.mobile

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.widget.Toast
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat

/**
 * Decides HOW a completion from Cortana is shown, based on what the phone is
 * doing right now:
 *
 *   - app not in the foreground   -> notification banner
 *   - in the app, another screen  -> toast
 *   - on the AI screen (Talk)     -> inline, rendered by TalkActivity
 *
 * The bridge deliberately sends only the text. Presentation cannot be decided
 * on the workstation because only the phone knows whether it is foregrounded
 * and which screen is open.
 *
 * Foreground state is tracked from each Activity's onResume/onPause rather
 * than an Application subclass, so nothing else in the app has to change.
 */
object Announcer {
    const val CHANNEL = "cortana.completions"
    private const val NOTIF_ID = 4201

    const val SCREEN_TALK = "talk"

    @Volatile private var resumed: String? = null

    /** Set while TalkActivity is showing, so it can render inline instead. */
    @Volatile var inlineSink: ((String) -> Unit)? = null

    fun onResume(screen: String) { resumed = screen }

    fun onPause(screen: String) {
        // Guarded: on an A -> B transition B resumes before A pauses, and an
        // unguarded clear would wipe B's state and misroute to a banner while
        // the app is plainly in the foreground.
        if (resumed == screen) resumed = null
    }

    fun deliver(ctx: Context, text: String) {
        val body = text.trim()
        if (body.isEmpty()) return
        val sink = inlineSink
        when {
            resumed == SCREEN_TALK && sink != null -> sink(body)
            resumed != null -> Toast.makeText(ctx, "Cortana: $body", Toast.LENGTH_LONG).show()
            else -> banner(ctx, body)
        }
    }

    fun ensureChannel(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        if (nm.getNotificationChannel(CHANNEL) != null) return
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "Task completions",
                                NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Cortana reporting finished background work"
            })
    }

    /** True when a banner can actually be posted (API < 33 always can). */
    fun canPostBanner(ctx: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            ContextCompat.checkSelfPermission(
                ctx, android.Manifest.permission.POST_NOTIFICATIONS
            ) == PackageManager.PERMISSION_GRANTED

    private fun banner(ctx: Context, text: String) {
        if (!canPostBanner(ctx)) return
        ensureChannel(ctx)
        val open = PendingIntent.getActivity(
            ctx, 0,
            Intent(ctx, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val n = NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle("Cortana")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        try {
            NotificationManagerCompat.from(ctx).notify(NOTIF_ID, n)
        } catch (e: SecurityException) {
            // Permission revoked between the check and the post. Losing the
            // banner is acceptable; crashing on a background thread is not.
        }
    }
}
