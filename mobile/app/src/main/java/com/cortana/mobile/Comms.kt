package com.cortana.mobile

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Telephony
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject
import kotlin.concurrent.thread

/**
 * The phone half of the comms hub: mirroring this phone's notifications and
 * SMS to the workstation, and executing the commands it sends back.
 *
 * EVERY switch here defaults to off, and the phone - not the workstation - is
 * the authority on what may leave it. A cmd frame arriving for a capability
 * the user has not enabled is refused with a sentence saying so, never
 * silently dropped: a workstation that hears nothing cannot tell "off" from
 * "broken", and the last several rounds of debugging in this repo were lost
 * exactly there.
 *
 * Nothing is stored. The mirror is a bounded in-memory queue flushed to the
 * bridge and forgotten; SMS is read from the system provider on demand and
 * never copied to disk here.
 */
object Comms {

    /** Bounded on purpose. A chatty phone posts dozens of notifications a
     *  minute, the workstation only ever shows the recent ones, and this
     *  process can live for days. Dropping the oldest beats growing forever. */
    private const val MAX_QUEUE = 60

    /** One POST per burst rather than one per notification. Unlocking a phone
     *  after an hour away fires a dozen of these in the same second. */
    private const val FLUSH_MS = 20_000L

    private const val SMS_LIMIT = 20

    private val queue = ArrayList<JSONObject>()
    private val handler = Handler(Looper.getMainLooper())
    private var flushScheduled = false

    // -- notification mirror -------------------------------------------------
    fun mirror(ctx: Context, app: String, title: String, text: String, whenMs: Long) {
        if (!Prefs.commsNotifications(ctx)) return
        val item = JSONObject()
            .put("app", app)
            .put("title", title.take(200))
            .put("text", text.take(400))
            .put("ts", whenMs / 1000.0)
        synchronized(queue) {
            // The same app posting the same text again is a notification being
            // UPDATED - a progress bar, a now-playing row - not a new event.
            val dupe = queue.any {
                it.optString("app") == item.optString("app") &&
                    it.optString("text") == item.optString("text") &&
                    it.optString("title") == item.optString("title")
            }
            if (dupe) return
            queue.add(item)
            while (queue.size > MAX_QUEUE) queue.removeAt(0)
        }
        scheduleFlush(ctx.applicationContext)
    }

    private fun scheduleFlush(ctx: Context) {
        if (flushScheduled) return
        flushScheduled = true
        handler.postDelayed({
            flushScheduled = false
            flush(ctx)
        }, FLUSH_MS)
    }

    fun flush(ctx: Context) {
        val app = ctx.applicationContext
        if (!Prefs.paired(app)) return
        val batch = synchronized(queue) {
            if (queue.isEmpty()) return
            val b = ArrayList(queue)
            queue.clear()
            b
        }
        thread {
            try {
                LinkClient.postComms(app, JSONObject().put("notifications", JSONArray(batch)))
            } catch (e: Exception) {
                // A workstation that was briefly unreachable must not silently
                // eat the mirror - put the batch back at the front and let the
                // next flush carry it.
                synchronized(queue) {
                    queue.addAll(0, batch)
                    while (queue.size > MAX_QUEUE) queue.removeAt(queue.size - 1)
                }
            }
        }
    }

    /** Called from the service's ~15-minute doze tick. */
    fun tick(ctx: Context) {
        flush(ctx)
        syncSms(ctx)
    }

    private fun syncSms(ctx: Context) {
        val app = ctx.applicationContext
        if (!Prefs.smsRead(app) || !Prefs.paired(app)) return
        // readSms answers an empty list to "no telephony", "provider refused
        // the query" AND "the grant was never given", which is fine for the
        // cmd path - that one checks the grant itself and returns a sentence -
        // and a LIE here. An empty sms array is how the workstation is told
        // "your inbox is empty", so a revoked READ_SMS would have Cortana say
        // there are no messages, every fifteen minutes, forever. Say why
        // instead, and send no list at all.
        thread {
            // The provider query is a disk read and tick() runs on the
            // service's main thread, so BOTH halves stay on this worker.
            val body = JSONObject()
            if (!has(app, Manifest.permission.READ_SMS))
                body.put("smsError", "READ_SMS is switched on for Cortana but " +
                    "not granted on this phone, so no messages can be read")
            else
                body.put("sms", readSms(app, SMS_LIMIT))
            try {
                LinkClient.postComms(app, body)
            } catch (e: Exception) { /* the next tick retries */ }
        }
    }

