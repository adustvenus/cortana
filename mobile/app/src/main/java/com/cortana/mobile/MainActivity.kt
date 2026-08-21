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
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
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
    private lateinit var swipe: SwipeRefreshLayout
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

    // ── Optimistic task/ZIP sync ───────────────────────────────────────────
    // Edits made here show INSTANTLY with a spinner, then resolve when the
    // dashboard applies them and the board snapshot comes back carrying the
    // change (the handshake). On failure the optimistic state is rolled back
    // and a toast says it did not sync.
    private val pendingToggles = HashMap<String, PendingToggle>()  // id -> desired state
    private val pendingRemoves = HashMap<String, Long>()           // id -> queued at
    private val pendingAdds = ArrayList<PendingAdd>()
    private var pendingZip: String? = null

    private data class PendingAdd(val text: String, val at: Long)
    private data class PendingToggle(val want: Boolean, val at: Long)

    /** How long an unacknowledged edit may spin before we call it failed. */
    private val SYNC_TIMEOUT_MS = 45_000L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!Prefs.paired(this)) {
            // Right after an app update the Android Keystore can lag a few
            // seconds, making the stored token unreadable - which is NOT the
            // same as being unpaired. If we know the host but can't open
            // secure storage yet, wait it out instead of bouncing the user to
            // a false "pair this phone" screen.
            if (Prefs.host(this).isNotEmpty() && !Prefs.secureStorageAvailable(this)) {
                setContentView(buildScaffold())
                showPlaceholder("Unlocking secure storage…\n(normal for a few seconds after an update)")
                retryPairedCheck(attempt = 0)
                return
            }
            startActivity(Intent(this, PairActivity::class.java))
            finish()
            return
        }
        setContentView(buildScaffold())
        showPlaceholder("Connecting to ${Prefs.dashName(this).ifEmpty { Prefs.host(this) }}…")
    }

    private fun retryPairedCheck(attempt: Int) {
        recycler.postDelayed({
            when {
                Prefs.paired(this) -> {
                    LinkClient.start(this, this)
                    showPlaceholder("Connecting to ${Prefs.dashName(this).ifEmpty { Prefs.host(this) }}…")
                }
                attempt < 10 -> retryPairedCheck(attempt + 1)
                else -> {   // ~10s of failures = genuinely gone; pair again
                    startActivity(Intent(this, PairActivity::class.java))
                    finish()
                }
            }
        }, 1000)
    }

    private fun buildScaffold(): LinearLayout {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Ui.BG)
        }
        linkDot = TextView(this).apply { text = "●"; textSize = 14f; setTextColor(Ui.DIM) }
        val title = TextView(this).apply {
            text = "CORTANA"
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
        // Pull down to force-refresh: one REST fetch, independent of the
        // WebSocket - useful when a push feels stale or the link just came back.
        swipe = SwipeRefreshLayout(this).apply {
            addView(recycler)
            setColorSchemeColors(Ui.ACCENT, Ui.LAVENDER)
            setProgressBackgroundColorSchemeColor(Ui.CARD)
            setOnRefreshListener { forceRefresh() }
        }
        root.addView(swipe,
            LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))
        return root
    }

    private fun forceRefresh() {
        // Pull-to-refresh means NOW: drop the 15-minute weather throttle too,
        // or the weather card visibly lags behind everything else.
        weatherAt = 0
        thread {
            val fresh = try { LinkClient.fetchState(this) } catch (e: Exception) { null }
            runOnUiThread {
                swipe.isRefreshing = false
                when {
                    fresh != null -> {
                        signatures.clear()          // force every card to redraw
                        onState(fresh)
                    }
                    LinkClient.lastState == null ->
                        Toast.makeText(this, "Can't reach the workstation", Toast.LENGTH_SHORT).show()
                    else ->
                        Toast.makeText(this, "Refresh failed - showing last known state", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    // Settings sits to the RIGHT of the dashboard, so you swipe the page
    // leftward (finger right-to-left) to travel right into it - the standard
    // pager direction. Vertical scrolling wins whenever the motion is more
    // vertical than horizontal, so the list is unaffected.
    private val swipeDetector by lazy {
        android.view.GestureDetector(this,
            object : android.view.GestureDetector.SimpleOnGestureListener() {
                override fun onFling(e1: android.view.MotionEvent?, e2: android.view.MotionEvent,
                                     vx: Float, vy: Float): Boolean {
                    e1 ?: return false
                    val dx = e2.x - e1.x; val dy = e2.y - e1.y
                    if (dx < -220 && Math.abs(dx) > 2 * Math.abs(dy) && vx < -900) {
                        startActivity(Intent(this@MainActivity, SettingsActivity::class.java))
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

    override fun onStart() {
        super.onStart()
        if (Prefs.paired(this)) LinkClient.start(this, this)
    }

    override fun onResume() {
        super.onResume()
        Announcer.onResume("main")
        Announcer.ensureChannel(this)
        // Asked here rather than at first launch: by now the phone is paired,
        // so the prompt arrives with obvious context. Declining costs only the
        // banner - toast and inline still work.
        if (Prefs.paired(this) && !Announcer.canPostBanner(this)) {
            try {
                requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 7)
            } catch (e: Exception) { /* older platform: permission does not exist */ }
        }
    }

    override fun onPause() {
        super.onPause()
        Announcer.onPause("main")
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
        // Presentation is Announcer's job now - it already decided banner vs
        // toast vs inline before this ran. Toasting again here would double up.
        lastAnnounce = text
        signatures.remove("link")
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
        reconcilePending(state)
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

    /** Reconcile optimistic state against what the dashboard sent back. A
     *  toggle whose server value now matches the desired one, or an add whose
     *  text now appears, has completed the handshake and stops showing as
     *  pending. Anything still unresolved after 45s is treated as failed. */
    private fun reconcilePending(state: JSONObject) {
        val tasks = state.optJSONObject("board")?.optJSONArray("tasks") ?: return
        val byId = HashMap<String, Boolean>()
        val texts = HashSet<String>()
        for (i in 0 until tasks.length()) {
            val t = tasks.optJSONObject(i) ?: continue
            byId[t.optString("id")] = t.optBoolean("done")
            texts.add(t.optString("t", t.optString("text")))
        }
        val now = System.currentTimeMillis()
        // Acknowledged: the dashboard's copy now matches what we asked for.
        var changed = pendingToggles.entries.removeAll { (id, p) -> byId[id] == p.want }
        if (pendingRemoves.keys.removeAll { it !in byId.keys }) changed = true
        if (pendingAdds.removeAll { it.text in texts }) changed = true

        // Timed out: nothing came back, so stop spinning and say so. Without
        // this a toggle could spin forever when the dashboard is closed.
        var timedOut = false
        if (pendingToggles.entries.removeAll { now - it.value.at > SYNC_TIMEOUT_MS }) timedOut = true
        if (pendingRemoves.entries.removeAll { now - it.value > SYNC_TIMEOUT_MS }) timedOut = true
        if (pendingAdds.removeAll { now - it.at > SYNC_TIMEOUT_MS }) timedOut = true
        if (timedOut) {
            changed = true
            toastNotSynced("Change")
        }
        val zip = state.optJSONObject("board")?.optString("weatherZip", "")
        if (pendingZip != null && zip == pendingZip) { pendingZip = null; changed = true }
        if (changed) signatures.remove("tasks")
    }

    private fun signatureFor(type: String, state: JSONObject): String = when (type) {
        "link" -> "${LinkClient.linkUp}|${state.optString("host")}|" +
                  "${state.optString("bridgeVersion")}|$lastAnnounce|${state.optString("brainError")}"
        "cortana" -> state.optJSONObject("cortana")?.toString() ?: ""
        "music" -> state.optJSONObject("spotify")?.toString() ?: ""
        "agenda" -> state.optJSONObject("calendar")?.toString() ?: ""
        // Pending state is local, so it is part of these cards' signatures.
        "tasks" -> (state.optJSONObject("board")?.optJSONArray("tasks")?.toString() ?: "none") +
                   "|" + pendingToggles.keys + "|" + pendingRemoves.keys + "|" + pendingAdds.size
        "git" -> state.optJSONObject("git")?.toString() ?: ""
        "weather" -> (weather?.toString() ?: "") + "|" + pendingZip +
                     "|" + Prefs.weatherZip(this)
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
        val toggle = if (playing)
            Ui.iconPill(context, Ui.pauseBars(context)) { spotify("pause") }
        else
            Ui.pillButton(context, "▶") { spotify("play") }
        addView(Ui.row(context,
            Ui.spacer(context),
            Ui.pillButton(context, "⏮") { spotify("previous") },
            transportGap(),
            toggle,
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

    /** Two-way tasks. The dashboard page owns the list; edits here apply
     *  optimistically with a spinner and resolve when the board snapshot comes
     *  back carrying them. The card always renders - even with no snapshot yet -
     *  so you can always add a task. */
    private fun tasksCard(state: JSONObject): LinearLayout {
        val tasks = state.optJSONObject("board")?.optJSONArray("tasks")
        val haveBoard = tasks != null
        return Ui.card(this).apply {
            val open = (0 until (tasks?.length() ?: 0)).count {
                !(tasks!!.optJSONObject(it)?.optBoolean("done") ?: false)
            } + pendingAdds.size
            addView(Ui.cardHeader(context, "TASKS", "tasks",
                trailing = Ui.value(context, if (haveBoard) "$open OPEN" else "…",
                    11f, Ui.DIM, mono = true)))
            addView(Ui.gap(context, 8))
            if (!haveBoard) {
                addView(Ui.value(context,
                    "waiting for the dashboard's task list — add the MOBILE LINK " +
                    "module on the dashboard if this persists", 13f, Ui.DIM))
            } else if (tasks!!.length() == 0 && pendingAdds.isEmpty()) {
                addView(Ui.value(context, "no tasks yet — add one below", 13f, Ui.DIM))
            }
            for (i in 0 until (tasks?.length() ?: 0)) {
                val t = tasks!!.optJSONObject(i) ?: continue
                val id = t.optString("id")
                if (pendingRemoves.containsKey(id)) continue   // optimistically gone
                val serverDone = t.optBoolean("done")
                val pending = pendingToggles[id]
                val done = pending?.want ?: serverDone
                // Dashboard stores task text under "t" (see _addTask in dc.html).
                addView(taskRow(t.optString("t", t.optString("text")), done,
                                syncing = pending != null,
                                onTap = if (id.isNotEmpty()) ({ taskToggle(id, serverDone) }) else null,
                                onRemove = if (id.isNotEmpty()) ({ taskRemove(id) }) else null))
            }
            // Locally-added tasks not yet echoed by the dashboard.
            for (p in pendingAdds)
                addView(taskRow(p.text, false, syncing = true, onTap = null, onRemove = null))

            addView(Ui.gap(context, 8))
            val input = android.widget.EditText(context).apply {
                hint = "add a task…"
                setTextColor(Ui.TEXT)
                setHintTextColor(Ui.DIM)
                textSize = 14f
                maxLines = 1
                inputType = android.text.InputType.TYPE_CLASS_TEXT
                imeOptions = android.view.inputmethod.EditorInfo.IME_ACTION_DONE
            }
            val send = {
                val txt = input.text.toString().trim()
                if (txt.isNotEmpty()) { input.setText(""); taskAdd(txt) }
            }
            input.setOnEditorActionListener { _, _, _ -> send(); true }
            addView(Ui.row(context,
                input.also {
                    it.layoutParams = LinearLayout.LayoutParams(0,
                        LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
                },
                Ui.pillButton(context, "+ ADD") { send() }))
        }
    }

    /** One task row: checkbox, text, and a spinner while the edit is in flight. */
    private fun taskRow(text: String, done: Boolean, syncing: Boolean,
                        onTap: (() -> Unit)?, onRemove: (() -> Unit)?): LinearLayout {
        val box = Ui.value(this, if (done) "☑" else "☐", 17f,
            if (done) Ui.GREEN else Ui.DIM).apply {
            setPadding(0, 0, Ui.dp(context, 10), 0)
        }
        val label = Ui.value(this, text, 14f,
            if (syncing || done) Ui.DIM else Ui.TEXT).apply {
            layoutParams = LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }
        val row = Ui.row(this, box, label)
        if (syncing) {
            // A spinner sits where the ✕ would be, so the row never implies it
            // is idle while an edit is still in flight.
            row.addView(Ui.spinner(this))
        } else if (onRemove != null) {
            row.addView(Ui.value(this, "✕", 14f, Ui.DIM).apply {
                setPadding(Ui.dp(context, 12), Ui.dp(context, 2),
                           Ui.dp(context, 4), Ui.dp(context, 2))
                setOnClickListener { onRemove() }
            })
        }
        row.setPadding(0, Ui.dp(this, 5), 0, Ui.dp(this, 5))
        // A row already syncing ignores taps, so double-tapping can't queue a
        // second contradictory op.
        if (onTap != null && !syncing) row.setOnClickListener { onTap() }
        return row
    }

    private fun taskToggle(id: String, serverDone: Boolean) {
        pendingToggles[id] = PendingToggle(!serverDone, System.currentTimeMillis())
        redrawTasks()
        taskOp(org.json.JSONObject().put("op", "toggle").put("id", id)) { ok ->
            if (!ok) {
                pendingToggles.remove(id)                     // roll back
                redrawTasks()
                toastNotSynced("Checkbox")
            }
        }
    }

    private fun taskRemove(id: String) {
        pendingRemoves[id] = System.currentTimeMillis()       // optimistic
        redrawTasks()
        taskOp(org.json.JSONObject().put("op", "remove").put("id", id)) { ok ->
            if (!ok) {
                pendingRemoves.remove(id)                     // roll back
                redrawTasks()
                toastNotSynced("Removal")
            }
        }
    }

    private fun taskAdd(text: String) {
        val entry = PendingAdd(text, System.currentTimeMillis())
        pendingAdds.add(entry)                                // optimistic
        redrawTasks()
        taskOp(org.json.JSONObject().put("op", "add").put("t", text)) { ok ->
            if (!ok) {
                pendingAdds.remove(entry)                     // roll back
                redrawTasks()
                toastNotSynced("Task")
            }
        }
    }

    private fun toastNotSynced(what: String) {
        Toast.makeText(this, "$what not synced — the change was undone",
            Toast.LENGTH_LONG).show()
    }

    /** Force the tasks card to rebuild (its signature covers server data only,
     *  so local pending state needs an explicit nudge). */
    private fun redrawTasks() {
        signatures.remove("tasks")
        LinkClient.lastState?.let { render(it) }
    }

    private fun taskOp(op: org.json.JSONObject, done: (Boolean) -> Unit) {
        thread {
            val ok = try {
                val r = LinkClient.taskOp(this, op)
                if (!r.optBoolean("dashboardOpen", true)) {
                    runOnUiThread {
                        Toast.makeText(this,
                            "Queued — applies when the dashboard is open",
                            Toast.LENGTH_SHORT).show()
                    }
                }
                r.optBoolean("ok")
            } catch (e: LinkClient.AuthException) {
                runOnUiThread { onAuthRejected() }
                false
            } catch (e: Exception) {
                false
            }
            runOnUiThread { done(ok) }
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
        // The phone's own ZIP wins, so weather works before any board snapshot
        // arrives; otherwise follow the dashboard's.
        val zip = Prefs.weatherZip(this).ifEmpty {
            state.optJSONObject("board")?.optString("weatherZip", "") ?: ""
        }
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

    /** Weather always renders: readings when we have them, a ZIP prompt when we
     *  don't. Changing the ZIP here also syncs it to the dashboard (pending
     *  until the board snapshot echoes it back). */
    private fun weatherCard(): LinearLayout {
        val w = weather
        val zip = Prefs.weatherZip(this).ifEmpty {
            LinkClient.lastState?.optJSONObject("board")?.optString("weatherZip", "") ?: ""
        }
        return Ui.card(this).apply {
            val trailing = if (pendingZip != null)
                Ui.row(context, Ui.value(context, "SYNCING", 11f, Ui.DIM, mono = true),
                       Ui.spinner(context, 13))
            else Ui.value(context, w?.optString("place") ?: "", 11f, Ui.DIM, mono = true)
            addView(Ui.cardHeader(context, "WEATHER", "weather", trailing = trailing))
            addView(Ui.gap(context, 8))
            if (w != null) {
                addView(Ui.row(context,
                    Ui.value(context, "${w.optInt("temp")}°", 30f),
                    transportGap(),
                    Ui.value(context, wmo[w.optInt("code")] ?: "—", 14f, Ui.DIM)))
                addView(Ui.value(context,
                    "H ${w.optInt("hi")}° · L ${w.optInt("lo")}° · wind ${w.optInt("wind")} mph",
                    13f, Ui.DIM, mono = true))
            } else {
                addView(Ui.value(context,
                    if (zip.isEmpty()) "enter a ZIP code below" else "loading $zip…",
                    13f, Ui.DIM))
            }
            addView(Ui.gap(context, 10))
            val input = android.widget.EditText(context).apply {
                hint = if (zip.isEmpty()) "ZIP code" else zip
                setTextColor(Ui.TEXT)
                setHintTextColor(Ui.DIM)
                textSize = 14f
                maxLines = 1
                inputType = android.text.InputType.TYPE_CLASS_NUMBER
                imeOptions = android.view.inputmethod.EditorInfo.IME_ACTION_DONE
            }
            val send = {
                val v = input.text.toString().trim()
                if (Regex("^\\d{5}$").matches(v)) { input.setText(""); setZip(v) }
                else Toast.makeText(context, "Enter a 5-digit ZIP", Toast.LENGTH_SHORT).show()
            }
            input.setOnEditorActionListener { _, _, _ -> send(); true }
            addView(Ui.row(context,
                input.also {
                    it.layoutParams = LinearLayout.LayoutParams(0,
                        LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
                },
                Ui.pillButton(context, "SET") { send() }))
        }
    }

    /** Apply a ZIP locally at once, then push it to the dashboard. The board
     *  echoing it back is the success handshake; failure rolls back. */
    private fun setZip(zip: String) {
        val previous = Prefs.weatherZip(this)
        Prefs.setWeatherZip(this, zip)
        pendingZip = zip
        weather = null
        weatherAt = 0
        signatures.remove("weather")
        LinkClient.lastState?.let { maybeFetchWeather(it); render(it) }
        thread {
            val ok = try {
                LinkClient.taskOp(this, org.json.JSONObject().put("op", "zip").put("zip", zip))
                    .optBoolean("ok")
            } catch (e: Exception) { false }
            runOnUiThread {
                if (!ok) {
                    // The phone keeps its own ZIP (it still works locally), but
                    // say plainly that the dashboard did not get it.
                    pendingZip = null
                    signatures.remove("weather")
                    LinkClient.lastState?.let { render(it) }
                    Toast.makeText(this,
                        "ZIP set on this phone but not synced to the dashboard",
                        Toast.LENGTH_LONG).show()
                    if (previous.isEmpty() && Prefs.weatherZip(this) != zip)
                        Prefs.setWeatherZip(this, previous)
                }
            }
        }
    }
}
