package com.cortana.mobile

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.graphics.Typeface
import android.os.Bundle
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast

class SettingsActivity : Activity() {

    private var adbPortField: EditText? = null

    private fun toast(msg: String) =
        Toast.makeText(this, msg, Toast.LENGTH_LONG).show()

    // Swiping the page rightward (finger left-to-right) travels back left to
    // the dashboard - the mirror of the gesture that opened this screen.
    private val swipeDetector by lazy {
        android.view.GestureDetector(this,
            object : android.view.GestureDetector.SimpleOnGestureListener() {
                override fun onFling(e1: android.view.MotionEvent?, e2: android.view.MotionEvent,
                                     vx: Float, vy: Float): Boolean {
                    e1 ?: return false
                    val dx = e2.x - e1.x; val dy = e2.y - e1.y
                    if (dx > 220 && Math.abs(dx) > 2 * Math.abs(dy) && vx > 900) {
                        finish()
                        return true
                    }
                    return false
                }
            })
    }

    override fun dispatchTouchEvent(ev: android.view.MotionEvent): Boolean {
        swipeDetector.onTouchEvent(ev)
        return super.dispatchTouchEvent(ev)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = Ui.dp(this, 22)
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Ui.BG)
            setPadding(pad, pad, pad, pad)
        }

        val back = TextView(this).apply {
            text = "←"
            textSize = 24f
            setTextColor(Ui.DIM)
            setPadding(0, 0, Ui.dp(this@SettingsActivity, 16), Ui.dp(this@SettingsActivity, 2))
            contentDescription = "Back to the dashboard"
            setOnClickListener { finish() }
        }
        col.addView(Ui.row(this, back, TextView(this).apply {
            text = "SETTINGS"
            typeface = Typeface.MONOSPACE
            textSize = 16f
            letterSpacing = 0.2f
            setTextColor(Ui.TEXT)
        }))
        col.addView(Ui.gap(this, 16))

        col.addView(Ui.row(this, Ui.label(this, "LINK"), Ui.helpIcon(this, "security")))
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

        col.addView(Ui.row(this, Ui.label(this, "VOICE"), Ui.helpIcon(this, "voice")))
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

        col.addView(Ui.row(this, Ui.label(this, "UPDATES"), Ui.helpIcon(this, "update")))
        col.addView(Ui.gap(this, 6))
        col.addView(Ui.value(this, "Installed: v${BuildConfig.VERSION_NAME}", 14f, Ui.DIM))
        col.addView(Ui.gap(this, 10))
        col.addView(Ui.pillButton(this, "CHECK FOR UPDATE", Ui.LAVENDER) {
            // Pulls CI's latest build onto the workstation first, then offers it.
            UpdateManager.checkNow(this, LinkClient.lastState)
        })
        col.addView(Ui.gap(this, 10))
        col.addView(Ui.pillButton(this, "INSTALL VIA WORKSTATION (ADB)", Ui.DIM) {
            AlertDialog.Builder(this)
                .setTitle("Install over wireless adb")
                .setMessage("For phones whose installer blocks updates silently " +
                    "(OnePlus/OPPO). Needs: Developer options → Wireless debugging ON, " +
                    "same Wi-Fi as the workstation, and a one-time adb pair. " +
                    "Enter the PORT shown on the Wireless debugging screen.")
                .setView(EditText(this).also { portField ->
                    portField.hint = "port, e.g. 37219"
                    portField.inputType = android.text.InputType.TYPE_CLASS_NUMBER
                    portField.setTextColor(Ui.TEXT)
                    portField.setHintTextColor(Ui.DIM)
                    adbPortField = portField
                })
                .setPositiveButton("Install") { _, _ ->
                    val port = adbPortField?.text?.toString()?.trim()?.toIntOrNull()
                    if (port == null) {
                        toast("Enter the port from the Wireless debugging screen")
                    } else UpdateManager.adbInstall(this, port)
                }
                .setNegativeButton("Cancel", null)
                .show()
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
