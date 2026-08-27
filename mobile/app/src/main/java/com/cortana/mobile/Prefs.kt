package com.cortana.mobile

import android.content.Context
import android.content.SharedPreferences
import android.os.Build
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Persisted settings, split by sensitivity:
 *
 *  - the pairing TOKEN lives in EncryptedSharedPreferences (it is this phone's
 *    credential to the workstation);
 *  - everything else - host, port, device names, module order, preferences -
 *    lives in ordinary SharedPreferences.
 *
 * The split exists because the encrypted store is the fragile one: it depends
 * on an Android Keystore master key that can transiently fail to initialise,
 * notably on the first launch after an app update. Keeping only the token
 * there means such a failure costs at most a re-pair - the host, the device
 * name and the layout all survive, so re-linking is a scanned QR rather than
 * a re-setup.
 *
 * We NEVER delete the encrypted store to recover from an error. An earlier
 * version did, which turned any transient failure into a permanently lost
 * pairing. It is retried instead, and left intact for the next launch.
 */
object Prefs {
    private const val SECURE_FILE = "cortana_link"     // token only
    private const val PLAIN_FILE = "cortana_prefs"     // everything else

    private var secureStore: SharedPreferences? = null
    private var plainStore: SharedPreferences? = null

    // ── stores ──────────────────────────────────────────────────────────────
    private fun plain(ctx: Context): SharedPreferences {
        plainStore?.let { return it }
        val sp = ctx.applicationContext
            .getSharedPreferences(PLAIN_FILE, Context.MODE_PRIVATE)
        plainStore = sp
        migrateIfNeeded(ctx, sp)
        return sp
    }

    /** Encrypted store, or null when the keystore is unavailable right now.
     *  Callers treat null as "no token yet", never as "wipe and start over". */
    private fun secure(ctx: Context): SharedPreferences? {
        secureStore?.let { return it }
        val appCtx = ctx.applicationContext
        repeat(2) { attempt ->                 // transient failures do happen
            try {
                val key = MasterKey.Builder(appCtx)
                    .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                    .build()
                val sp = EncryptedSharedPreferences.create(
                    appCtx, SECURE_FILE, key,
                    EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                    EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM)
                secureStore = sp
                return sp
            } catch (e: Exception) {
                if (attempt == 0) Thread.sleep(150)
            }
        }
        return null
    }

    /** One-time move of non-secret values out of the encrypted store, so an
     *  upgrade from the old layout keeps host/port/name/settings. */
    private fun migrateIfNeeded(ctx: Context, sp: SharedPreferences) {
        if (sp.getBoolean("migrated", false)) return
        val old = secure(ctx)
        val edit = sp.edit().putBoolean("migrated", true)
        if (old != null) {
            old.getString("host", null)?.let { edit.putString("host", it) }
            old.getString("dashName", null)?.let { edit.putString("dashName", it) }
            old.getString("deviceName", null)?.let { edit.putString("deviceName", it) }
            old.getString("altHosts", null)?.let { edit.putString("altHosts", it) }
            old.getString("moduleOrder", null)?.let { edit.putString("moduleOrder", it) }
            old.getString("skipVer", null)?.let { edit.putString("skipVer", it) }
            if (old.contains("port")) edit.putInt("port", old.getInt("port", 8765))
            if (old.contains("localTts")) edit.putBoolean("localTts", old.getBoolean("localTts", false))
        }
        edit.apply()
    }

    // ── the credential ──────────────────────────────────────────────────────
    fun token(ctx: Context): String = secure(ctx)?.getString("token", "") ?: ""

    fun secureStorageAvailable(ctx: Context) = secure(ctx) != null

    // ── ordinary settings ───────────────────────────────────────────────────
    fun host(ctx: Context): String = plain(ctx).getString("host", "") ?: ""
    fun port(ctx: Context): Int = plain(ctx).getInt("port", 8765)
    fun dashName(ctx: Context): String = plain(ctx).getString("dashName", "") ?: ""
    fun deviceName(ctx: Context): String = plain(ctx).getString("deviceName", "") ?: ""
    fun localTtsOnly(ctx: Context): Boolean = plain(ctx).getBoolean("localTts", false)
    fun skippedVersion(ctx: Context): String = plain(ctx).getString("skipVer", "") ?: ""
    // Highest announcement id seen. Sent on connect so the bridge replays
    // only what was missed while the app was closed.
    fun lastAnnounce(ctx: Context): Int = plain(ctx).getInt("lastAnnounce", 0)

