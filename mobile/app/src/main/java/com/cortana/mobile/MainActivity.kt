package com.cortana.mobile

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Typeface
import android.os.Bundle
import android.view.Gravity
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.recyclerview.widget.ItemTouchHelper
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import org.json.JSONArray
import org.json.JSONObject
import java.util.Collections
import kotlin.concurrent.thread

/**
 * The phone-side mirror of the Dusk dashboard: the same modules and palette,
 * stacked in one scrolling column, since a phone can't fit the 24x16 grid.
 *
 * Content is strictly view-only - no service controls, no task editing. The
 * three interactions the product does allow: talk to Cortana, drive Spotify
 * transport, and press-and-hold to reorder cards (phone-local; nothing can be
 * added or removed here).
 *
 * Rendering is diff-based. State arrives every ~1.5s, so rebuilding every card
 * each push would flicker, fight an in-progress drag and reset scroll. Each
 * card carries a signature of the data it draws; only cards whose signature
 * changed are rebuilt, and the list is only re-notified when the set or order
 * actually differs.
 */
class MainActivity : Activity(), LinkClient.Listener {

    /** Module types this app can render, in default order. */
    private val supported = listOf("cortana", "music", "agenda", "tasks", "weather", "git")

    private lateinit var recycler: RecyclerView
    private lateinit var adapter: CardAdapter
    private lateinit var linkDot: TextView

    private val cards = HashMap<String, LinearLayout>()   // type -> built view
    private val signatures = HashMap<String, String>()    // type -> data fingerprint
    private val artCache = HashMap<String, Bitmap>()

