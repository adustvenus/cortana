package com.cortana.mobile

import android.content.Context
import android.os.Handler
import android.os.Looper
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit
import kotlin.math.min

/**
 * The phone end of the Cortana bridge link: REST calls plus one auto-reconnecting
 * WebSocket that receives dashboard-state pushes and announcements.
 *
 * Connectivity model: the bridge lives on the workstation's Tailscale address.
 * Wi-Fi <-> LTE handoffs and workstation sleep show up as socket drops - the WS
 * reconnects with exponential backoff (1s..30s) for as long as start() is
 * active, and every REST call is independent of the socket's health.
 */
object LinkClient {
    private val JSON = "application/json".toMediaType()

    val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .build()

    // Voice turns can legitimately run for minutes (the orchestrator does real
    // work); this client waits instead of aborting a long-running turn.
    val slowHttp: OkHttpClient = http.newBuilder()
        .readTimeout(6, TimeUnit.MINUTES)
        .build()

    interface Listener {
        fun onState(state: JSONObject)
        fun onAnnounce(text: String)
        fun onLink(up: Boolean)
        fun onAuthRejected() {}

        /** The whole announcement frame. Screens only ever want the text, so
         *  the default forwards to onAnnounce and nothing on screen changed;
         *  LinkService overrides it because the urgency decides which
         *  notification channel - and therefore whether the phone buzzes at
         *  3am - the line lands on. */
        fun onAnnounceFull(text: String, urgency: String, id: Int) = onAnnounce(text)
    }

    /** Marker for a holder that is NOT a screen: the foreground service.
     *  uiHolders uses it to tell "somebody is looking at the app" from "the app
     *  is closed and only the service is listening", which is the difference
     *  between showing an announcement as a toast and posting it to the
     *  notification shade. */
    interface Background

    private val main = Handler(Looper.getMainLooper())
    private var ws: WebSocket? = null
    private var wantRun = false
    private var backoffSec = 1L
    // True between newWebSocket() and the first callback: without it, start()
    // being called twice in a row opens two sockets.
    private var dialing = false
    var lastState: JSONObject? = null
        private set
    var linkUp = false
        private set

    // Address fail-over: the stored host first, then every address the bridge
    // advertised (Tailscale + LAN). A phone paired at home therefore keeps
    // working on cellular - it just rotates to the tailnet address - and the
    // winner is promoted to primary so later calls go straight there.
    private var hostIdx = 0

    private fun candidates(ctx: Context): List<String> {
        val primary = Prefs.host(ctx)
        val alts = Prefs.altHosts(ctx)
        return (listOf(primary) + alts).filter { it.isNotEmpty() }.distinct()
    }

    private fun activeHost(ctx: Context): String {
        val c = candidates(ctx)
        if (c.isEmpty()) return Prefs.host(ctx)
        return c[hostIdx % c.size]
    }

    private fun rotateHost(ctx: Context) {
        val c = candidates(ctx)
        if (c.size > 1) hostIdx = (hostIdx + 1) % c.size
    }

    private fun base(ctx: Context) = "http://${activeHost(ctx)}:${Prefs.port(ctx)}"

    private fun authed(ctx: Context, b: Request.Builder) =
        b.header("Authorization", "Bearer ${Prefs.token(ctx)}")

    // ── WebSocket lifecycle ─────────────────────────────────────────────────
    // Everything holding the link open: the screens (start/stop from onStart/
    // onStop) and, since 2.5.0, LinkService. Android starts the incoming
    // activity BEFORE stopping the outgoing one, so MainActivity.onStop would
    // otherwise tear down the socket the AI screen had just brought up - and
    // with the service in the list, backgrounding the app stops closing it at
    // all.
    //
    // This used to be a single `listener` plus a holders COUNT, which meant the
    // second holder silently replaced the first one's callbacks. That was
    // invisible while only one screen at a time could be attached; the service
    // has to keep receiving announcements while MainActivity is attached too,
    // so it is a list now.
    //
    // Every mutation here happens on the main thread. start/stop are called
    // from onStart/onStop and onStartCommand, the socket callbacks post, and
    // that is the whole of the concurrency story.
    private val listeners = ArrayList<Listener>()

    /** Holders that are actual screens. Zero means nobody is looking, which is
     *  exactly when an announcement has to become a notification. */
    val uiHolders: Int get() = listeners.count { it !is Background }

