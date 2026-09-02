package com.cortana.mobile

import android.Manifest
import android.app.PendingIntent
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationManager
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import androidx.core.content.ContextCompat
import org.json.JSONObject
import kotlin.concurrent.thread
import kotlin.math.round

/**
 * What this phone tells the workstation about where its owner is.
 *
 * DEFAULT OFF, and the honest description of what leaves the phone when it is
 * on lives next to the switch in Settings, not here. On the wire:
 * {place, zone, lat, lon, charging, driving, screenOn} to POST /api/presence.
 *
 * Deliberate frugality, because this runs on a phone and the workstation it
 * reports to is a 5GB laptop:
 *
 *  - COARSE location only. The question is "is he home", not "which room".
 *    Coordinates go out rounded to three decimals (~110m), so the workstation
 *    never accumulates anything finer than the answer needs.
 *  - No continuous listener. Location arrives through a PendingIntent, which
 *    means the platform delivers the fix and this process does not have to be
 *    alive waiting for it - and it dodges the LocationListener default-method
 *    difference between API 26 and 30 entirely.
 *  - Every input is an EVENT (a fix, a charger, a car stereo), plus one
 *    heartbeat every half hour so a workstation that restarted learns the
 *    current answer. A phone that sits on a charger overnight in one place
 *    sends two payloads a night, not 480.
 *  - The broadcast receiver is a DISABLED manifest component until the switch
 *    is turned on. A `return if off` guard would still cost a process start on
 *    every Bluetooth connect; this costs nothing at all.
 *
 * All persistent state lives in Prefs rather than in fields here. The receiver
 * frequently runs in a freshly started process with no service and no
 * activity, and in-memory state would simply be gone.
 */
object Presence {

    /** How far from a saved point still counts as being there. A network fix is
     *  good to a few hundred metres, so a tight radius would flap between home
     *  and out all evening and every flap is a push. */
    const val ZONE_RADIUS_M = 300f

    /** Resend even when nothing changed, so a workstation that restarted is not
     *  stuck on "unknown" until the next time the user moves. */
    private const val HEARTBEAT_MS = 30L * 60L * 1000L

    private const val REQUEST_MS = 15L * 60L * 1000L
    private const val REQUEST_METRES = 250f

    // -- wiring --------------------------------------------------------------
    fun start(ctx: Context) {
        if (!Prefs.presenceOn(ctx)) { stop(ctx); return }
        setReceiverEnabled(ctx, true)
        requestUpdates(ctx)
        report(ctx)
    }

    fun stop(ctx: Context) {
        removeUpdates(ctx)
        setReceiverEnabled(ctx, false)
    }

    /** Called from the service's ~15-minute doze tick. Re-requests updates as
     *  well as reporting: a process that was killed loses its location request
     *  along with everything else, and nothing else would ever put it back. */
    fun tick(ctx: Context) {
        if (!Prefs.presenceOn(ctx)) return
        requestUpdates(ctx)
        report(ctx)
    }

    // `done` exists for PresenceReceiver, which holds its process alive with
    // goAsync() until the POST has actually been attempted. It has to be
    // invoked on EVERY path, including the ones that decide not to send at
    // all, or the broadcast is never finished and the receiver is charged with
    // a timeout.
    fun onFix(ctx: Context, loc: Location, done: (() -> Unit)? = null) {
        Prefs.setPresenceFix(ctx, loc.latitude, loc.longitude)
        report(ctx, done)
    }

    fun onDriving(ctx: Context, driving: Boolean, done: (() -> Unit)? = null) {
        if (Prefs.presenceDriving(ctx) == driving) { done?.invoke(); return }
        Prefs.setPresenceDriving(ctx, driving)
        report(ctx, done)
    }

    fun onPower(ctx: Context, done: (() -> Unit)? = null) = report(ctx, done)

    // -- location ------------------------------------------------------------
    /** MUTABLE on purpose. LocationManager delivers a fix by filling the extras
     *  of this intent, and an immutable PendingIntent discards the fill-in
     *  silently - you get callbacks with no location in them. */
    private fun pending(ctx: Context): PendingIntent = PendingIntent.getBroadcast(
        ctx.applicationContext, 7,
        Intent(ctx.applicationContext, PresenceReceiver::class.java)
            .setAction(PresenceReceiver.ACTION_FIX),
        if (Build.VERSION.SDK_INT >= 31)
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
        else PendingIntent.FLAG_UPDATE_CURRENT)

