package com.cortana.mobile

import android.app.Activity
import android.app.AlertDialog
import android.app.PendingIntent
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.widget.ProgressBar
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

    /** Manual CHECK FOR UPDATE: first ask the workstation to pull CI's latest
     *  build (so the user never has to run git pull for a phone update), then
     *  offer whatever is now in dist. */
    fun checkNow(activity: Activity, state: JSONObject?) {
        toast(activity, "Checking - syncing the workstation…")
        thread {
            val apk = try {
                val r = LinkClient.apkRefresh(activity)
                if (!r.optBoolean("ok") && r.has("error")) {
                    activity.runOnUiThread { toast(activity, r.optString("error")) }
                }
                r.optJSONObject("apk")
            } catch (e: Exception) {
                activity.runOnUiThread { toast(activity, "Can't reach the workstation: ${e.message}") }
                null
            } ?: state?.optJSONObject("apk")
            activity.runOnUiThread {
                maybeOffer(activity, JSONObject().put("apk", apk ?: JSONObject()), manual = true)
            }
        }
    }

    /** Fallback for skins that block the installer UI: have the workstation
     *  push the APK to this phone over wireless adb (privileged install). */
    fun adbInstall(activity: Activity, port: Int) {
        toast(activity, "Asking the workstation to install…")
        thread {
            val r = try {
                LinkClient.apkAdbInstall(activity, port)
            } catch (e: Exception) {
                JSONObject().put("ok", false).put("error", e.message ?: "link error")
            }
            activity.runOnUiThread {
                if (r.optBoolean("ok"))
                    toast(activity, "Installed v${r.optString("version")} - reopen Cortana")
                else
                    toast(activity, "Install failed: ${r.optString("error", r.optString("output"))}")
            }
        }
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

    /** Android requires a per-app 'Install unknown apps' grant for THIS app
     *  before the system installer will act on our APK - and without it, many
     *  skins dismiss the install sheet silently. Check up front and walk the
     *  user to the exact settings screen instead of failing with no feedback. */
    private fun ensureInstallPermission(activity: Activity): Boolean {
        if (activity.packageManager.canRequestPackageInstalls()) return true
        AlertDialog.Builder(activity)
            .setTitle("One-time permission needed")
            .setMessage("Android needs you to allow Cortana to install its own " +
                "updates. On the next screen, turn on \"Allow from this source\", " +
                "come back, and tap CHECK FOR UPDATE again.")
            .setPositiveButton("Open settings") { _, _ ->
                activity.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:${activity.packageName}")))
            }
            .setNegativeButton("Later", null)
            .show()
        return false
    }

    private fun download(activity: Activity) {
        if (!ensureInstallPermission(activity)) return
        // ProgressDialog is deprecated; a plain dialog with a determinate bar
        // gives the same feedback without the deprecated widget.
        val bar = ProgressBar(activity, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            isIndeterminate = false
            val p = (16 * activity.resources.displayMetrics.density).toInt()
            setPadding(p, p, p, p)
        }
        val dlg = AlertDialog.Builder(activity)
            .setTitle("Downloading update")
            .setView(bar)
            .setCancelable(false)
            .create()
        dlg.show()
        thread {
            try {
                val dir = File(activity.cacheDir, "apk").apply { mkdirs() }
                val dest = File(dir, "cortana-mobile-update.apk")
                LinkClient.downloadApk(activity, dest) { pct ->
                    activity.runOnUiThread { bar.progress = pct }
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

    /**
     * Install via the PackageInstaller session API rather than an ACTION_VIEW
     * intent. Two reasons this is the right path:
     *  - On Android 12+, an app updating ITSELF while being its own
     *    installer-of-record may commit with USER_ACTION_NOT_REQUIRED: a real
     *    silent self-update, like a normal store-managed app.
     *  - The ACTION_VIEW hand-off goes through the system installer UI, which
     *    OxygenOS/ColorOS drop silently. This path avoids that UI entirely.
     * If the platform still wants confirmation it replies PENDING_USER_ACTION
     * and InstallReceiver surfaces the prompt, so we degrade gracefully.
     */
    private fun install(activity: Activity, apk: File) {
        try {
            val installer = activity.packageManager.packageInstaller
            val params = PackageInstaller.SessionParams(
                PackageInstaller.SessionParams.MODE_FULL_INSTALL)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                // Self-update, so ask for the no-prompt path. The platform
                // silently ignores this when it isn't permitted.
                params.setRequireUserAction(
                    PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
            }
            val sessionId = installer.createSession(params)
            installer.openSession(sessionId).use { session ->
                session.openWrite("cortana", 0, apk.length()).use { out ->
                    apk.inputStream().use { it.copyTo(out) }
                    session.fsync(out)
                }
                val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
                val pi = PendingIntent.getBroadcast(
                    activity, sessionId,
                    Intent(activity, InstallReceiver::class.java), flags)
                session.commit(pi.intentSender)
            }
            toast(activity, "Installing update…")
        } catch (e: Exception) {
            // Last resort: the classic intent hand-off.
            try {
                val uri = FileProvider.getUriForFile(
                    activity, "${activity.packageName}.fileprovider", apk)
                activity.startActivity(Intent(Intent.ACTION_VIEW).apply {
                    setDataAndType(uri, "application/vnd.android.package-archive")
                    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
                })
            } catch (e2: Exception) {
                toast(activity, "Install failed: ${e.message}")
            }
        }
    }

    private fun toast(activity: Activity, msg: String) =
        android.widget.Toast.makeText(activity, msg, android.widget.Toast.LENGTH_LONG).show()
}
