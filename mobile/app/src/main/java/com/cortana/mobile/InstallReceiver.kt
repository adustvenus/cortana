package com.cortana.mobile

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.widget.Toast

/**
 * Result sink for PackageInstaller sessions started by UpdateManager.
 *
 * STATUS_PENDING_USER_ACTION means the platform wants a confirmation dialog
 * (older Android, or the silent path was refused) - we launch whatever intent
 * it hands back. Everything else is terminal: report it, since a silent failure
 * is exactly the experience this whole path exists to eliminate.
 */
class InstallReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        when (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, -1)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                @Suppress("DEPRECATION")
                val confirm = intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
                if (confirm != null) {
                    confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    context.startActivity(confirm)
                } else {
                    toast(context, "Update needs confirmation but Android sent no prompt")
                }
            }
            PackageInstaller.STATUS_SUCCESS ->
                toast(context, "Cortana updated - reopening picks up the new version")
            else -> {
                val msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)
                toast(context, "Update failed: ${msg ?: "unknown error"}")
            }
        }
    }

    private fun toast(ctx: Context, msg: String) =
        Toast.makeText(ctx, msg, Toast.LENGTH_LONG).show()
}