    private fun requestUpdates(ctx: Context) {
        val app = ctx.applicationContext
        if (!hasLocation(app)) {
            Prefs.setPresenceError(app, "location permission not granted")
            return
        }
        val lm = app.getSystemService(LocationManager::class.java)
        if (lm == null) {
            Prefs.setPresenceError(app, "this device has no location service")
            return
        }
        // NETWORK first, then PASSIVE. GPS is deliberately never requested: it
        // is the expensive one and it answers a question finer than the one
        // being asked.
        val providers = try { lm.allProviders } catch (e: Exception) { emptyList<String>() }
        val provider = when {
            providers.contains(LocationManager.NETWORK_PROVIDER) -> LocationManager.NETWORK_PROVIDER
            providers.contains(LocationManager.PASSIVE_PROVIDER) -> LocationManager.PASSIVE_PROVIDER
            else -> {
                Prefs.setPresenceError(app, "no coarse location provider on this phone")
                return
            }
        }
        try {
            lm.requestLocationUpdates(provider, REQUEST_MS, REQUEST_METRES, pending(app))
            Prefs.setPresenceError(app, "")
        } catch (e: SecurityException) {
            Prefs.setPresenceError(app, "location permission was revoked")
        } catch (e: Exception) {
            Prefs.setPresenceError(app, e.message ?: "location unavailable")
        }
    }

    private fun removeUpdates(ctx: Context) {
        try {
            ctx.applicationContext.getSystemService(LocationManager::class.java)
                ?.removeUpdates(pending(ctx))
        } catch (e: Exception) { }
    }

    private fun setReceiverEnabled(ctx: Context, on: Boolean) {
        try {
            ctx.applicationContext.packageManager.setComponentEnabledSetting(
                ComponentName(ctx.applicationContext, PresenceReceiver::class.java),
                if (on) PackageManager.COMPONENT_ENABLED_STATE_ENABLED
                else PackageManager.COMPONENT_ENABLED_STATE_DISABLED,
                PackageManager.DONT_KILL_APP)
        } catch (e: Exception) { }
    }

    // -- reporting -----------------------------------------------------------
    /**
     * Send the snapshot if it says something new, or if the last one is old
     * enough that the workstation should hear it again. Nothing is sent while
     * the switch is off or the phone is unpaired, both of which are checked
     * here rather than at the call sites - there are six of those and one of
     * them would eventually be missed.
     */
    fun report(ctx: Context, done: (() -> Unit)? = null) {
        val app = ctx.applicationContext
        if (!Prefs.presenceOn(app) || !Prefs.paired(app)) { done?.invoke(); return }
        val body = snapshot(app)
        // screenOn belongs in the signature. It is the field the workstation
        // now uses to tell "the user is looking at the phone" from "the
        // service is holding a socket in a pocket", and leaving it out meant a
        // lock or unlock produced no report at all - the value the bridge held
        // was whatever the screen happened to be doing at the last place or
        // charging change, routinely half an hour stale.
        val sig = body.optString("place") + "|" + body.optString("zone") + "|" +
            body.optBoolean("charging") + "|" + body.optBoolean("driving") + "|" +
            body.optBoolean("screenOn")
        val now = System.currentTimeMillis()
        val since = now - Prefs.presenceSentAt(app)
        if (sig == Prefs.presenceSig(app) && since < HEARTBEAT_MS) { done?.invoke(); return }
        thread {
            try {
                LinkClient.postPresence(app, body)
                Prefs.setPresenceSent(app, sig, now)
                Prefs.setPresenceError(app, "")
            } catch (e: LinkClient.AuthException) {
                Prefs.setPresenceError(app, "link revoked - re-pair this phone")
            } catch (e: Exception) {
                // Unreachable workstation is the ordinary case here, not an
                // error worth surfacing loudly: the next event or tick retries.
                Prefs.setPresenceError(app, e.message ?: "could not reach the workstation")
            } finally {
                done?.invoke()
            }
        }
    }

    fun snapshot(ctx: Context): JSONObject {
        val j = JSONObject()
        j.put("place", place(ctx))
        // zone is an EXTRA beyond the agreed presence contract (which knows
        // only home/out/driving/unknown). The workstation is free to ignore it;
        // it exists so "at work" is distinguishable from "somewhere else"
        // without widening the agreed vocabulary.
        j.put("zone", zone(ctx))
        if (Prefs.presenceHaveFix(ctx)) {
            j.put("lat", coarse(Prefs.presenceLat(ctx)))
            j.put("lon", coarse(Prefs.presenceLon(ctx)))
        }
        j.put("charging", charging(ctx))
        j.put("driving", Prefs.presenceDriving(ctx))
        j.put("screenOn", screenOn(ctx))
        return j
    }

