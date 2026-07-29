package com.cortana.mobile

import android.app.Activity
import android.content.Intent
import android.graphics.Typeface
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import kotlin.concurrent.thread

/**
 * First-run pairing. Primary path is scanning the QR on the dashboard's
 * MOBILE LINK module - that opens the bridge's /get page, which deep-links
 * back here (cortana://pair) with host + code baked in, and we pair with no
 * typing. Manual entry is a fallback tucked behind a toggle.
 */
class PairActivity : Activity() {

    private lateinit var hostIn: EditText
    private lateinit var portIn: EditText
    private lateinit var codeIn: EditText
    private lateinit var nameIn: EditText
    private lateinit var status: TextView
    private lateinit var manualBox: LinearLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = Ui.dp(this, 24)
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setBackgroundColor(Ui.BG)
            setPadding(pad, Ui.dp(context, 40), pad, pad)
        }

        // ── primary: scan the QR ──
        col.addView(ImageView(this).apply {
            setImageResource(R.drawable.sphere)
            layoutParams = LinearLayout.LayoutParams(Ui.dp(context, 96), Ui.dp(context, 96))
        })
        col.addView(Ui.gap(this, 20))
        col.addView(TextView(this).apply {
            text = "LINK THIS PHONE"
            typeface = Typeface.MONOSPACE
            textSize = 18f
            letterSpacing = 0.2f
            setTextColor(Ui.TEXT)
            gravity = Gravity.CENTER
        })
        col.addView(Ui.gap(this, 14))
        col.addView(TextView(this).apply {
            text = "On the Dusk dashboard, open the MOBILE LINK module and tap " +
                "PAIR A PHONE. Then just point this phone's camera at the QR code " +
                "that appears — it downloads nothing else and links you automatically."
            textSize = 14f
            setTextColor(Ui.DIM)
            gravity = Gravity.CENTER
            setLineSpacing(0f, 1.35f)
        })
        col.addView(Ui.gap(this, 26))
        col.addView(Ui.value(this, "Make sure Tailscale is connected on this phone first.",
            12f, Ui.LAVENDER).apply { gravity = Gravity.CENTER })
        col.addView(Ui.gap(this, 30))

        // ── fallback: manual entry, hidden until asked for ──
        val toggle = Ui.value(this, "Can't scan? Enter details manually ▾", 13f, Ui.ACCENT).apply {
            gravity = Gravity.CENTER
        }
        col.addView(toggle)

        manualBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
            setPadding(0, Ui.dp(context, 18), 0, 0)
        }
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
            manualBox.addView(f); manualBox.addView(Ui.gap(this, 12))
        }
        manualBox.addView(Ui.pillButton(this, "PAIR", Ui.ACCENT) { pair() })
        col.addView(manualBox)

        toggle.setOnClickListener {
            val show = manualBox.visibility != View.VISIBLE
            manualBox.visibility = if (show) View.VISIBLE else View.GONE
            toggle.text = if (show) "Hide manual entry ▴" else "Can't scan? Enter details manually ▾"
        }

        col.addView(Ui.gap(this, 16))
        status = Ui.value(this, "", 13f, Ui.ROSE).apply { gravity = Gravity.CENTER }
        col.addView(status)

        setContentView(ScrollView(this).apply { addView(col); setBackgroundColor(Ui.BG) })

        handleDeepLink()
    }

    /** cortana://pair?host=&port=&code= from the scanned /get page → auto-pair. */
    private fun handleDeepLink() {
        val uri = intent?.data ?: return
        if (uri.scheme != "cortana" || uri.host != "pair") return
        uri.getQueryParameter("host")?.let { if (it.isNotEmpty()) hostIn.setText(it) }
        uri.getQueryParameter("port")?.let { if (it.isNotEmpty()) portIn.setText(it) }
        uri.getQueryParameter("code")?.let { if (it.isNotEmpty()) codeIn.setText(it) }
        if (!uri.getQueryParameter("host").isNullOrEmpty()
            && uri.getQueryParameter("code")?.length == 6) {
            status.setTextColor(Ui.DIM)
            status.text = "Pairing from QR…"
            pair()
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
            manualBox.visibility = View.VISIBLE
            status.setTextColor(Ui.ROSE)
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