    /** Set by LinkService: inbound {type:"cmd"} frames (SMS and friends),
     *  delivered on the main thread.
     *
     *  Null is NOT "drop the frame". This is one slot and three components can
     *  reach it - the service sets it on attach and clears it on destroy,
     *  MainActivity fills it in when there is no service - which left a window
     *  (stop the service from its own notification while the board is open)
     *  where the socket was up, the switch said the capability was on, and
     *  every command was silently discarded. The frame goes to Comms either
     *  way now; the hook is only about WHICH context handles it, and the
     *  capability switches inside Comms are the thing that actually decides
     *  whether anything happens. */
    var onCmd: ((JSONObject) -> Unit)? = null

    /** Call from the main thread. */
    fun start(ctx: Context, l: Listener) {
        if (!listeners.contains(l)) listeners.add(l)
        wantRun = true
        backoffSec = 1
        connect(ctx.applicationContext)
        lastState?.let { l.onState(it) }
        l.onLink(linkUp)
    }

    /** Call from the main thread. The socket closes only when the LAST holder
     *  lets go, so the service keeps it up while every screen is stopped. */
    fun stop(l: Listener) {
        listeners.remove(l)
        if (listeners.isNotEmpty()) return
        wantRun = false
        ws?.close(1000, "background")
        ws = null
        dialing = false
    }

    /** Reconnect now if the socket is down. The doze alarm and the network
     *  callback both land here: Handler.postDelayed - which is all the backoff
     *  retry has - does not run in deep doze, so without an outside nudge a
     *  socket dropped at 2am stays dropped until the phone is unlocked. */
    fun poke(ctx: Context) {
        val app = ctx.applicationContext
        main.post {
            if (!wantRun) return@post
            backoffSec = 1
            connect(app)
        }
    }

