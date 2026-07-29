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
    }

    private val main = Handler(Looper.getMainLooper())
    private var ws: WebSocket? = null
    private var wantRun = false
    private var backoffSec = 1L
    private var listener: Listener? = null
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

    // ── WebSocket lifecycle (foreground only - activities call start/stop) ──
    fun start(ctx: Context, l: Listener) {
        listener = l
        wantRun = true
        backoffSec = 1
        connect(ctx.applicationContext)
        lastState?.let { l.onState(it) }
        l.onLink(linkUp)
    }

    fun stop() {
        wantRun = false
        listener = null
        ws?.close(1000, "background")
        ws = null
    }

    private fun connect(ctx: Context) {
        if (!wantRun || !Prefs.paired(ctx)) return
        val tryHost = activeHost(ctx)
        val req = authed(ctx, Request.Builder().url(
            "ws://$tryHost:${Prefs.port(ctx)}/api/ws")).build()
        ws = http.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                backoffSec = 1
                // This address works - make it the primary so REST calls and
                // the next launch skip the dead one entirely.
                if (tryHost.isNotEmpty() && tryHost != Prefs.host(ctx)) {
                    Prefs.setHost(ctx, tryHost)
                    hostIdx = 0
                }
                setLink(true)
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
                        main.post { listener?.onState(j) }
                    }
                    "announce" -> main.post { listener?.onAnnounce(j.optString("text")) }
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                setLink(false)
                if (response?.code == 401) {
                    // Token revoked on the dashboard - stop hammering, re-pair.
                    main.post { listener?.onAuthRejected() }
                    return
                }
                if (!wantRun) return
                // Unreachable on this address (wrong network, workstation moved):
                // try the next known one before backing off further.
                rotateHost(ctx)
                val delay = backoffSec
                backoffSec = min(backoffSec * 2, 30)
                main.postDelayed({ if (wantRun) connect(ctx) }, delay * 1000)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                setLink(false)
            }
        })
    }

    private fun setLink(up: Boolean) {
        if (linkUp == up) return
        linkUp = up
        main.post { listener?.onLink(up) }
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

    fun spotify(ctx: Context, action: String): JSONObject {
        val body = JSONObject().put("action", action).toString().toRequestBody(JSON)
        http.newCall(authed(ctx, Request.Builder()
            .url("${base(ctx)}/api/spotify").post(body)).build()).execute().use { r ->
            if (r.code == 401) throw AuthException()
            return JSONObject(r.body?.string() ?: "{}")
        }
    }

    fun fetchBytes(url: String): ByteArray? = try {
        http.newCall(Request.Builder().url(url).build()).execute().use { r ->
            if (r.isSuccessful) r.body?.bytes() else null
        }
    } catch (e: Exception) { null }

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
