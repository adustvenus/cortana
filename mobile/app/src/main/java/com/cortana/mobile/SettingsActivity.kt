package com.cortana.mobile

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast

class SettingsActivity : Activity() {

    private var adbPortField: EditText? = null

    // onResume redraws this screen, and onCreate is immediately followed by
    // onResume - without this the whole column is built twice on entry.
    private var justBuilt = false

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
        // Every activity loads the palette itself: the widget can launch the
        // talk screen straight into a cold process, with no MainActivity to
        // have done it. Cheap, idempotent, and reads a cached value.
        Theme.load(this)
        setContentView(buildUi())
        justBuilt = true
    }

    /**
     * Half of this screen reports grants that are given on OTHER screens -
     * notification access, battery optimisation, the location dialog - and the
     * user always comes back here straight afterwards. Redrawing on resume is
     * the only way the switches describe the state we actually returned to; a
     * screen that says "granted" over a refused permission is the failure this
     * whole section exists to avoid.
     */
    override fun onResume() {
        super.onResume()
        if (justBuilt) { justBuilt = false; return }
        setContentView(buildUi())
        LinkService.sync(this)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>,
                                            grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        setContentView(buildUi())
    }

    override fun onStop() {
        super.onStop()
        // Flipping the background switch here only takes effect once something
        // starts the service, and Android 12+ refuses a foreground start from
        // the background. Doing it while this activity is still on screen is
        // the reliable moment.
        LinkService.sync(this)
    }

    // ── the screen ──────────────────────────────────────────────────────────
    private fun buildUi(): View {
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

        linkSection(col)
        backgroundSection(col)
        presenceSection(col)
        commsSection(col)
        voiceSection(col)
        updatesSection(col)
        aboutSection(col)

        return ScrollView(this).apply { addView(col); setBackgroundColor(Ui.BG) }
    }

    // ── LINK ────────────────────────────────────────────────────────────────
    private fun linkSection(col: LinearLayout) {
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
    }

    // ── BACKGROUND ──────────────────────────────────────────────────────────
    private fun backgroundSection(col: LinearLayout) {
        col.addView(Ui.row(this, Ui.label(this, "BACKGROUND"), Ui.helpIcon(this, "background")))
        col.addView(Ui.gap(this, 6))
        col.addView(toggle("Keep the link open while the app is closed",
            Prefs.background(this)) { on ->
            Prefs.setBackground(this, on)
            LinkService.sync(this)
            if (on && Build.VERSION.SDK_INT >= 33) request(Manifest.permission.POST_NOTIFICATIONS)
        })
        col.addView(Ui.value(this,
            "Runs a service with a permanent low-priority notification, holding " +
            "the WebSocket open. Without it, an announcement made while the app " +
            "is closed only reaches you the next time you open it.",
            13f, Ui.DIM))
        col.addView(Ui.gap(this, 10))

        val exempt = ignoringBatteryOptimisation()
        col.addView(Ui.value(this,
            if (exempt) "Battery optimisation: EXEMPT"
            else "Battery optimisation: ON — Android may kill the socket overnight",
            13f, if (exempt) Ui.GREEN else Ui.ACCENT, mono = true))
        col.addView(Ui.gap(this, 8))
        if (!exempt) {
            col.addView(Ui.pillButton(this, "EXEMPT FROM BATTERY OPTIMISATION", Ui.ACCENT) {
                askBatteryExemption()
            })
            col.addView(Ui.gap(this, 8))
        }
        col.addView(Ui.pillButton(this, "NOTIFICATION SETTINGS", Ui.DIM) {
            openAppNotificationSettings()
        })
        col.addView(Ui.gap(this, 24))
    }

    /**
     * Deep doze suspends network access and every Handler timer with it, so a
     * socket that drops at 3am stays dropped. The exemption is the platform's
     * own answer to that. Play Store policy forbids most apps from asking for
     * it; this app is sideloaded onto one phone by the person who built it, so
     * the policy does not apply - but it is still opt-in, and the link works
     * (worse, with a ~15-minute reconnect backstop) without it.
     */
    private fun askBatteryExemption() {
        try {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:$packageName")))
        } catch (e: Exception) {
            // Some skins remove the direct dialog. The list screen always
            // exists, it just takes two more taps.
            try {
                startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            } catch (e2: Exception) {
                toast("This phone has no battery-optimisation screen to open.")
            }
        }
    }

    private fun ignoringBatteryOptimisation(): Boolean = try {
        getSystemService(PowerManager::class.java)
            ?.isIgnoringBatteryOptimizations(packageName) ?: false
    } catch (e: Exception) { false }

    private fun openAppNotificationSettings() {
        try {
            startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                .putExtra(Settings.EXTRA_APP_PACKAGE, packageName))
        } catch (e: Exception) {
            try {
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:$packageName")))
            } catch (e2: Exception) { toast("Couldn't open the notification screen.") }
        }
    }

    // ── PRESENCE ────────────────────────────────────────────────────────────
    private fun presenceSection(col: LinearLayout) {
        col.addView(Ui.row(this, Ui.label(this, "PRESENCE"), Ui.helpIcon(this, "presence")))
        col.addView(Ui.gap(this, 6))
        col.addView(Ui.value(this,
            "OFF by default. When on, this phone tells the workstation: whether " +
            "you are at a place you saved as home or work, whether it is " +
            "charging, whether a car's hands-free unit is connected, whether the " +
            "screen is on, and a coarse position rounded to about 110 metres. " +
            "It sends when one of those changes, and once every half hour " +
            "otherwise — not continuously.",
            13f, Ui.DIM))
        col.addView(Ui.gap(this, 8))
        col.addView(toggle("Report presence to the workstation",
            Prefs.presenceOn(this)) { on ->
            Prefs.setPresenceOn(this, on)
            if (on) {
                // ONE call, not two. Activity.requestPermissions refuses a
                // second request while the first dialog is still up - it logs
                // "Can request only one set of permissions at a time" and
                // returns an empty result - so asking for these separately got
                // BLUETOOTH_CONNECT silently dropped every time, and from API
                // 31 that grant is what lets the phone RECEIVE the car's
                // ACL_CONNECTED at all. Driving detection was dead and nothing
                // said so. Background location stays a separate request below:
                // Android requires that one to come after the foreground grant.
                val perms = ArrayList<String>()
                perms.add(Manifest.permission.ACCESS_COARSE_LOCATION)
                if (Build.VERSION.SDK_INT >= 31)
                    perms.add(Manifest.permission.BLUETOOTH_CONNECT)
                request(*perms.toTypedArray())
                Presence.start(this)
            } else {
                Presence.stop(this)
            }
        })

        if (Prefs.presenceOn(this)) {
            col.addView(Ui.gap(this, 8))
            col.addView(Ui.value(this, Presence.describe(this), 13f, Ui.LAVENDER, mono = true))
            col.addView(Ui.gap(this, 8))
            if (!Presence.hasLocation(this)) {
                col.addView(Ui.pillButton(this, "ALLOW COARSE LOCATION", Ui.ROSE) {
                    request(Manifest.permission.ACCESS_COARSE_LOCATION)
                })
                col.addView(Ui.gap(this, 8))
            } else if (!Presence.hasBackgroundLocation(this)) {
                // Android insists this is a SECOND, separate request made after
                // the foreground one is already granted; asking for both at once
                // gets the background half silently dropped.
                col.addView(Ui.value(this,
                    "Location is allowed only while the app is open, so presence " +
                    "stops the moment you leave the app. Set it to \"Allow all " +
                    "the time\" for this to be useful.", 13f, Ui.ACCENT))
                col.addView(Ui.gap(this, 6))
                col.addView(Ui.pillButton(this, "ALLOW ALL THE TIME", Ui.ACCENT) {
                    if (Build.VERSION.SDK_INT >= 29)
                        request(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
                })
                col.addView(Ui.gap(this, 8))
            }
            col.addView(Ui.row(this,
                Ui.pillButton(this, if (Prefs.hasHome(this)) "HOME SET" else "SET HOME HERE",
                    if (Prefs.hasHome(this)) Ui.GREEN else Ui.LAVENDER) { saveHere("home") },
                View(this).apply {
                    layoutParams = LinearLayout.LayoutParams(
                        Ui.dp(this@SettingsActivity, 10), 1)
                },
                Ui.pillButton(this, if (Prefs.hasWork(this)) "WORK SET" else "SET WORK HERE",
                    if (Prefs.hasWork(this)) Ui.GREEN else Ui.LAVENDER) { saveHere("work") }))
            col.addView(Ui.gap(this, 8))
            col.addView(Ui.pillButton(this, "FORGET SAVED PLACES", Ui.ROSE) {
                Prefs.clearPresenceData(this)
                setContentView(buildUi())
                toast("Home, work and the last fix are gone from this phone.")
            })
        }
        col.addView(Ui.gap(this, 24))
    }

    private fun saveHere(which: String) {
        val err = Presence.saveHere(this, which)
        if (err != null) toast(err)
        else {
            setContentView(buildUi())
            toast("Saved this spot as $which (within ${Presence.ZONE_RADIUS_M.toInt()}m).")
        }
    }

    // ── COMMS ───────────────────────────────────────────────────────────────
    private fun commsSection(col: LinearLayout) {
        col.addView(Ui.row(this, Ui.label(this, "COMMS"), Ui.helpIcon(this, "comms")))
        col.addView(Ui.gap(this, 6))
        col.addView(Ui.value(this,
            "Lets Cortana see what the phone sees. Every switch is off until you " +
            "turn it on, and turning one off stops that traffic immediately even " +
            "though the system permission stays granted.",
            13f, Ui.DIM))
        col.addView(Ui.gap(this, 10))

        val access = Comms.notificationAccessGranted(this)
        col.addView(toggle("Mirror this phone's notifications",
            Prefs.commsNotifications(this)) { on ->
            Prefs.setCommsNotifications(this, on)
            if (on && !Comms.notificationAccessGranted(this))
                toast("Also grant notification access below, or nothing is mirrored.")
        })
        col.addView(Ui.value(this,
            if (access) "Notification access: GRANTED"
            else "Notification access: NOT GRANTED — nothing will be mirrored",
            13f, if (access) Ui.GREEN else Ui.ACCENT, mono = true))
        col.addView(Ui.gap(this, 8))
        col.addView(Ui.pillButton(this,
            if (access) "REVIEW NOTIFICATION ACCESS" else "GRANT NOTIFICATION ACCESS",
            if (access) Ui.DIM else Ui.LAVENDER) {
            // No app may request this one programmatically; the system screen
            // is the only place it can be given.
            try {
                startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
            } catch (e: Exception) {
                toast("Open Settings → Apps → Special access → Notification access.")
            }
        })
        col.addView(Ui.gap(this, 14))

        col.addView(toggle("Let Cortana read recent SMS", Prefs.smsRead(this)) { on ->
            Prefs.setSmsRead(this, on)
            // READ_SMS only. RECEIVE_SMS is not declared and nothing here
            // receives one; requesting it produced a dialog for a permission
            // the app has no use for.
            // Contacts alongside SMS, because a mirror that can only say
            // "+15550100 texted you" is barely worth having. Optional: refuse
            // it and everything still works in numbers.
            if (on) request(Manifest.permission.READ_SMS, Manifest.permission.READ_CONTACTS)
        })
        col.addView(toggle("Let Cortana send SMS on request", Prefs.smsSend(this)) { on ->
            Prefs.setSmsSend(this, on)
            // Without Contacts, "text Mum" cannot be resolved and Cortana has
            // to be given a number - so ask for both here too.
            if (on) request(Manifest.permission.SEND_SMS, Manifest.permission.READ_CONTACTS)
        })
        col.addView(Ui.gap(this, 10))
        col.addView(Ui.value(this,
            "Leaving this phone right now: ${Comms.describe(this)}.",
            13f, Ui.LAVENDER, mono = true))
        col.addView(Ui.gap(this, 24))
    }

    // ── VOICE ───────────────────────────────────────────────────────────────
    private fun voiceSection(col: LinearLayout) {
        col.addView(Ui.row(this, Ui.label(this, "VOICE"), Ui.helpIcon(this, "voice")))
        col.addView(Ui.gap(this, 6))
        col.addView(toggle("Use phone TTS instead of Cortana's voice (saves data)",
            Prefs.localTtsOnly(this)) { v -> Prefs.setLocalTtsOnly(this, v) })
        col.addView(Ui.gap(this, 24))
    }

    // ── UPDATES ─────────────────────────────────────────────────────────────
    private fun updatesSection(col: LinearLayout) {
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
    }

    // ── ABOUT ───────────────────────────────────────────────────────────────
    private fun aboutSection(col: LinearLayout) {
        col.addView(Ui.label(this, "ABOUT"))
        col.addView(Ui.gap(this, 6))
        col.addView(Ui.value(this,
            "Cortana Mobile - the phone mirror of the Dusk dashboard, plus voice, " +
            "and whichever of the background link, presence and comms switches " +
            "above you chose to turn on. Connects only over your tailnet; the " +
            "pairing token lives in encrypted storage on this phone.", 13f, Ui.DIM))
    }

    // ── helpers ─────────────────────────────────────────────────────────────
    /** isChecked is set BEFORE the listener is installed - the other way round
     *  fires the change handler on every rebuild of this screen. */
    private fun toggle(label: String, on: Boolean, onChange: (Boolean) -> Unit): Switch =
        Switch(this).apply {
            text = label
            isChecked = on
            setTextColor(Ui.TEXT)
            textSize = 14f
            setOnCheckedChangeListener { _, v -> onChange(v) }
        }

    private fun request(vararg perms: String) {
        val need = perms.filter {
            try { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
            catch (e: Exception) { false }
        }
        if (need.isEmpty()) return
        try { requestPermissions(need.toTypedArray(), 21) } catch (e: Exception) { }
    }
}