    private fun connect(ctx: Context) {
        if (!wantRun || !Prefs.paired(ctx)) return
        // Added with the service: start() used to dial unconditionally, so
        // every onStart leaked another live socket behind the field. Harmless
        // enough with one screen; with a service that restarts it is a pile-up.
        if (dialing || ws != null) return
        dialing = true
        val tryHost = activeHost(ctx)
        val req = authed(ctx, Request.Builder().url(
            "ws://$tryHost:${Prefs.port(ctx)}/api/ws")).build()
        ws = http.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                // linkUp and backoffSec are read and written by start/stop and
                // by the other callbacks, all of which run on the main thread.
                // Setting them here, on OkHttp's reader thread, was the one
                // exception - and it raced onClosed from the socket this one
                // replaced.
                main.post { dialing = false; backoffSec = 1; setLink(true) }
                // This address works - make it the primary so REST calls and
                // the next launch skip the dead one entirely.
                if (tryHost.isNotEmpty() && tryHost != Prefs.host(ctx)) {
                    Prefs.setHost(ctx, tryHost)
                    hostIdx = 0
                }
                // Tell the workstation which app version this phone runs, so
                // the dashboard can show per-device "up to date" / "update
                // available" next to the linked device.
                try {
                    webSocket.send(JSONObject().put("type", "hello")
                        .put("version", BuildConfig.VERSION_NAME)
                        // Anything announced while this phone was away gets
                        // replayed from here, so a completion is not lost just
                        // because the app happened to be closed.
                        .put("lastAnnounce", Prefs.lastAnnounce(ctx)).toString())
                } catch (e: Exception) { /* presence still works without it */ }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val j = try { JSONObject(text) } catch (e: Exception) { return }
                when (j.optString("type")) {
                    "state" -> {
                        lastState = j
                        val dash = j.optString("host")
                        if (dash.isNotEmpty() && dash != Prefs.dashName(ctx))
                            Prefs.setDashName(ctx, dash)
                        // Remember every address the bridge can be reached on,
                        // so leaving/joining the LAN never strands the phone.
                        j.optJSONArray("addresses")?.let { arr ->
                            val list = (0 until arr.length()).map { arr.optString(it) }
                                .filter { it.isNotEmpty() }
                            if (list.isNotEmpty() && list != Prefs.altHosts(ctx))
                                Prefs.setAltHosts(ctx, list)
                        }
                        main.post { listeners.toList().forEach { l -> l.onState(j) } }
                    }
                    "announce" -> {
                        val aid = j.optInt("id", 0)
                        if (aid > 0) Prefs.setLastAnnounce(ctx, aid)
                        val text = j.optString("text")
                        // A bridge that predates the schedule work sends no
                        // urgency at all, and "normal" is the right reading of
                        // a plain spoken line.
                        val urg = j.optString("urgency").ifEmpty { "normal" }
                        main.post {
                            listeners.toList().forEach { l -> l.onAnnounceFull(text, urg, aid) }
                        }
                    }
                    // The workstation asking this phone to do something (send
                    // an SMS, read the inbox). Only for capabilities the user
                    // switched on - Comms refuses the rest by name.
                    "cmd" -> main.post {
                        val hook = onCmd
                        if (hook != null) hook(j) else Comms.handleCmd(ctx, j)
                    }
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                val code = response?.code
                // Everything below mutates shared state, so it runs on the main
                // thread like every other mutation here. connect() is called
                // from the main thread, so the field is always assigned by the
                // time this runnable gets to compare against it.
                main.post {
                    // A terminal callback from a socket that is no longer the
                    // live one is HISTORY. Acting on it flipped linkUp to false
                    // over a working socket - close(1000) does not finish until
                    // the peer answers, so a background/foreground bounce lands
                    // the old socket's callback after the new one is already
                    // open - and nothing sets linkUp back to true without a
                    // fresh onOpen. The card and the service's permanent row
                    // then read DISCONNECTED for as long as the link stays up,
                    // which is the exact lie this file is supposed to prevent.
                    // ws == null is the after-stop() case and still counts.
                    if (ws != null && ws !== webSocket) return@post
                    ws = null
                    dialing = false
                    setLink(false)
                    if (code == 401) {
                        // Token revoked on the dashboard - stop hammering, re-pair.
                        listeners.toList().forEach { l -> l.onAuthRejected() }
                        return@post
                    }
                    if (!wantRun) return@post
                    // Unreachable on this address (wrong network, workstation
                    // moved): try the next known one before backing off further.
                    rotateHost(ctx)
                    val delay = backoffSec
                    backoffSec = min(backoffSec * 2, 30)
                    main.postDelayed({ if (wantRun) connect(ctx) }, delay * 1000)
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                main.post {
                    // Same rule as onFailure: only the current socket - or none
                    // at all - may move this state.
                    if (ws != null && ws !== webSocket) return@post
                    ws = null
                    dialing = false
                    setLink(false)
                    // A clean close is not a failure, so onFailure's retry never
                    // runs for it. Before the service that did not matter -
                    // something would reopen the socket the next time a screen
                    // appeared - but a background holder has no such event, and
                    // a bridge restart would have left it down until morning.
                    if (wantRun) main.postDelayed({ if (wantRun) connect(ctx) }, 2000)
                }
            }
        })
    }

    private fun setLink(up: Boolean) {
        if (linkUp == up) return
        linkUp = up
        main.post { listeners.toList().forEach { l -> l.onLink(up) } }
    }

    // ── REST (all blocking - call from a background thread) ──
    fun pair(host: String, port: Int, code: String, deviceName: String): JSONObject {
        val body = JSONObject().put("code", code).put("deviceName", deviceName)
            .toString().toRequestBody(JSON)
        http.newCall(Request.Builder().url("http://$host:$port/api/pair").post(body).build())
            .execute().use { r ->
                val j = JSONObject(r.body?.string() ?: "{}")
                if (!r.isSuccessful) throw Exception(j.optString("error", "HTTP ${r.code}"))
                return j
            }
    }

    fun converse(ctx: Context, wav: File?, text: String?): JSONObject {
        val req = if (wav != null) {
            val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                .addFormDataPart("audio", "utterance.wav",
                    wav.asRequestBody("audio/wav".toMediaType()))
                .build()
            authed(ctx, Request.Builder().url("${base(ctx)}/api/converse").post(body)).build()
        } else {
            val body = JSONObject().put("text", text ?: "").toString().toRequestBody(JSON)
            authed(ctx, Request.Builder().url("${base(ctx)}/api/converse").post(body)).build()
        }
        slowHttp.newCall(req).execute().use { r ->
            if (r.code == 401) throw AuthException()
            return JSONObject(r.body?.string() ?: "{}")
        }
    }

    /** Cortana's real voice for [text]; null = no audio (use the phone's TTS). */
    fun tts(ctx: Context, text: String): ByteArray? {
        val body = JSONObject().put("text", text).toString().toRequestBody(JSON)
        slowHttp.newCall(authed(ctx, Request.Builder()
            .url("${base(ctx)}/api/tts").post(body)).build()).execute().use { r ->
            if (r.code == 401) throw AuthException()
            if (r.code != 200) return null
            return r.body?.bytes()
        }
    }

    /** Task edit relayed to the dashboard (which owns the task list):
     *  add {op:'add', t:text} or toggle {op:'toggle', id}. Blocking. */
    fun taskOp(ctx: Context, op: JSONObject): JSONObject {
        val body = op.toString().toRequestBody(JSON)
        http.newCall(authed(ctx, Request.Builder()
            .url("${base(ctx)}/api/tasks").post(body)).build()).execute().use { r ->
            if (r.code == 401) throw AuthException()
            return JSONObject(r.body?.string() ?: "{}")
        }
    }

    fun spotify(ctx: Context, action: String): JSONObject {
        val body = JSONObject().put("action", action).toString().toRequestBody(JSON)
        http.newCall(authed(ctx, Request.Builder()
            .url("${base(ctx)}/api/spotify").post(body)).build()).execute().use { r ->
            if (r.code == 401) throw AuthException()
            return JSONObject(r.body?.string() ?: "{}")
        }
    }

    private fun postJson(ctx: Context, path: String, body: JSONObject): JSONObject {
        http.newCall(authed(ctx, Request.Builder()
            .url("${base(ctx)}$path").post(body.toString().toRequestBody(JSON)))
            .build()).execute().use { r ->
            if (r.code == 401) throw AuthException()
            return try { JSONObject(r.body?.string() ?: "{}") } catch (e: Exception) { JSONObject() }
        }
    }

    /** Where this phone thinks its owner is. Blocking; Presence calls it from a
     *  worker thread and treats every failure as "try again at the next
     *  event", because an unreachable workstation is the ordinary case for a
     *  phone that has left the house. */
    fun postPresence(ctx: Context, body: JSONObject): JSONObject =
        postJson(ctx, "/api/presence", body)

    /** Mirrored notifications and/or recent SMS. Blocking. */
    fun postComms(ctx: Context, body: JSONObject): JSONObject =
        postJson(ctx, "/api/comms/sync", body)

    /** The outcome of a {type:"cmd"} frame. Sent even when the phone REFUSED
     *  the command: a workstation that hears nothing back cannot tell a
     *  switched-off capability from a broken phone. Blocking. */
    fun postCmdResult(ctx: Context, body: JSONObject): JSONObject =
        postJson(ctx, "/api/cmd/result", body)

    fun fetchBytes(url: String): ByteArray? = try {
        http.newCall(Request.Builder().url(url).build()).execute().use { r ->
            if (r.isSuccessful) r.body?.bytes() else null
        }
    } catch (e: Exception) { null }

    /** One-shot state fetch over REST - the pull-to-refresh path, independent
     *  of the WebSocket's health. Blocking. */
    fun fetchState(ctx: Context): JSONObject {
        http.newCall(authed(ctx, Request.Builder()
            .url("${base(ctx)}/api/state")).build()).execute().use { r ->
            if (r.code == 401) throw AuthException()
            val j = JSONObject(r.body?.string() ?: "{}")
            if (r.isSuccessful) lastState = j
            return j
        }
    }

    /** Ask the workstation to git-pull so mobile/dist matches CI's latest
     *  build, then report the (possibly new) APK info. Blocking; can take a
     *  minute on a slow pull. */
    fun apkRefresh(ctx: Context): JSONObject {
        val body = "{}".toRequestBody(JSON)
        slowHttp.newCall(authed(ctx, Request.Builder()
            .url("${base(ctx)}/api/apk/refresh").post(body)).build()).execute().use { r ->
            if (r.code == 401) throw AuthException()
            return JSONObject(r.body?.string() ?: "{}")
        }
    }

    /** Ask the workstation to install the update to THIS phone over wireless
     *  adb (the privileged path some skins force us onto). Blocking. */
    fun apkAdbInstall(ctx: Context, port: Int): JSONObject {
        val body = JSONObject().put("port", port).toString().toRequestBody(JSON)
        slowHttp.newCall(authed(ctx, Request.Builder()
            .url("${base(ctx)}/api/apk/adb").post(body)).build()).execute().use { r ->
            if (r.code == 401) throw AuthException()
            return JSONObject(r.body?.string() ?: "{}")
        }
    }

    fun downloadApk(ctx: Context, dest: File, onProgress: (Int) -> Unit) {
        slowHttp.newCall(authed(ctx, Request.Builder()
            .url("${base(ctx)}/api/apk/download")).build()).execute().use { r ->
            if (r.code == 401) throw AuthException()
            if (!r.isSuccessful) throw Exception("download failed: HTTP ${r.code}")
            val body = r.body ?: throw Exception("empty download")
            val total = body.contentLength()
            dest.outputStream().use { out ->
                val buf = ByteArray(65536)
                var done = 0L
                body.byteStream().use { ins ->
                    while (true) {
                        val n = ins.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        done += n
                        if (total > 0) onProgress((done * 100 / total).toInt())
                    }
                }
            }
        }
    }

    class AuthException : Exception("unauthorized - re-pair this phone")
}
