package com.cortana.mobile

import android.app.AlertDialog
import android.content.Context

/**
 * In-app help. Every non-obvious surface carries a "?" that opens the matching
 * entry here, so behaviour is explained where the user meets it rather than in
 * a README they'd have to go find.
 *
 * House rule for the copy: say what the thing IS, where its data comes from,
 * and what to do when it looks wrong - in that order. Assume a technical
 * reader; skip the hand-holding, keep the specifics (file names, commands).
 */
object Help {

    private val topics = mapOf(
        "link" to ("Mobile link" to """
            This phone talks to one workstation - the machine running Cortana and
            the Dusk dashboard - through a small service there called the bridge
            (cortana-bridge.service, port 8765).

            LINKED means the WebSocket to that bridge is open and state is
            streaming. DISCONNECTED means it isn't; the app keeps retrying with
            backoff and rotates through every address the workstation advertised
            (its Tailscale address and its LAN address), so moving between home
            Wi-Fi and cellular recovers on its own.

            If it stays disconnected: check Tailscale is connected on this phone,
            the workstation is awake, and the bridge is running
            (systemctl --user status cortana-bridge).
        """),

        "cortana" to ("Cortana status" to """
            A live mirror of the orb on the dashboard. States: idle, listening,
            thinking, working, speaking, or offline. The lines underneath are her
            reasoning feed - the same "visible thinking" the dashboard shows.

            SVC ACTIVE/INACTIVE is the systemd unit on the workstation. MANUAL RUN
            means she was started by hand rather than by systemd.

            Offline here doesn't stop you talking to her: the bridge runs voice
            turns itself, so the sphere still works while the service is down.
        """),

        "music" to ("Music" to """
            Now-playing and transport control for Spotify. The phone holds no
            Spotify credentials - it asks the bridge, which acts with the grant
            the dashboard's Music module already has.

            Because your phone is usually the active Spotify device, the device
            name shown here is a useful end-to-end check that the link is really
            working. Playback control needs Spotify Premium; the readout works on
            free accounts too.
        """),

        "agenda" to ("Agenda" to """
            Today's events from Google Calendar, fetched by Cortana on the
            workstation every 10 minutes and mirrored here read-only.

            Working-location blocks, focus time, birthdays and invitations you
            declined are filtered out - they come back from Google's API but
            aren't things you booked.

            If it says the data is from an earlier day, Cortana isn't running on
            the workstation and the agenda is stale rather than empty.
        """),

        "tasks" to ("Tasks" to """
            The task list from the dashboard board, mirrored read-only. Tasks live
            in the dashboard page's own storage, so they reach the phone through
            the snapshot the MOBILE LINK module pushes to the bridge.

            Add, complete and remove tasks on the dashboard - the phone is a
            viewer by design.
        """),

        "git" to ("Git status" to """
            The state of the Cortana repository on the workstation (~/cortana):
            current branch, whether the working tree is clean, and the last five
            commits.

            Useful here because Cortana edits her own source: an unexpected dirty
            tree or an unfamiliar commit usually means a self-edit landed.
        """),

        "weather" to ("Weather" to """
            Fetched by this phone directly from open-meteo.com, using the ZIP code
            set on the dashboard's Weather module (it arrives in the board
            snapshot), so both screens always show the same place.

            No API key and no account - if it's blank, either the ZIP hasn't been
            set on the dashboard or the phone had no connectivity at fetch time.
        """),

        "reorder" to ("Rearranging modules" to """
            Press and hold any card, then drag it up or down. The order is saved
            on this phone and from then on overrides the dashboard's layout
            order.

            Modules can't be added or removed here - the phone mirrors what the
            dashboard board contains. Change the board on the workstation and the
            phone follows.
        """),

        "talk" to ("Talking to Cortana" to """
            Tap the sphere to record, tap again to send. Audio goes to the
            workstation, where her real speech-to-text, reasoning and voice run -
            the reply you hear is her actual voice, not the phone's.

            Tap while she's speaking to cut her off. If the voice pipeline is
            unavailable you still get the reply as text, read aloud by Android's
            own voice. You can type instead at any time.
        """),

        "update" to ("Updates" to """
            CHECK FOR UPDATE asks the workstation to pull the latest build that
            CI published, then offers it if it's newer than what's installed.
            Nothing needs to be run on the workstation by hand.

            Installs use Android's PackageInstaller. On Android 12+ an app
            updating itself can install with no prompt at all; if the platform
            insists on confirming, you'll get its dialog.

            Some skins (OnePlus, OPPO, realme) block that installer silently. If
            an update never appears to happen, use INSTALL VIA WORKSTATION (ADB):
            enable Wireless debugging on this phone and the workstation pushes
            the build over adb instead, which those skins don't intercept.
        """),

        "voice" to ("Voice settings" to """
            By default Cortana's replies stream back as her real voice, generated
            on the workstation. That costs a little bandwidth per reply.

            Switch to the phone's own text-to-speech when you're on a slow or
            metered connection - you keep the conversation, you just lose her
            voice. It's also the automatic fallback whenever the voice pipeline
            can't be reached.
        """),

        "security" to ("Link security" to """
            Pairing exchanged a one-time code for a 256-bit token, stored in this
            phone's encrypted preferences. The workstation keeps only a SHA-256
            hash of it, so its copy can't be used to impersonate this phone.

            Every request carries that token. Revoke it any time from the
            dashboard's MOBILE LINK module (✕ next to this device), or unlink
            here to erase the phone's copy.

            The bridge is never exposed to the internet - it's reachable only on
            your tailnet or your LAN.
        """)
    )

    fun show(ctx: Context, topic: String) {
        val (title, body) = topics[topic] ?: return
        AlertDialog.Builder(ctx)
            .setTitle(title)
            .setMessage(body.trimIndent().trim())
            .setPositiveButton("Got it", null)
            .show()
    }

    fun has(topic: String) = topics.containsKey(topic)
}
