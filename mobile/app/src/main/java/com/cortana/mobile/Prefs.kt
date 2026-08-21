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

    /** Phone-local module order from drag-and-drop; empty = follow the board. */
    fun moduleOrder(ctx: Context): List<String> =
        (plain(ctx).getString("moduleOrder", "") ?: "")
            .split(",").filter { it.isNotEmpty() }

    fun setModuleOrder(ctx: Context, order: List<String>) {
        plain(ctx).edit().putString("moduleOrder", order.joinToString(",")).apply()
    }

    /** Drop the credential only. Host, name and layout are kept so re-pairing
     *  is a scanned code rather than a full re-setup. */
    fun unlink(ctx: Context) {
        secure(ctx)?.edit()?.remove("token")?.apply()
        plain(ctx).edit().remove("dashName").apply()
    }
}
