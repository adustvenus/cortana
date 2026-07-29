package com.cortana.mobile

import android.content.Context
import android.content.SharedPreferences
import android.os.Build
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Settings + the device token, in EncryptedSharedPreferences (the token is the
 * phone's credential to the workstation - it never leaves this store).
 *
 * Edge case: a corrupted Android keystore entry makes EncryptedSharedPreferences
 * throw on open. We delete the store and start clean - the user re-pairs, which
 * beats a permanently crashing app.
 */
object Prefs {
    private const val FILE = "cortana_link"
    private var sp: SharedPreferences? = null

    private fun open(ctx: Context): SharedPreferences {
        sp?.let { return it }
        val appCtx = ctx.applicationContext
        val created = try {
            encrypted(appCtx)
        } catch (e: Exception) {
            appCtx.deleteSharedPreferences(FILE)
            try { encrypted(appCtx) } catch (e2: Exception) {
                appCtx.getSharedPreferences(FILE, Context.MODE_PRIVATE)
            }
        }
        sp = created
        return created
    }

    private fun encrypted(ctx: Context): SharedPreferences {
        val key = MasterKey.Builder(ctx)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        return EncryptedSharedPreferences.create(
            ctx, FILE, key,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
        )
    }

    fun host(ctx: Context): String = open(ctx).getString("host", "") ?: ""
    fun port(ctx: Context): Int = open(ctx).getInt("port", 8765)
    fun token(ctx: Context): String = open(ctx).getString("token", "") ?: ""
    fun dashName(ctx: Context): String = open(ctx).getString("dashName", "") ?: ""
    fun deviceName(ctx: Context): String =
        open(ctx).getString("deviceName", "") ?: ""
    fun localTtsOnly(ctx: Context): Boolean = open(ctx).getBoolean("localTts", false)
    fun skippedVersion(ctx: Context): String = open(ctx).getString("skipVer", "") ?: ""

    fun defaultDeviceName(): String =
        ("${Build.MANUFACTURER} ${Build.MODEL}").trim().ifEmpty { "Android phone" }

    fun paired(ctx: Context): Boolean = token(ctx).isNotEmpty() && host(ctx).isNotEmpty()

    fun savePairing(ctx: Context, host: String, port: Int, token: String,
                    dashName: String, deviceName: String) {
        open(ctx).edit()
            .putString("host", host).putInt("port", port)
            .putString("token", token).putString("dashName", dashName)
            .putString("deviceName", deviceName)
            .apply()
    }

    fun setDashName(ctx: Context, name: String) {
        open(ctx).edit().putString("dashName", name).apply()
    }

    fun setLocalTtsOnly(ctx: Context, v: Boolean) {
        open(ctx).edit().putBoolean("localTts", v).apply()
    }

    fun setSkippedVersion(ctx: Context, v: String) {
        open(ctx).edit().putString("skipVer", v).apply()
    }

    /** Phone-local module order from drag-and-drop; empty = follow the board. */
    fun moduleOrder(ctx: Context): List<String> =
        (open(ctx).getString("moduleOrder", "") ?: "")
            .split(",").filter { it.isNotEmpty() }

    fun setModuleOrder(ctx: Context, order: List<String>) {
        open(ctx).edit().putString("moduleOrder", order.joinToString(",")).apply()
    }

    fun unlink(ctx: Context) {
        open(ctx).edit().remove("token").remove("dashName").apply()
    }
}
