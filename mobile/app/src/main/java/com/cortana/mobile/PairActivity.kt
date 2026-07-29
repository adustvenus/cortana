package com.cortana.mobile

import android.app.Activity
import android.content.Intent
import android.graphics.Typeface
import android.os.Bundle
import android.text.InputType
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import kotlin.concurrent.thread

/**
 * First-run pairing. The user types the workstation's Tailscale name/IP and
 * the 6-digit code shown on the dashboard's MOBILE LINK module. A successful
 * exchange stores the device token; everything after that is automatic.
 */
class PairActivity : Activity() {

    private lateinit var hostIn: EditText
    private lateinit var portIn: EditText
    private lateinit var codeIn: EditText
    private lateinit var nameIn: EditText
    private lateinit var status: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = Ui.dp(this, 22)
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Ui.BG)
            setPadding(pad, pad, pad, pad)
        }

        col.addView(TextView(this).apply {
            text = "LINK THIS PHONE"
            typeface = Typeface.MONOSPACE
            textSize = 17f
            letterSpacing = 0.2f
            setTextColor(Ui.TEXT)
        })
        col.addView(Ui.gap(this, 6))
        col.addView(Ui.value(this,
            "1. Install Tailscale on the workstation and this phone (same tailnet).\n" +
            "2. On the Dusk dashboard, add the MOBILE LINK module and tap PAIR A PHONE.\n" +
            "3. Enter the workstation's Tailscale name/IP and the code shown.",
            13f, Ui.DIM))
        col.addView(Ui.gap(this, 18))

        hostIn = field("Workstation host (Tailscale name or IP)", Prefs.host(this))
        portIn = field("Port", Prefs.port(this).toString()).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        codeIn = field("6-digit pairing code", "").apply {
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        nameIn = field("This phone's name (shown on the dashboard)",
            Prefs.deviceName(this).ifEmpty { Prefs.defaultDeviceName() })
        for (f in listOf(hostIn, portIn, codeIn, nameIn)) {
            col.addView(f); col.addView(Ui.gap(this, 12))
        }

        status = Ui.value(this, "", 13f, Ui.ROSE)
        col.addView(status)
        col.addView(Ui.gap(this, 12))
        col.addView(Ui.pillButton(this, "PAIR", Ui.ACCENT) { pair() })

        setContentView(ScrollView(this).apply { addView(col); setBackgroundColor(Ui.BG) })

        // QR onboarding: the bridge's /get page deep-links cortana://pair with
        // the host + code baked in - fill the form and pair without typing.
        intent?.data?.let { uri ->
            if (uri.scheme == "cortana" && uri.host == "pair") {
                uri.getQueryParameter("host")?.let { if (it.isNotEmpty()) hostIn.setText(it) }
                uri.getQueryParameter("port")?.let { if (it.isNotEmpty()) portIn.setText(it) }
                uri.getQueryParameter("code")?.let { if (it.isNotEmpty()) codeIn.setText(it) }
                if (!uri.getQueryParameter("host").isNullOrEmpty()
                    && uri.getQueryParameter("code")?.length == 6) {
                    status.setTextColor(Ui.DIM)
                    status.text = "Pairing from QR link…"
                    pair()
                }
            }
        }
    }

    private fun field(hint: String, value: String): EditText = EditText(this).apply {
        this.hint = hint
        setText(value)
        setTextColor(Ui.TEXT)
        setHintTextColor(Ui.DIM)
        textSize = 15f
    }

    private fun pair() {
        val host = hostIn.text.toString().trim()
        val port = portIn.text.toString().trim().toIntOrNull() ?: 8765
        val code = codeIn.text.toString().trim()
        val name = nameIn.text.toString().trim().ifEmpty { Prefs.defaultDeviceName() }
        if (host.isEmpty() || code.length != 6) {
            status.text = "Enter the host and the 6-digit code."
            return
        }
        status.setTextColor(Ui.DIM)
        status.text = "Pairing…"
        thread {
            try {
                val r = LinkClient.pair(host, port, code, name)
                Prefs.savePairing(this, host, port,
                    r.optString("token"), r.optString("host"), name)
                runOnUiThread {
                    Toast.makeText(this, "Linked to ${r.optString("host")}", Toast.LENGTH_LONG).show()
                    startActivity(Intent(this, MainActivity::class.java))
                    finish()
                }
            } catch (e: Exception) {
                runOnUiThread {
                    status.setTextColor(Ui.ROSE)
                    status.text = "Pairing failed: ${e.message}\n" +
                        "Check: same tailnet? bridge running? code fresh (10 min)?"
                }
            }
        }
    }
}