    fun defaultDeviceName(): String =
        ("${Build.MANUFACTURER} ${Build.MODEL}").trim().ifEmpty { "Android phone" }

    fun paired(ctx: Context): Boolean = token(ctx).isNotEmpty() && host(ctx).isNotEmpty()

    fun savePairing(ctx: Context, host: String, port: Int, token: String,
                    dashName: String, deviceName: String) {
        secure(ctx)?.edit()?.putString("token", token)?.apply()
        plain(ctx).edit()
            .putString("host", host).putInt("port", port)
            .putString("dashName", dashName).putString("deviceName", deviceName)
            .apply()
    }

    fun setDashName(ctx: Context, name: String) {
        plain(ctx).edit().putString("dashName", name).apply()
    }

    fun setLocalTtsOnly(ctx: Context, v: Boolean) {
        plain(ctx).edit().putBoolean("localTts", v).apply()
    }

    fun setLastAnnounce(ctx: Context, id: Int) {
        if (id > lastAnnounce(ctx)) plain(ctx).edit().putInt("lastAnnounce", id).apply()
    }

    fun setSkippedVersion(ctx: Context, v: String) {
        plain(ctx).edit().putString("skipVer", v).apply()
    }

    /** Every address the workstation advertised (Tailscale + LAN). The client
     *  fails over between these, so a phone paired at home still reaches the
     *  bridge from cellular without re-pairing. */
    fun altHosts(ctx: Context): List<String> =
        (plain(ctx).getString("altHosts", "") ?: "")
            .split(",").map { it.trim() }.filter { it.isNotEmpty() }

    fun setAltHosts(ctx: Context, hosts: List<String>) {
        plain(ctx).edit().putString("altHosts", hosts.joinToString(",")).apply()
    }

    /** Promote the address that actually worked to the primary host. */
    fun setHost(ctx: Context, host: String) {
        plain(ctx).edit().putString("host", host).apply()
    }

    /** ZIP entered on the phone. Overrides the dashboard's board ZIP so the
     *  weather card works even before a board snapshot arrives. */
    fun weatherZip(ctx: Context): String = plain(ctx).getString("weatherZip", "") ?: ""

    fun setWeatherZip(ctx: Context, zip: String) {
        plain(ctx).edit().putString("weatherZip", zip).apply()
    }

    /** The dashboard's colour tokens, as the raw JSON object the bridge sent.
     *  Cached so the app opens in the right palette instead of showing the
     *  built-in defaults until the first snapshot lands. Not secret. */
    fun themeTokens(ctx: Context): String = plain(ctx).getString("themeTokens", "") ?: ""

    fun setThemeTokens(ctx: Context, json: String) {
        plain(ctx).edit().putString("themeTokens", json).apply()
    }

    /** Phone-local module order from drag-and-drop; empty = follow the board. */
    fun moduleOrder(ctx: Context): List<String> =
        (plain(ctx).getString("moduleOrder", "") ?: "")
            .split(",").filter { it.isNotEmpty() }

    fun setModuleOrder(ctx: Context, order: List<String>) {
        plain(ctx).edit().putString("moduleOrder", order.joinToString(",")).apply()
    }

    // ── background link, presence, comms ────────────────────────────────────
    // Everything below is a CAPABILITY switch and every one of them defaults to
    // false. That is not caution for its own sake: this app can read the phone's
    // notifications, its location and its text messages, and an install that
    // starts doing any of that without being asked is indistinguishable from
    // spyware. The Settings screen states what each one sends before you can
    // turn it on.

    /** Hold the WebSocket open with a foreground service so announcements
     *  arrive while the app is closed. */
    fun background(ctx: Context): Boolean = plain(ctx).getBoolean("bgLink", false)

    fun setBackground(ctx: Context, v: Boolean) {
        plain(ctx).edit().putBoolean("bgLink", v).apply()
    }

    fun presenceOn(ctx: Context): Boolean = plain(ctx).getBoolean("presenceOn", false)

    fun setPresenceOn(ctx: Context, v: Boolean) {
        plain(ctx).edit().putBoolean("presenceOn", v).apply()
    }

    fun commsNotifications(ctx: Context): Boolean = plain(ctx).getBoolean("commsNotif", false)

    fun setCommsNotifications(ctx: Context, v: Boolean) {
        plain(ctx).edit().putBoolean("commsNotif", v).apply()
    }

