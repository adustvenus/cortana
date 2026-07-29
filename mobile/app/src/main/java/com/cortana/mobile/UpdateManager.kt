package com.cortana.mobile

import android.app.Activity
import android.app.AlertDialog
import android.app.ProgressDialog
import android.content.Intent
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File
import kotlin.concurrent.thread

/**
 * In-app update flow. The bridge's state feed carries the APK version CI last
 * built (committed to mobile/dist and pulled onto the workstation), so the
 * phone learns about a new build the moment it connects - no store, no
 * GitHub access needed from the phone.
 *
 * Flow: newer version seen -> pop-up -> download over the (Tailscale) link ->
 * hand the file to Android's package installer. The install prompt is the
 * "restart to update": the app is killed and relaunched by the installer.
 * Signature stays constant across CI builds (committed keystore), so updates
 * always install over the existing app.
 */
object UpdateManager {
    private var offeredThisSession = ""

    /** Compare dotted versions: 1.2.10 > 1.2.9. Unparseable -> not newer. */
    fun isNewer(remote: String, local: String): Boolean {
        if (remote.isEmpty() || local.isEmpty()) return false
        val r = remote.split(".").map { it.toIntOrNull() ?: return false }
        val l = local.split(".").map { it.toIntOrNull() ?: return false }
        for (i in 0 until maxOf(r.size, l.size)) {
            val a = r.getOrElse(i) { 0 }
            val b = l.getOrElse(i) { 0 }
            if (a != b) return a > b
        }
        return false
    }

    /** Called with each state push; shows the prompt at most once per session
     *  per version, and honors a "skip this version" choice. */
    fun maybeOffer(activity: Activity, state: JSONObject, manual: Boolean = false) {
        val apk = state.optJSONObject("apk") ?: return
        val remote = apk.optString("version")
        if (!apk.optBoolean("available")) {
            if (manual) toast(activity, "No APK published on the workstation yet")
            return
        }
        if (!isNewer(remote, BuildConfig.VERSION_NAME)) {
            if (manual) toast(activity, "Up to date (v${BuildConfig.VERSION_NAME})")
            return
        }
        if (!manual && (offeredThisSession == remote ||
                        Prefs.skippedVersion(activity) == remote)) return
        offeredThisSession = remote
        AlertDialog.Builder(activity)
            .setTitle("Update available")
            .setMessage("Cortana Mobile v$remote is ready (you have " +
                "v${BuildConfig.VERSION_NAME}). Install now? The app restarts " +
                "as part of the update.")
            .setPositiveButton("Update") { _, _ -> download(activity) }
            .setNegativeButton("Later", null)
            .setNeutralButton("Skip this version") { _, _ ->
                Prefs.setSkippedVersion(activity, remote)
            }
            .show()
    }

    private fun download(activity: Activity) {
        @Suppress("DEPRECATION")
        val dlg = ProgressDialog(activity).apply {
            setTitle("Downloading update")
            setProgressStyle(ProgressDialog.STYLE_HORIZONTAL)
            max = 100
            setCancelable(false)
            show()
        }
        thread {
            try {
                val dir = File(activity.cacheDir, "apk").apply { mkdirs() }
                val dest = File(dir, "cortana-mobile-update.apk")
                LinkClient.downloadApk(activity, dest) { pct ->
                    activity.runOnUiThread { dlg.progress = pct }
                }
                activity.runOnUiThread {
                    dlg.dismiss()
                    install(activity, dest)
                }
            } catch (e: Exception) {
                activity.runOnUiThread {
                    dlg.dismiss()
                    toast(activity, "Update failed: ${e.message}")
                }
            }
        }
    }

    private fun install(activity: Activity, apk: File) {
        val uri = FileProvider.getUriForFile(
            activity, "com.cortana.mobile.fileprovider", apk)
        activity.startActivity(Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        })
    }

    private fun toast(activity: Activity, msg: String) =
        android.widget.Toast.makeText(activity, msg, android.widget.Toast.LENGTH_LONG).show()
}
