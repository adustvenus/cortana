package com.cortana.mobile

import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import androidx.core.app.NotificationCompat
import androidx.core.app.RemoteInput
import org.json.JSONObject
import kotlin.concurrent.thread

/**
 * The link, held open while the app is closed.
 *
 * LinkClient was foreground-only by design: activities called start/stop, so a
 * closed app had no socket and an announcement made while you were away was
 * only ever REPLAYED the next time you opened the app. That is fine for a
 * mirror and useless for a reminder - "your build finished" an hour late is
 * not a notification, it is a log entry. This service becomes another holder
 * of the same socket, so the announcement arrives when it is made and lands in
 * the notification shade with a Reply box on it.
 *
 * It does not replace the activity lifecycle - it joins it. MainActivity and
 * TalkActivity still start/stop the client; the socket now closes only when the
 * LAST holder lets go, which with this service running is never.
 *
 * WHAT CANNOT BE VERIFIED FROM A DEV BOX, stated plainly: whether the socket
 * actually survives a night of deep doze on THIS phone's OEM skin. Android's
 * documented behaviour is that a foreground service keeps the process alive
 * while network access is frozen in doze windows; OnePlus/OPPO/Xiaomi skins add
 * their own killers on top and none of that is simulable here. Two defences
 * are in place - a battery-optimisation exemption prompt in Settings, and an
 * AlarmManager setAndAllowWhileIdle backstop that pokes the socket every ~15
 * minutes - and neither of them is proof. The only proof is a phone left
 * overnight and an announcement fired at 4am. That is a test step, not a claim.
 */
class LinkService : Service(), LinkClient.Listener, LinkClient.Background {

    private val main = Handler(Looper.getMainLooper())
    private var netCallback: ConnectivityManager.NetworkCallback? = null
    private var attached = false