    /** Three decimals is about 110 metres. Anything finer is a movement log
     *  rather than an answer to "is he home". */
    private fun coarse(v: Double): Double = round(v * 1000.0) / 1000.0

    /** The agreed vocabulary: home | out | driving | unknown. */
    fun place(ctx: Context): String {
        if (Prefs.presenceDriving(ctx)) return "driving"
        return when (zone(ctx)) {
            "home" -> "home"
            "unknown" -> "unknown"
            else -> "out"
        }
    }

    /** home | work | elsewhere | unknown - this phone's own finer label. */
    fun zone(ctx: Context): String {
        if (!Prefs.presenceHaveFix(ctx)) return "unknown"
        val lat = Prefs.presenceLat(ctx)
        val lon = Prefs.presenceLon(ctx)
        if (Prefs.hasHome(ctx) &&
            metres(lat, lon, Prefs.homeLat(ctx), Prefs.homeLon(ctx)) < ZONE_RADIUS_M) return "home"
        if (Prefs.hasWork(ctx) &&
            metres(lat, lon, Prefs.workLat(ctx), Prefs.workLon(ctx)) < ZONE_RADIUS_M) return "work"
        // No saved points at all means we can say where the phone is but not
        // what that place MEANS - which is "unknown", not "elsewhere".
        if (!Prefs.hasHome(ctx) && !Prefs.hasWork(ctx)) return "unknown"
        return "elsewhere"
    }

    private fun metres(aLat: Double, aLon: Double, bLat: Double, bLon: Double): Float {
        return try {
            val out = FloatArray(1)
            Location.distanceBetween(aLat, aLon, bLat, bLon, out)
            out[0]
        } catch (e: Exception) { Float.MAX_VALUE }
    }

    private fun charging(ctx: Context): Boolean = try {
        ctx.getSystemService(BatteryManager::class.java)?.isCharging ?: false
    } catch (e: Exception) { false }

    private fun screenOn(ctx: Context): Boolean = try {
        ctx.getSystemService(PowerManager::class.java)?.isInteractive ?: false
    } catch (e: Exception) { false }

    // -- permissions ---------------------------------------------------------
    fun hasLocation(ctx: Context): Boolean =
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_COARSE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    /** "Allow all the time". Below API 29 there is no such thing - foreground
     *  location IS background location - so the answer there is yes. */
    fun hasBackgroundLocation(ctx: Context): Boolean =
        Build.VERSION.SDK_INT < 29 || ContextCompat.checkSelfPermission(
            ctx, Manifest.permission.ACCESS_BACKGROUND_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    // -- saved points --------------------------------------------------------
    /** Returns null on success, a sentence explaining why not otherwise. */
    fun saveHere(ctx: Context, which: String): String? {
        if (!Prefs.presenceHaveFix(ctx))
            return "No location fix yet. Leave presence on for a few minutes, then try again."
        val lat = Prefs.presenceLat(ctx)
        val lon = Prefs.presenceLon(ctx)
        if (which == "work") Prefs.setWork(ctx, lat, lon) else Prefs.setHome(ctx, lat, lon)
        report(ctx)
        return null
    }

    // -- for the card --------------------------------------------------------
    /** One line for the PRESENCE card: what this phone last decided and when it
     *  last managed to say so. */
    fun describe(ctx: Context): String {
        if (!Prefs.presenceOn(ctx)) return "not reporting"
        val bits = ArrayList<String>()
        bits.add(zone(ctx))
        if (Prefs.presenceDriving(ctx)) bits.add("driving")
        if (charging(ctx)) bits.add("charging")
        val sent = Prefs.presenceSentAt(ctx)
        if (sent > 0) {
            val mins = (System.currentTimeMillis() - sent) / 60000
            bits.add(if (mins < 1) "sent just now" else "sent " + mins + "m ago")
        } else {
            bits.add("nothing sent yet")
        }
        val err = Prefs.presenceError(ctx)
        if (err.isNotEmpty()) bits.add(err)
        return bits.joinToString(" - ")
    }

    /** Fingerprint of everything the card draws, including the local state the
     *  board snapshot knows nothing about - without it the card never repaints
     *  when the phone itself changes its mind. */
    fun cardSignature(ctx: Context): String =
        Prefs.presenceOn(ctx).toString() + "|" + place(ctx) + "|" + zone(ctx) + "|" +
            Prefs.presenceDriving(ctx) + "|" + (Prefs.presenceSentAt(ctx) / 60000) +
            "|" + Prefs.presenceError(ctx)
}