    // -- SMS -----------------------------------------------------------------
    /**
     * Recent inbox messages, newest first. Read-only, through the system
     * provider - this app is not the default SMS handler and does not want to
     * be, which is also why sent messages are not mirrored.
     */
    fun readSms(ctx: Context, limit: Int): JSONArray {
        val out = JSONArray()
        if (!has(ctx, Manifest.permission.READ_SMS)) return out
        val cols = arrayOf("_id", "address", "body", "date", "read")
        val n = if (limit < 1) 1 else if (limit > 100) 100 else limit
        try {
            ctx.contentResolver.query(Telephony.Sms.Inbox.CONTENT_URI, cols,
                null, null, "date DESC LIMIT " + n)?.use { c ->
                while (c.moveToNext()) {
                    out.put(JSONObject()
                        .put("id", c.getString(0) ?: "")
                        .put("from", c.getString(1) ?: "")
                        .put("body", (c.getString(2) ?: "").take(600))
                        .put("ts", c.getLong(3) / 1000.0)
                        .put("unread", c.getInt(4) == 0))
                }
            }
        } catch (e: Exception) {
            // No telephony (tablet), a provider that refuses the LIMIT clause,
            // or the permission revoked between the check and the query. An
            // empty list is the right answer to all three.
        }
        return out
    }

    /** Returns null on success, a sentence on failure - the same shape the rest
     *  of this app uses for "the external thing may simply not be there". */
    @Suppress("DEPRECATION")
    fun sendSms(ctx: Context, to: String, body: String): String? {
        if (!Prefs.smsSend(ctx)) return "sending SMS is switched off in this phone's Settings"
        if (!has(ctx, Manifest.permission.SEND_SMS))
            return "SEND_SMS was not granted on this phone"
        if (to.isBlank()) return "no number to send to"
        if (body.isBlank()) return "no message to send"
        return try {
            val sm = if (Build.VERSION.SDK_INT >= 31)
                ctx.getSystemService(android.telephony.SmsManager::class.java)
            else
                android.telephony.SmsManager.getDefault()
            if (sm == null) return "this device has no SMS service"
            // divideMessage/sendMultipart rather than sendTextMessage: anything
            // Cortana writes is easily over one 160-character segment and a
            // single-part send would be truncated.
            sm.sendMultipartTextMessage(to, null, sm.divideMessage(body), null, null)
            null
        } catch (e: Exception) {
            e.message ?: "the send failed with no message"
        }
    }

    // -- commands from the workstation ---------------------------------------
    /**
     * {type:"cmd", id, cmd, args} arriving on the WebSocket. Executed on a
     * worker thread - the SMS provider query is a disk read and this is called
     * on the main thread - and the outcome is POSTed back so the workstation
     * never has to guess whether the phone acted.
     */
    fun handleCmd(ctx: Context, frame: JSONObject) {
        val app = ctx.applicationContext
        val id = frame.optString("id")
        val cmd = frame.optString("cmd")
        val args = frame.optJSONObject("args") ?: JSONObject()
        thread {
            var ok = false
            var error = ""
            var result: Any = JSONObject.NULL
            when (cmd) {
                "sms.send" -> {
                    val err = sendSms(app, args.optString("to"), args.optString("body"))
                    if (err == null) ok = true else error = err
                }
                "sms.read" -> when {
                    !Prefs.smsRead(app) ->
                        error = "reading SMS is switched off in this phone's Settings"
                    !has(app, Manifest.permission.READ_SMS) ->
                        error = "READ_SMS was not granted on this phone"
                    else -> {
                        result = readSms(app, args.optInt("limit", 10))
                        ok = true
                    }
                }
                else -> error = "this phone does not know the command " + cmd
            }
            val reply = JSONObject().put("id", id).put("cmd", cmd).put("ok", ok)
            if (error.isNotEmpty()) reply.put("error", error)
            if (ok) reply.put("result", result)
            try {
                LinkClient.postCmdResult(app, reply)
            } catch (e: Exception) {
                // Nothing useful to do: the workstation times the command out,
                // which is the correct outcome for a phone it cannot reach.
            }
        }
    }

    // -- grants --------------------------------------------------------------
    fun has(ctx: Context, permission: String): Boolean =
        ContextCompat.checkSelfPermission(ctx, permission) == PackageManager.PERMISSION_GRANTED

    /** Notification access is not a runtime permission - it is a per-app switch
     *  buried in system Settings that no app may request programmatically, only
     *  deep-link to. Settings shows this state so the toggle cannot look on
     *  while the grant is missing. */
    fun notificationAccessGranted(ctx: Context): Boolean = try {
        NotificationManagerCompat.getEnabledListenerPackages(ctx).contains(ctx.packageName)
    } catch (e: Exception) { false }

    /** One line for the Settings screen: exactly what is leaving right now. */
    fun describe(ctx: Context): String {
        val on = ArrayList<String>()
        if (Prefs.commsNotifications(ctx)) on.add("notifications")
        if (Prefs.smsRead(ctx)) on.add("SMS in")
        if (Prefs.smsSend(ctx)) on.add("SMS out")
        return if (on.isEmpty()) "nothing leaves this phone" else on.joinToString(" - ")
    }
}