    // -- lifecycle -----------------------------------------------------------
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        // The notification is drawn in the board's accent, and a service can be
        // started into a cold process with no activity to have loaded it.
        Theme.load(this)
        channels()
        // startForegroundService gives five seconds to become foreground or the
        // system kills the process outright, so this happens before anything
        // that can block or throw.
        goForeground()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                // The Stop action must also turn the preference off, or
                // START_STICKY and the boot receiver would bring it straight
                // back and the button would look broken.
                Prefs.setBackground(this, false)
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_REPLY -> handleReply(intent)
            ACTION_TICK -> tick()
            else -> {}
        }
        attach()
        // scheduleTick() deliberately NOT called here. setAndAllowWhileIdle
        // REPLACES any pending alarm with the same PendingIntent, so arming it
        // on every start command meant every launch of the app, the widget or
        // Settings pushed the 15-minute tick another 15 minutes into the
        // future. Anyone using their phone normally never let it fire, so the
        // only scheduled /api/comms/sync and /api/presence never ran - which is
        // most of why "who texted" answered nothing and presence went stale.
        // It is armed once in attach() and re-armed by tick() after each fire.
        return START_STICKY
    }

    override fun onDestroy() {
        super.onDestroy()
        attached = false
        LinkClient.onCmd = null
        LinkClient.stop(this)
        unregisterNetwork()
        unregisterScreen()
        cancelTick()
    }

    private fun attach() {
        if (attached) return
        attached = true
        LinkClient.onCmd = { frame -> Comms.handleCmd(this, frame) }
        LinkClient.start(this, this)
        Presence.start(this)
        Comms.tick(this)
        registerNetwork()
        registerScreen()
        scheduleTick()
    }

    // -- notification channels -----------------------------------------------
    /**
     * Three channels, not one. A channel's importance belongs to the USER once
     * it exists - the app can lower it and never raise it - so there is no way
     * to pick "buzz" or "silent" per announcement after the fact. Splitting by
     * urgency up front is the only way that choice survives, and it also lets
     * someone mute ambient notes without muting alarms.
     */
    private fun channels() {
        val nm = getSystemService(NotificationManager::class.java) ?: return
        fun ch(id: String, name: String, importance: Int, desc: String) {
            try {
                val c = NotificationChannel(id, name, importance)
                c.description = desc
                nm.createNotificationChannel(c)
            } catch (e: Exception) { /* already exists, or an OEM refusal */ }
        }
        ch(CH_ONGOING, "Link", NotificationManager.IMPORTANCE_MIN,
            "The quiet permanent row saying the workstation link is up.")
        ch(CH_URGENT, "Urgent", NotificationManager.IMPORTANCE_HIGH,
            "Alarms, timers, and anything Cortana marked urgent or critical.")
        ch(CH_NORMAL, "Cortana", NotificationManager.IMPORTANCE_DEFAULT,
            "Reminders, finished background work, ordinary announcements.")
        ch(CH_QUIET, "Ambient", NotificationManager.IMPORTANCE_LOW,
            "Ambient notes - shown in the shade, never sounded.")
    }

    private fun goForeground() {
        try {
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(ONGOING_ID, ongoingNotification(),
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            } else {
                // Pre-34 the type comes from the manifest; passing the 34-only
                // SPECIAL_USE constant here would be rejected as a type the
                // platform cannot match.
                startForeground(ONGOING_ID, ongoingNotification())
            }
        } catch (e: Exception) {
            // Android 12+ refuses a foreground start from some background
            // states (ForegroundServiceStartNotAllowedException). Stopping
            // cleanly beats crashing; the next app launch starts us properly.
            stopSelf()
        }
    }

    private fun ongoingNotification(): Notification {
        val where = Prefs.dashName(this).ifEmpty { Prefs.host(this) }
            .ifEmpty { "the workstation" }
        val open = PendingIntent.getActivity(this, 1,
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val off = PendingIntent.getForegroundService(this, 2,
            Intent(this, LinkService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CH_ONGOING)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(
                if (LinkClient.linkUp) "Linked to " + where
                else "Reconnecting to " + where)
            .setContentText("Cortana can reach this phone while the app is closed.")
            .setOngoing(true)
            .setSilent(true)
            .setShowWhen(false)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setColor(Theme.accent)
            .setContentIntent(open)
            .addAction(0, "Stop", off)
            .build()
    }

    // -- LinkClient.Listener -------------------------------------------------
    override fun onState(state: JSONObject) { /* the service renders nothing */ }

    // Never reached: onAnnounceFull is overridden and is the only caller.
    override fun onAnnounce(text: String) {}

    override fun onLink(up: Boolean) {
        // Keep the permanent row honest. A row reading "Linked" over a dead
        // socket is exactly the failure mode this repo has been bitten by.
        try {
            getSystemService(NotificationManager::class.java)
                ?.notify(ONGOING_ID, ongoingNotification())
        } catch (e: Exception) { /* cosmetic */ }
    }

    override fun onAuthRejected() {
        post(TOKEN_ID, "Cortana link revoked",
            "This phone's access was revoked on the dashboard. Open the app and pair again.",
            CH_URGENT, replyable = false)
        Prefs.setBackground(this, false)
        stopSelf()
    }

    /**
     * An announcement arrived. If a screen is attached the app is already
     * showing it (a toast on the board, spoken on the talk screen), so posting
     * a notification too would double every line while the user is looking
     * straight at it. Only the closed-app case becomes a notification.
     */
    override fun onAnnounceFull(text: String, urgency: String, id: Int) {
        if (text.isBlank()) return
        if (LinkClient.uiHolders > 0) return
        val channel = when (urgency) {
            "critical", "urgent" -> CH_URGENT
            "ambient" -> CH_QUIET
            else -> CH_NORMAL
        }
        post(nextId(), "Cortana", text, channel, replyable = true)
    }

    // -- notifications -------------------------------------------------------
    private fun post(nid: Int, title: String, body: String, channel: String,
                     replyable: Boolean) {
        val open = PendingIntent.getActivity(this, nid,
            Intent(this, TalkActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val b = NotificationCompat.Builder(this, channel)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setColor(Theme.accent)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setContentIntent(open)
        if (replyable) {
            val remote = RemoteInput.Builder(KEY_REPLY).setLabel("Reply to Cortana").build()
            // MUTABLE is not optional here: the whole point of a RemoteInput is
            // that the system writes the typed text INTO this intent, and an
            // immutable PendingIntent silently discards the fill-in.
            val reply = PendingIntent.getForegroundService(this, nid,
                Intent(this, LinkService::class.java).setAction(ACTION_REPLY)
                    .putExtra(EXTRA_NID, nid),
                mutableFlags())
            b.addAction(NotificationCompat.Action.Builder(0, "Reply", reply)
                .addRemoteInput(remote)
                .setAllowGeneratedReplies(false)
                .build())
        }
        try {
            getSystemService(NotificationManager::class.java)?.notify(nid, b.build())
        } catch (e: Exception) {
            // POST_NOTIFICATIONS not granted (33+) or the OEM refused. The
            // announcement is still in the app; Settings offers the grant.
        }
    }

    /**
     * A reply typed into the notification shade. Handled by the SERVICE rather
     * than by a BroadcastReceiver on purpose: a receiver gets about ten seconds
     * and a real voice turn can take minutes, so the answer would be killed
     * mid-turn and the user would see nothing at all.
     */
    private fun handleReply(intent: Intent) {
        val nid = intent.getIntExtra(EXTRA_NID, nextId())
        val typed = try {
            RemoteInput.getResultsFromIntent(intent)
                ?.getCharSequence(KEY_REPLY)?.toString()?.trim()
        } catch (e: Exception) { null }
        if (typed == null || typed.isEmpty()) return
        // Replace the row at once. A notification that keeps its Reply box open
        // after you hit send looks like it swallowed the message.
        post(nid, "You: " + typed, "sending to the workstation...", CH_QUIET,
            replyable = false)
        thread {
            val answer = try {
                val r = LinkClient.converse(this, null, typed)
                r.optString("reply").ifEmpty {
                    r.optString("error").ifEmpty { "no reply" }
                }
            } catch (e: LinkClient.AuthException) {
                "This phone's access was revoked on the dashboard - pair again from Settings."
            } catch (e: Exception) {
                "I couldn't reach the workstation. [" + e.message + "]"
            }
            main.post { post(nid, "Cortana", answer, CH_NORMAL, replyable = true) }
        }
    }

    // -- doze backstop -------------------------------------------------------
    /**
     * LinkClient's reconnect is Handler.postDelayed, which does not run in deep
     * doze - a socket dropped at 2am would stay dropped until the phone was
     * unlocked. setAndAllowWhileIdle is the only timer the platform still
     * honours there. It is throttled to roughly one firing every nine minutes,
     * so fifteen sits comfortably inside the budget.
     */
    private fun tickIntent(): PendingIntent = PendingIntent.getForegroundService(this, 3,
        Intent(this, LinkService::class.java).setAction(ACTION_TICK),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

    private fun scheduleTick() {
        try {
            getSystemService(AlarmManager::class.java)?.setAndAllowWhileIdle(
                AlarmManager.ELAPSED_REALTIME_WAKEUP,
                SystemClock.elapsedRealtime() + TICK_MS, tickIntent())
        } catch (e: Exception) { /* alarm quota; the socket may still be fine */ }
    }

    private fun cancelTick() {
        try { getSystemService(AlarmManager::class.java)?.cancel(tickIntent()) }
        catch (e: Exception) { }
    }

    private fun tick() {
        LinkClient.poke(this)
        Presence.tick(this)
        Comms.tick(this)
        // Re-arm HERE, where a fire has actually happened. setAndAllowWhileIdle
        // is one-shot, so without this the tick runs exactly once per service
        // lifetime.
        scheduleTick()
    }

    // Screen on/off cannot be declared in a manifest - they are runtime-only
    // broadcasts. Without this, `screenOn` was sampled only when some OTHER
    // field changed or on the 30-minute heartbeat, so locking the phone told
    // the workstation nothing and presence stayed wrong for half an hour.
    private var screenReceiver: BroadcastReceiver? = null

    private fun registerScreen() {
        if (screenReceiver != null || !Prefs.presenceOn(this)) return
        val r = object : BroadcastReceiver() {
            override fun onReceive(c: Context?, i: Intent?) { Presence.report(this@LinkService) }
        }
        val f = IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_SCREEN_OFF)
        }
        try { registerReceiver(r, f); screenReceiver = r } catch (e: Exception) { }
    }

    private fun unregisterScreen() {
        val r = screenReceiver ?: return
        screenReceiver = null
        try { unregisterReceiver(r) } catch (e: Exception) { }
    }

    private fun registerNetwork() {
        if (netCallback != null) return
        val cm = getSystemService(ConnectivityManager::class.java) ?: return
        val cb = object : ConnectivityManager.NetworkCallback() {
            // Wi-Fi to LTE and back is the ordinary case, not the exotic one.
            // Reconnecting the instant a network appears beats waiting out a
            // backoff that may be sitting at thirty seconds.
            override fun onAvailable(network: Network) {
                LinkClient.poke(this@LinkService)
            }
        }
        try {
            cm.registerDefaultNetworkCallback(cb)
            netCallback = cb
        } catch (e: Exception) { netCallback = null }
    }

    private fun unregisterNetwork() {
        val cb = netCallback ?: return
        netCallback = null
        try {
            getSystemService(ConnectivityManager::class.java)?.unregisterNetworkCallback(cb)
        } catch (e: Exception) { }
    }

    private fun mutableFlags(): Int =
        if (Build.VERSION.SDK_INT >= 31)
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
        else PendingIntent.FLAG_UPDATE_CURRENT

    companion object {
        const val CH_ONGOING = "cortana.link"
        const val CH_URGENT = "cortana.urgent"
        const val CH_NORMAL = "cortana.normal"
        const val CH_QUIET = "cortana.quiet"

        const val ACTION_STOP = "com.cortana.mobile.action.LINK_STOP"
        const val ACTION_REPLY = "com.cortana.mobile.action.LINK_REPLY"
        const val ACTION_TICK = "com.cortana.mobile.action.LINK_TICK"
        const val KEY_REPLY = "cortana.reply"
        const val EXTRA_NID = "nid"

        private const val ONGOING_ID = 1
        private const val TOKEN_ID = 2
        private const val TICK_MS = 15L * 60L * 1000L

        // Announcement ids cycle rather than grow: the shade should show the
        // last few lines, not one row per announcement since install.
        private var seq = 100

        private fun nextId(): Int {
            seq += 1
            if (seq > 9000) seq = 101
            return seq
        }

        /** Start the service if the user asked for it and the phone is paired.
         *  Safe to call from anywhere, any number of times. */
        fun start(ctx: Context) {
            if (!Prefs.background(ctx) || !Prefs.paired(ctx)) return
            try {
                ctx.applicationContext.startForegroundService(
                    Intent(ctx.applicationContext, LinkService::class.java))
            } catch (e: Exception) {
                // Refused from the background on 12+. Not fatal: the next time
                // an activity is on screen, sync() succeeds.
            }
        }

        fun stop(ctx: Context) {
            try {
                ctx.applicationContext.stopService(
                    Intent(ctx.applicationContext, LinkService::class.java))
            } catch (e: Exception) { }
        }

        /** Make the running state match the preference. Called from every
         *  activity's onStart, so flipping the switch anywhere takes effect. */
        fun sync(ctx: Context) {
            if (Prefs.background(ctx) && Prefs.paired(ctx)) start(ctx) else stop(ctx)
        }
    }
}