    private var lastAnnounce = ""
    private var weather: JSONObject? = null
    private var weatherZip = ""
    private var weatherAt = 0L
    private var dragging = false
    private var pendingState: JSONObject? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!Prefs.paired(this)) {
            startActivity(Intent(this, PairActivity::class.java))
            finish()
            return
        }
        setContentView(buildScaffold())
        showPlaceholder("Connecting to ${Prefs.dashName(this).ifEmpty { Prefs.host(this) }}…")
    }

    private fun buildScaffold(): LinearLayout {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Ui.BG)
        }
        linkDot = TextView(this).apply { text = "●"; textSize = 14f; setTextColor(Ui.DIM) }
        val title = TextView(this).apply {
            text = "DUSK"
            typeface = Typeface.MONOSPACE
            textSize = 15f
            letterSpacing = 0.24f
            setTextColor(Ui.TEXT)
            setPadding(Ui.dp(context, 10), 0, 0, 0)
        }
        val sphereBtn = ImageView(this).apply {
            setImageResource(R.drawable.sphere)
            layoutParams = LinearLayout.LayoutParams(Ui.dp(context, 34), Ui.dp(context, 34))
                .apply { rightMargin = Ui.dp(context, 14) }
            contentDescription = "Talk to Cortana"
            setOnClickListener { startActivity(Intent(context, TalkActivity::class.java)) }
        }
        val gear = TextView(this).apply {
            text = "⚙"
            textSize = 22f
            setTextColor(Ui.DIM)
            setPadding(Ui.dp(context, 6), 0, Ui.dp(context, 4), 0)
            contentDescription = "Settings"
            setOnClickListener { startActivity(Intent(context, SettingsActivity::class.java)) }
        }
        root.addView(Ui.row(this, linkDot, title, Ui.spacer(this), sphereBtn, gear).apply {
            setPadding(Ui.dp(context, 18), Ui.dp(context, 16), Ui.dp(context, 12), Ui.dp(context, 10))
        })

        adapter = CardAdapter()
        recycler = RecyclerView(this).apply {
            layoutManager = LinearLayoutManager(context)
            adapter = this@MainActivity.adapter
            clipToPadding = false
            setPadding(0, 0, 0, Ui.dp(context, 24))
        }
        ItemTouchHelper(dragCallback).attachToRecyclerView(recycler)
        root.addView(recycler,
            LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))
        return root
    }

    override fun onStart() {
        super.onStart()
        if (Prefs.paired(this)) LinkClient.start(this, this)
    }

    override fun onStop() {
        super.onStop()
        LinkClient.stop()
    }

    // ── list + drag-to-reorder ──────────────────────────────────────────────
    private inner class Holder(val frame: FrameLayout) : RecyclerView.ViewHolder(frame)

    private inner class CardAdapter : RecyclerView.Adapter<Holder>() {
        val order = ArrayList<String>()   // module types; "link" is pinned at 0

        /** Replace the visible list. Only notifies when the order actually
         *  changed - an unchanged push must not disturb scroll or drag. */
        fun setOrder(next: List<String>) {
            if (order == next) return
            order.clear(); order.addAll(next)
            notifyDataSetChanged()
        }

        /** Redraw one card in place (used when only its data changed). */
        fun refresh(type: String) {
            val i = order.indexOf(type)
            if (i >= 0) notifyItemChanged(i)
        }

        fun move(from: Int, to: Int) {
            if (from < to) for (i in from until to) Collections.swap(order, i, i + 1)
            else for (i in from downTo to + 1) Collections.swap(order, i, i - 1)
            notifyItemMoved(from, to)
        }

        override fun getItemCount() = order.size

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) =
            Holder(FrameLayout(parent.context).apply {
                layoutParams = RecyclerView.LayoutParams(
                    RecyclerView.LayoutParams.MATCH_PARENT,
                    RecyclerView.LayoutParams.WRAP_CONTENT)
            })

        override fun onBindViewHolder(holder: Holder, position: Int) {
            val card = cards[order[position]] ?: return
            (card.parent as? ViewGroup)?.removeView(card)
            val m = Ui.dp(holder.frame.context, 14)
            val v = Ui.dp(holder.frame.context, 6)
            card.layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT).apply { setMargins(m, v, m, v) }
            holder.frame.removeAllViews()
            holder.frame.addView(card)
        }
    }

    private val dragCallback = object : ItemTouchHelper.Callback() {
        override fun isLongPressDragEnabled() = true

        override fun getMovementFlags(rv: RecyclerView, vh: RecyclerView.ViewHolder) =
            if (vh.bindingAdapterPosition <= 0) makeMovementFlags(0, 0)   // link card pinned
            else makeMovementFlags(ItemTouchHelper.UP or ItemTouchHelper.DOWN, 0)

        override fun onMove(rv: RecyclerView, vh: RecyclerView.ViewHolder,
                            target: RecyclerView.ViewHolder): Boolean {
            val from = vh.bindingAdapterPosition
            val to = target.bindingAdapterPosition
            if (from <= 0 || to <= 0) return false
            adapter.move(from, to)
            return true
        }

        override fun onSwiped(vh: RecyclerView.ViewHolder, dir: Int) {}

        override fun onSelectedChanged(vh: RecyclerView.ViewHolder?, actionState: Int) {
            super.onSelectedChanged(vh, actionState)
            val nowDragging = actionState == ItemTouchHelper.ACTION_STATE_DRAG
            if (dragging && !nowDragging) {
                dragging = false
                Prefs.setModuleOrder(this@MainActivity,
                    adapter.order.filter { it != "link" })
                pendingState?.let { render(it) }     // apply what we deferred
                pendingState = null
            } else {
                dragging = nowDragging
            }
        }
    }

    // ── ordering ────────────────────────────────────────────────────────────
    /** The user's saved drag order wins; otherwise mirror the dashboard board;
     *  supported types missing from both are appended so nothing disappears. */
    private fun phoneOrder(state: JSONObject): List<String> {
        val saved = Prefs.moduleOrder(this).filter { it in supported }
        val base = saved.ifEmpty { boardOrder(state) }
        return base + supported.filter { it !in base }
    }

    private fun boardOrder(state: JSONObject): List<String> {
        val layout = state.optJSONObject("board")?.optJSONArray("layout") ?: return supported
        val placed = ArrayList<Pair<Int, String>>()
        for (i in 0 until layout.length()) {
            val m = layout.optJSONObject(i) ?: continue
            placed.add(Pair(m.optInt("y") * 100 + m.optInt("x"), m.optString("type")))
        }
        placed.sortBy { it.first }
        return LinkedHashSet(placed.map { it.second }.filter { it in supported }).toList()
    }

    // ── LinkClient.Listener ─────────────────────────────────────────────────
    override fun onLink(up: Boolean) {
        linkDot.setTextColor(if (up) Ui.GREEN else Ui.ROSE)
        val st = LinkClient.lastState
        if (st != null) {
            signatures.remove("link")      // link card shows connection state
            render(st)
        } else if (!up) {
            showPlaceholder("Link down - reconnecting…\n" +
                "Check Tailscale on this phone, and that the workstation is awake.")
        }
    }

    override fun onAnnounce(text: String) {
        lastAnnounce = text
        signatures.remove("link")
        Toast.makeText(this, "Cortana: $text", Toast.LENGTH_LONG).show()
        LinkClient.lastState?.let { render(it) }
    }

    override fun onAuthRejected() {
        AlertDialog.Builder(this)
            .setTitle("Link revoked")
            .setMessage("This phone's access was revoked on the dashboard. Pair again to reconnect.")
            .setPositiveButton("Re-pair") { _, _ ->
                Prefs.unlink(this)
                startActivity(Intent(this, PairActivity::class.java))
                finish()
            }
            .setNegativeButton("Close", null)
            .show()
    }

    override fun onState(state: JSONObject) {
        UpdateManager.maybeOffer(this, state)
        maybeFetchWeather(state)
        render(state)
    }

    // ── rendering ───────────────────────────────────────────────────────────
    private fun showPlaceholder(msg: String) {
        cards["link"] = Ui.card(this).apply {
            addView(Ui.cardHeader(context, "MOBILE LINK · DISCONNECTED", "link",
                labelColor = Ui.ROSE, leading = Ui.dot(context, Ui.ROSE)))
            addView(Ui.gap(context, 8))
            addView(Ui.value(context, msg, 14f, Ui.DIM))
        }
        signatures.clear()
        adapter.setOrder(listOf("link"))
        adapter.refresh("link")
    }

    /**
     * Rebuild only what changed. `sig` is a cheap fingerprint of the data each
     * card draws; identical fingerprint means the existing view still shows the
     * truth and must be left alone (rebuilding would flicker and drop scroll).
     */
    private fun render(state: JSONObject) {
        if (dragging) { pendingState = state; return }    // never yank cards mid-drag

        val wanted = ArrayList<String>()
        wanted.add("link")
        wanted.addAll(phoneOrder(state))

        for (type in wanted) {
            val sig = signatureFor(type, state)
            if (signatures[type] == sig && cards.containsKey(type)) continue
            val card = buildCard(type, state)
            if (card == null) { cards.remove(type); signatures.remove(type); continue }
            cards[type] = card
            signatures[type] = sig
            adapter.refresh(type)
        }
        // Drop types that produced no card this pass (e.g. no task data yet).
        val visible = wanted.filter { cards.containsKey(it) }
        adapter.setOrder(visible)
    }

    private fun signatureFor(type: String, state: JSONObject): String = when (type) {
        "link" -> "${LinkClient.linkUp}|${state.optString("host")}|" +
                  "${state.optString("bridgeVersion")}|$lastAnnounce|${state.optString("brainError")}"
        "cortana" -> state.optJSONObject("cortana")?.toString() ?: ""
        "music" -> state.optJSONObject("spotify")?.toString() ?: ""
        "agenda" -> state.optJSONObject("calendar")?.toString() ?: ""
        "tasks" -> state.optJSONObject("board")?.optJSONArray("tasks")?.toString() ?: ""
        "git" -> state.optJSONObject("git")?.toString() ?: ""
        "weather" -> weather?.toString() ?: ""
        else -> ""
    }

    private fun buildCard(type: String, state: JSONObject): LinearLayout? = when (type) {
        "link" -> linkCard(state)
        "cortana" -> cortanaCard(state)
        "music" -> musicCard(state)
        "agenda" -> agendaCard(state)
        "tasks" -> tasksCard(state)
        "weather" -> weatherCard()
        "git" -> gitCard(state)
        else -> null
    }

    private fun linkCard(state: JSONObject): LinearLayout = Ui.card(this).apply {
        val up = LinkClient.linkUp
        val host = state.optString("host", Prefs.dashName(context))
        addView(Ui.cardHeader(context,
            if (up) "MOBILE LINK" else "MOBILE LINK · DISCONNECTED", "link",
            labelColor = if (up) Ui.LAVENDER else Ui.ROSE,
            leading = Ui.dot(context, if (up) Ui.GREEN else Ui.ROSE),
            trailing = Ui.value(context, "BRIDGE v${state.optString("bridgeVersion", "?")}",
                11f, Ui.DIM, mono = true)))
        addView(Ui.gap(context, 8))
        if (up) {
            addView(Ui.value(context, "Linked to $host", 15f))
        } else {
            addView(Ui.value(context, "Reconnecting to $host…", 15f, Ui.ROSE))
            addView(Ui.value(context,
                "Check: Tailscale on this phone · workstation awake · bridge running",
                12f, Ui.DIM))
        }
        addView(Ui.value(context,
            "This phone: ${Prefs.deviceName(context).ifEmpty { Prefs.defaultDeviceName() }}",
            13f, Ui.DIM))
        if (lastAnnounce.isNotEmpty()) {
            addView(Ui.gap(context, 6))
            addView(Ui.value(context, "› $lastAnnounce", 13f, Ui.LAVENDER))
        }
        val brainErr = state.optString("brainError")
        if (brainErr.isNotEmpty()) {
            addView(Ui.gap(context, 6))
            addView(Ui.value(context, brainErr, 12f, Ui.ROSE))
        }
        addView(Ui.gap(context, 8))
        addView(Ui.row(context,
            Ui.value(context, "press and hold a card to reorder", 11f, Ui.DIM),
            Ui.helpIcon(context, "reorder")))
    }

    private fun cortanaCard(state: JSONObject): LinearLayout = Ui.card(this).apply {
        val c = state.optJSONObject("cortana") ?: JSONObject()
        val svc = c.optString("service", "unknown")
        val fresh = c.optBoolean("fresh")
        val effState = if (svc != "active" && !fresh) "offline" else c.optString("state", "offline")
        val col = Ui.stateColor(effState)
        addView(Ui.cardHeader(context, "CORTANA · ${effState.uppercase()}", "cortana",
            labelColor = col, leading = Ui.dot(context, col),
            trailing = Ui.value(context,
                if (fresh && svc != "active") "MANUAL RUN" else "SVC ${svc.uppercase()}",
                11f, Ui.DIM, mono = true)))
        val sub = buildString {
            val mode = c.optString("mode")
            if (mode.isNotEmpty())
                append(mapOf("ptt" to "PTT", "wake" to "WAKE", "open" to "CONVO")[mode] ?: mode.uppercase())
            for (extra in listOf(c.optString("agent"), c.optString("detail"))) {
                if (extra.isNotEmpty()) { if (isNotEmpty()) append(" · "); append(extra) }
            }
            if (c.optBoolean("stale") && effState == "offline") {
                if (isNotEmpty()) append(" · ")
                append("state stale")
            }
        }
        if (sub.isNotEmpty()) {
            addView(Ui.gap(context, 6))
            addView(Ui.value(context, sub, 13f, Ui.DIM, mono = true))
        }
        val thoughts = c.optJSONArray("thoughts") ?: JSONArray()
        if (thoughts.length() > 0) addView(Ui.gap(context, 8))
        for (i in 0 until thoughts.length()) {
            addView(Ui.value(context, "· " + thoughts.optString(i), 13f,
                if (i == thoughts.length() - 1) Ui.TEXT else Ui.DIM))
        }
    }

    private fun musicCard(state: JSONObject): LinearLayout = Ui.card(this).apply {
        val sp = state.optJSONObject("spotify") ?: JSONObject()
        addView(Ui.cardHeader(context, "MUSIC", "music",
            trailing = Ui.value(context, sp.optString("device", ""), 11f, Ui.DIM, mono = true)))
        addView(Ui.gap(context, 8))
        if (!sp.optBoolean("configured", false) || !sp.optBoolean("connected", false)) {
            addView(Ui.value(context,
                "Spotify isn't connected - connect it on the dashboard's Music module.",
                13f, Ui.DIM))
            return@apply
        }
        if (sp.has("error")) {
            addView(Ui.value(context,
                "Spotify error: ${sp.opt("error")} ${sp.optString("errorMsg")}", 13f, Ui.ROSE))
            return@apply
        }
        if (!sp.optBoolean("active", false)) {
            addView(Ui.value(context,
                "Nothing playing. Start Spotify on any device - including this phone - " +
                "and control it here.", 13f, Ui.DIM))
        } else {
            val art = ImageView(context).apply {
                layoutParams = LinearLayout.LayoutParams(Ui.dp(context, 54), Ui.dp(context, 54))
                    .apply { rightMargin = Ui.dp(context, 12) }
            }
            loadArt(sp.optString("art"), art)
            val meta = LinearLayout(context).apply {
                orientation = LinearLayout.VERTICAL
                addView(Ui.value(context, sp.optString("track", "—"), 15f))
                addView(Ui.value(context, sp.optString("artist", ""), 13f, Ui.DIM))
            }
            addView(LinearLayout(context).apply {
                gravity = Gravity.CENTER_VERTICAL
                addView(art); addView(meta)
            })
        }
        addView(Ui.gap(context, 10))
        val playing = sp.optBoolean("playing", false)
        addView(Ui.row(context,
            Ui.spacer(context),
            Ui.pillButton(context, "⏮") { spotify("previous") },
            transportGap(),
            Ui.pillButton(context, if (playing) "⏸" else "▶") {
                spotify(if (playing) "pause" else "play")
            },
            transportGap(),
            Ui.pillButton(context, "⏭") { spotify("next") },
            Ui.spacer(context)))
    }

    private fun transportGap() = android.view.View(this).apply {
        layoutParams = LinearLayout.LayoutParams(Ui.dp(context, 14), 1)
    }

    private fun spotify(action: String) {
        thread {
            try {
                val r = LinkClient.spotify(this, action)
                if (!r.optBoolean("ok") && r.has("error"))
                    runOnUiThread {
                        Toast.makeText(this, r.optString("error"), Toast.LENGTH_LONG).show()
                    }
            } catch (e: LinkClient.AuthException) {
                runOnUiThread { onAuthRejected() }
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(this, "Spotify: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun loadArt(url: String, into: ImageView) {
        if (url.isEmpty()) return
        artCache[url]?.let { into.setImageBitmap(it); return }
        thread {
            val bytes = LinkClient.fetchBytes(url) ?: return@thread
            val bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: return@thread
            if (artCache.size > 12) artCache.clear()
            artCache[url] = bmp
            runOnUiThread { into.setImageBitmap(bmp) }
        }
    }

    private fun agendaCard(state: JSONObject): LinearLayout = Ui.card(this).apply {
        val cal = state.optJSONObject("calendar") ?: JSONObject()
        addView(Ui.cardHeader(context, "AGENDA", "agenda"))
        addView(Ui.gap(context, 8))
        val events = cal.optJSONArray("events") ?: JSONArray()
        val err = cal.optString("error")
        when {
            events.length() == 0 && err.isNotEmpty() ->
                addView(Ui.value(context, err, 13f, Ui.DIM))
            events.length() == 0 ->
                addView(Ui.value(context, "nothing on the calendar today", 13f, Ui.DIM))
            else -> for (i in 0 until events.length()) {
                val ev = events.optJSONObject(i) ?: continue
                val past = ev.optBoolean("past")
                val time = if (ev.optBoolean("allDay")) "—" else ev.optString("time")
                addView(Ui.row(context,
                    Ui.value(context, time, 13f, if (past) Ui.DIM else Ui.ACCENT, mono = true)
                        .apply { minWidth = Ui.dp(context, 64) },
                    Ui.value(context, ev.optString("title"), 14f, if (past) Ui.DIM else Ui.TEXT)))
            }
        }
    }

    private fun tasksCard(state: JSONObject): LinearLayout? {
        val tasks = state.optJSONObject("board")?.optJSONArray("tasks") ?: return null
        return Ui.card(this).apply {
            val open = (0 until tasks.length()).count {
                !(tasks.optJSONObject(it)?.optBoolean("done") ?: false)
            }
            addView(Ui.cardHeader(context, "TASKS", "tasks",
                trailing = Ui.value(context, "$open OPEN", 11f, Ui.DIM, mono = true)))
            addView(Ui.gap(context, 8))
            if (tasks.length() == 0)
                addView(Ui.value(context, "no tasks on the board", 13f, Ui.DIM))
            for (i in 0 until tasks.length()) {
                val t = tasks.optJSONObject(i) ?: continue
                val done = t.optBoolean("done")
                addView(Ui.value(context,
                    (if (done) "✓ " else "○ ") + t.optString("text", t.optString("title")),
                    14f, if (done) Ui.DIM else Ui.TEXT))
            }
        }
    }

    private fun gitCard(state: JSONObject): LinearLayout = Ui.card(this).apply {
        val g = state.optJSONObject("git") ?: JSONObject()
        val clean = g.optBoolean("clean", true)
        addView(Ui.cardHeader(context, "GIT · CORTANA", "git",
            leading = Ui.dot(context, if (clean) Ui.GREEN else Ui.ACCENT),
            trailing = Ui.value(context, g.optString("branch", "?"), 12f, Ui.DIM, mono = true)))
        addView(Ui.gap(context, 6))
        addView(Ui.value(context,
            if (clean) "clean" else "${g.optInt("files")} file(s) modified",
            13f, if (clean) Ui.GREEN else Ui.ACCENT, mono = true))
        val log = g.optJSONArray("log") ?: JSONArray()
        if (log.length() > 0) addView(Ui.gap(context, 6))
        for (i in 0 until log.length()) {
            val commit = log.optJSONObject(i) ?: continue
            addView(Ui.row(context,
                Ui.value(context, commit.optString("hash"), 12f, Ui.ACCENT, mono = true)
                    .apply { minWidth = Ui.dp(context, 70) },
                Ui.value(context, commit.optString("msg"), 13f, Ui.DIM).apply {
                    maxLines = 1
                    ellipsize = android.text.TextUtils.TruncateAt.END
                }))
        }
    }

    // ── weather: fetched by the phone (keyless APIs); the ZIP comes from the
    // board snapshot so both screens always show the same place ─────────────
    private fun maybeFetchWeather(state: JSONObject) {
        val zip = state.optJSONObject("board")?.optString("weatherZip", "") ?: ""
        if (!Regex("^\\d{5}$").matches(zip)) return
        val fresh = System.currentTimeMillis() - weatherAt < 15 * 60 * 1000
        if (zip == weatherZip && fresh) return
        weatherZip = zip
        weatherAt = System.currentTimeMillis()
        thread {
            try {
                val geo = JSONObject(String(
                    LinkClient.fetchBytes("https://api.zippopotam.us/us/$zip") ?: return@thread))
                val place = geo.optJSONArray("places")?.optJSONObject(0) ?: return@thread
                val lat = place.optString("latitude")
                val lon = place.optString("longitude")
                val name = "${place.optString("place name")}, ${place.optString("state abbreviation")}"
                val w = JSONObject(String(LinkClient.fetchBytes(
                    "https://api.open-meteo.com/v1/forecast?latitude=$lat&longitude=$lon" +
                    "&current=temperature_2m,weather_code,wind_speed_10m" +
                    "&daily=temperature_2m_max,temperature_2m_min&temperature_unit=fahrenheit" +
                    "&wind_speed_unit=mph&timezone=auto&forecast_days=1") ?: return@thread))
                val cur = w.optJSONObject("current")
                val day = w.optJSONObject("daily")
                weather = JSONObject()
                    .put("place", name.uppercase())
                    .put("temp", Math.round(cur?.optDouble("temperature_2m") ?: 0.0))
                    .put("code", cur?.optInt("weather_code") ?: -1)
                    .put("wind", Math.round(cur?.optDouble("wind_speed_10m") ?: 0.0))
                    .put("hi", Math.round(day?.optJSONArray("temperature_2m_max")?.optDouble(0) ?: 0.0))
                    .put("lo", Math.round(day?.optJSONArray("temperature_2m_min")?.optDouble(0) ?: 0.0))
                runOnUiThread { LinkClient.lastState?.let { render(it) } }
            } catch (e: Exception) { /* keep the last good reading */ }
        }
    }

    private val wmo = mapOf(0 to "clear", 1 to "mostly clear", 2 to "partly cloudy",
        3 to "overcast", 45 to "fog", 48 to "fog", 51 to "drizzle", 53 to "drizzle",
        55 to "drizzle", 61 to "rain", 63 to "rain", 65 to "heavy rain",
        71 to "snow", 73 to "snow", 75 to "heavy snow", 80 to "showers",
        81 to "showers", 82 to "heavy showers", 95 to "thunderstorm",
        96 to "thunderstorm", 99 to "thunderstorm")

    private fun weatherCard(): LinearLayout? {
        val w = weather ?: return null
        return Ui.card(this).apply {
            addView(Ui.cardHeader(context, "WEATHER", "weather",
                trailing = Ui.value(context, w.optString("place"), 11f, Ui.DIM, mono = true)))
            addView(Ui.gap(context, 8))
            addView(Ui.row(context,
                Ui.value(context, "${w.optInt("temp")}°", 30f),
                transportGap(),
                Ui.value(context, wmo[w.optInt("code")] ?: "—", 14f, Ui.DIM)))
            addView(Ui.value(context,
                "H ${w.optInt("hi")}° · L ${w.optInt("lo")}° · wind ${w.optInt("wind")} mph",
                13f, Ui.DIM, mono = true))
        }
    }
}
