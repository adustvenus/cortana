package com.cortana.mobile

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.graphics.Typeface
import android.os.Bundle
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView

class SettingsActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = Ui.dp(this, 22)
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Ui.BG)
            setPadding(pad, pad, pad, pad)
        }

        col.addView(TextView(this).apply {
            text = "SETTINGS"
            typeface = Typeface.MONOSPACE
            textSize = 16f
            letterSpacing = 0.2f
            setTextColor(Ui.TEXT)
        })
        col.addView(Ui.gap(this, 16))

        col.addView(Ui.label(this, "LINK"))
        col.addView(Ui.gap(this, 6))
        col.addView(Ui.value(this,
            "Workstation: ${Prefs.dashName(this).ifEmpty { "—" }}\n" +
            "Host: ${Prefs.host(this)}:${Prefs.port(this)}\n" +
            "This phone: ${Prefs.deviceName(this).ifEmpty { Prefs.defaultDeviceName() }}",
            14f, Ui.DIM))
        col.addView(Ui.gap(this, 10))
        col.addView(Ui.pillButton(this, "RE-PAIR / CHANGE HOST", Ui.ACCENT) {
            startActivity(Intent(this, PairActivity::class.java))
        })
        col.addView(Ui.gap(this, 10))
        col.addView(Ui.pillButton(this, "UNLINK THIS PHONE", Ui.ROSE) {
            AlertDialog.Builder(this)
                .setTitle("Unlink?")
                .setMessage("Removes the stored token from this phone. Also revoke it on " +
                    "the dashboard's MOBILE LINK module to invalidate it server-side.")
                .setPositiveButton("Unlink") { _, _ ->
                    Prefs.unlink(this)
                    startActivity(Intent(this, PairActivity::class.java))
                    finish()
                }
                .setNegativeButton("Cancel", null)
                .show()
        })
        col.addView(Ui.gap(this, 24))

        col.addView(Ui.label(this, "VOICE"))
        col.addView(Ui.gap(this, 6))
        val ttsSwitch = Switch(this).apply {
            text = "Use phone TTS instead of Cortana's voice (saves data)"
            isChecked = Prefs.localTtsOnly(context)
            setTextColor(Ui.TEXT)
            textSize = 14f
            setOnCheckedChangeListener { _, v -> Prefs.setLocalTtsOnly(context, v) }
        }
        col.addView(ttsSwitch)
        col.addView(Ui.gap(this, 24))

        col.addView(Ui.label(this, "UPDATES"))
        col.addView(Ui.gap(this, 6))
        col.addView(Ui.value(this, "Installed: v${BuildConfig.VERSION_NAME}", 14f, Ui.DIM))
        col.addView(Ui.gap(this, 10))
        col.addView(Ui.pillButton(this, "CHECK FOR UPDATE", Ui.LAVENDER) {
            val st = LinkClient.lastState
            if (st == null)
                android.widget.Toast.makeText(this,
                    "Not connected to the bridge right now", android.widget.Toast.LENGTH_LONG).show()
            else UpdateManager.maybeOffer(this, st, manual = true)
        })
        col.addView(Ui.gap(this, 24))

        col.addView(Ui.label(this, "ABOUT"))
        col.addView(Ui.gap(this, 6))
        col.addView(Ui.value(this,
            "Cortana Mobile - the view-only phone mirror of the Dusk dashboard, " +
            "plus voice. Connects only over your tailnet; the pairing token " +
            "lives in encrypted storage on this phone.", 13f, Ui.DIM))

        setContentView(ScrollView(this).apply { addView(col); setBackgroundColor(Ui.BG) })
    }
}