    fun smsRead(ctx: Context): Boolean = plain(ctx).getBoolean("smsRead", false)

    fun setSmsRead(ctx: Context, v: Boolean) {
        plain(ctx).edit().putBoolean("smsRead", v).apply()
    }

    fun smsSend(ctx: Context): Boolean = plain(ctx).getBoolean("smsSend", false)

    fun setSmsSend(ctx: Context, v: Boolean) {
        plain(ctx).edit().putBoolean("smsSend", v).apply()
    }

    // ── presence working state ──────────────────────────────────────────────
    // Stored rather than held in memory because PresenceReceiver usually runs
    // in a freshly started process with no service and no activity behind it -
    // in-memory state would simply be gone every time it mattered.
    //
    // Float, not Double: SharedPreferences has no double, and float32 resolves
    // a latitude to about a metre, which is far finer than the 300m zone radius
    // these feed.
    fun presenceHaveFix(ctx: Context): Boolean = plain(ctx).contains("pLat")
    fun presenceLat(ctx: Context): Double = plain(ctx).getFloat("pLat", 0f).toDouble()
    fun presenceLon(ctx: Context): Double = plain(ctx).getFloat("pLon", 0f).toDouble()

    fun setPresenceFix(ctx: Context, lat: Double, lon: Double) {
        plain(ctx).edit().putFloat("pLat", lat.toFloat())
            .putFloat("pLon", lon.toFloat()).apply()
    }

    fun presenceDriving(ctx: Context): Boolean = plain(ctx).getBoolean("pDriving", false)

    fun setPresenceDriving(ctx: Context, v: Boolean) {
        plain(ctx).edit().putBoolean("pDriving", v).apply()
    }

    fun presenceSentAt(ctx: Context): Long = plain(ctx).getLong("pSentAt", 0L)
    fun presenceSig(ctx: Context): String = plain(ctx).getString("pSig", "") ?: ""

    fun setPresenceSent(ctx: Context, sig: String, at: Long) {
        plain(ctx).edit().putString("pSig", sig).putLong("pSentAt", at).apply()
    }

    /** Why presence is not reporting, in a sentence, or empty. Shown on the
     *  card: a switch that says ON over a permission that was revoked in system
     *  Settings is the kind of quiet lie this repo keeps paying for. */
    fun presenceError(ctx: Context): String = plain(ctx).getString("pErr", "") ?: ""

    fun setPresenceError(ctx: Context, msg: String) {
        if (presenceError(ctx) == msg) return
        plain(ctx).edit().putString("pErr", msg).apply()
    }

    fun hasHome(ctx: Context): Boolean = plain(ctx).contains("homeLat")
    fun homeLat(ctx: Context): Double = plain(ctx).getFloat("homeLat", 0f).toDouble()
    fun homeLon(ctx: Context): Double = plain(ctx).getFloat("homeLon", 0f).toDouble()

    fun setHome(ctx: Context, lat: Double, lon: Double) {
        plain(ctx).edit().putFloat("homeLat", lat.toFloat())
            .putFloat("homeLon", lon.toFloat()).apply()
    }

    fun hasWork(ctx: Context): Boolean = plain(ctx).contains("workLat")
    fun workLat(ctx: Context): Double = plain(ctx).getFloat("workLat", 0f).toDouble()
    fun workLon(ctx: Context): Double = plain(ctx).getFloat("workLon", 0f).toDouble()

    fun setWork(ctx: Context, lat: Double, lon: Double) {
        plain(ctx).edit().putFloat("workLat", lat.toFloat())
            .putFloat("workLon", lon.toFloat()).apply()
    }

    /** Forget both saved points and the last fix. Offered next to the presence
     *  switch, because turning a thing off and leaving its data behind is not
     *  what anyone means by off. */
    fun clearPresenceData(ctx: Context) {
        plain(ctx).edit()
            .remove("homeLat").remove("homeLon")
            .remove("workLat").remove("workLon")
            .remove("pLat").remove("pLon")
            .remove("pSig").remove("pSentAt").remove("pErr")
            .remove("pDriving")
            .apply()
    }

    /** Drop the credential only. Host, name and layout are kept so re-pairing
     *  is a scanned code rather than a full re-setup. */
    fun unlink(ctx: Context) {
        secure(ctx)?.edit()?.remove("token")?.apply()
        plain(ctx).edit().remove("dashName").apply()
    }
}
