package com.cortana.mobile

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RadialGradient
import android.graphics.Rect
import android.graphics.Shader
import android.graphics.drawable.Drawable
import org.json.JSONObject
import kotlin.math.hypot
import kotlin.math.min

/**
 * The dashboard's palette, on the phone.
 *
 * The colours used to be `const val`s here and hex literals in colors.xml, so
 * the app looked like the dashboard only for as long as nobody changed the
 * dashboard's background. The board now derives its whole scheme from that
 * image (Dashboard/PALETTE.md) and publishes the tokens through the bridge;
 * this reads them out of the state snapshot and caches them, so the phone wears
 * the same palette and draws the same sphere.
 *
 * Defaults below are the dashboard's shipped defaults - what assets/bg-dusk.png
 * extracts to - so a phone that has never seen a snapshot still looks right.
 *
 * NOT covered: the launcher icon. Android resolves that from the manifest at
 * install time and an app cannot repaint its own icon at runtime. Everything
 * else - the in-app spheres, the home-screen widget, every card and label -
 * follows the board within one push.
 */
object Theme {

    // ── roles (ARGB) ────────────────────────────────────────────────────────
    var bg = 0xFF171F29.toInt(); private set
    var surface = 0xFF232B37.toInt(); private set
    var surface2 = 0xFF2F3846.toInt(); private set
    var border = 0xFF3C4655.toInt(); private set
    var panel = 0xFFEAF3FE.toInt(); private set
    var hairline = 0xFFF1F5FC.toInt(); private set
    var text = 0xFFF4F9FF.toInt(); private set
    var textDim = 0xFFAEB5BE.toInt(); private set
    var accent = 0xFFE1AA84.toInt(); private set
    var accent2 = 0xFFE0A2AD.toInt(); private set
    var accent3 = 0xFFA5C6F5.toInt(); private set
    var peach = 0xFFFAD7B5.toInt(); private set
    var orbHi = 0xFFFDE3C8.toInt(); private set
    var orbMid = 0xFFD38E9B.toInt(); private set
    var orb = 0xFF506581.toInt(); private set

    /** Semantic, deliberately NOT themed: green means healthy on every
     *  palette, and a red failure must not soften into the board's accent. */
    const val GREEN = 0xFF9BE8B8.toInt()
    const val RED = 0xFFF0505A.toInt()

    // Last adopted token blob, verbatim, so an unchanged push is cheap.
    private var raw = ""

    private val TOKENS = listOf(
        "--bg-rgb", "--surface-rgb", "--surface2-rgb", "--border-rgb",
        "--panel-rgb", "--hairline-rgb", "--text-rgb", "--text-dim-rgb",
        "--accent-rgb", "--accent2-rgb", "--accent3-rgb", "--peach-rgb",
        "--orb-hi-rgb", "--orb-mid-rgb", "--orb-rgb"
    )

    // ── loading ─────────────────────────────────────────────────────────────
    /** Restore the cached palette. Call before inflating anything, or the first
     *  frame is drawn in the defaults and then visibly jumps. */
    fun load(ctx: Context) {
        val cached = Prefs.themeTokens(ctx)
        if (cached.isNotEmpty()) {
            try {
                adopt(JSONObject(cached))
                raw = cached
            } catch (e: Exception) { /* unparseable cache: keep the defaults */ }
        }
    }

    /**
     * Take the palette out of a state snapshot. Returns true when something
     * actually changed, so the caller can rebuild views instead of doing it on
     * every push (state arrives every ~1.5s).
     */
    fun updateFrom(ctx: Context, state: JSONObject): Boolean {
        val obj = state.optJSONObject("theme") ?: return false
        val incoming = obj.toString()
        if (incoming == raw) return false
        if (!adopt(obj)) return false
        raw = incoming
        Prefs.setThemeTokens(ctx, incoming)
        return true
    }

    /** Parse "r,g,b" tokens into ARGB. A token that is absent or malformed
     *  leaves its role alone rather than blanking it - a half-delivered palette
     *  should degrade, not produce an invisible UI on a device with no console. */
    private fun adopt(obj: JSONObject): Boolean {
        var any = false
        for (name in TOKENS) {
            val c = parse(obj.optString(name, "")) ?: continue
            any = true
            when (name) {
                "--bg-rgb" -> bg = c
                "--surface-rgb" -> surface = c
                "--surface2-rgb" -> surface2 = c
                "--border-rgb" -> border = c
                "--panel-rgb" -> panel = c
                "--hairline-rgb" -> hairline = c
                "--text-rgb" -> text = c
                "--text-dim-rgb" -> textDim = c
                "--accent-rgb" -> accent = c
                "--accent2-rgb" -> accent2 = c
                "--accent3-rgb" -> accent3 = c
                "--peach-rgb" -> peach = c
                "--orb-hi-rgb" -> orbHi = c
                "--orb-mid-rgb" -> orbMid = c
                "--orb-rgb" -> orb = c
            }
        }
        return any
    }

    private fun parse(v: String): Int? {
        if (v.isEmpty()) return null
        val parts = v.split(",")
        if (parts.size != 3) return null
        val nums = IntArray(3)
        for (i in 0..2) {
            val n = parts[i].trim().toIntOrNull() ?: return null
            if (n < 0 || n > 255) return null
            nums[i] = n
        }
        return Color.rgb(nums[0], nums[1], nums[2])
    }

    // ── derived helpers ─────────────────────────────────────────────────────
    /** The card fill. The dashboard paints modules as translucent glass over a
     *  blurred wallpaper; the phone has no wallpaper behind its cards, so the
     *  same role is drawn opaque here rather than faking a blur that would just
     *  read as murk. */
    val card: Int get() = surface

    fun alpha(color: Int, a: Float): Int =
        Color.argb((a * 255).toInt().coerceIn(0, 255),
            Color.red(color), Color.green(color), Color.blue(color))

    /** True when the board is running light, so callers can pick a matching
     *  system bar treatment instead of guessing. */
    val isLight: Boolean
        get() = (0.2126 * Color.red(bg) + 0.7152 * Color.green(bg) +
                 0.0722 * Color.blue(bg)) > 140

    // ── the sphere ──────────────────────────────────────────────────────────
    /**
     * Cortana's orb, drawn from the same three stops the dashboard and the
     * minimized bubble use, so all three spheres are one object.
     *
     * This replaces res/drawable/sphere.xml, whose colours were hex literals.
     * It is a real Drawable rather than a GradientDrawable because the board
     * paints `radial-gradient(circle at 35% 30%, hi, mid 55%, deep)`, and
     * GradientDrawable cannot place an off-centre origin with an explicit
     * middle stop - it would spread the three colours evenly and the sphere
     * would stop looking lit.
     */
    class SphereDrawable(
        private val hi: Int, private val mid: Int, private val deep: Int,
        private val rim: Int
    ) : Drawable() {

        private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
        private val stroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            color = rim
        }
        private var built = Rect()

        override fun draw(canvas: Canvas) {
            val b = bounds
            if (b.width() <= 0 || b.height() <= 0) return
            if (b != built) {
                val cx = b.left + b.width() * 0.35f
                val cy = b.top + b.height() * 0.30f
                // CSS's default final stop is the farthest CORNER, not the
                // edge; using the radius instead makes the core swallow most
                // of the face and the highlight all but disappears.
                val r = hypot(
                    maxOf(cx - b.left, b.right - cx),
                    maxOf(cy - b.top, b.bottom - cy)
                )
                fill.shader = RadialGradient(
                    cx, cy, maxOf(r, 1f),
                    intArrayOf(hi, mid, deep),
                    floatArrayOf(0f, 0.55f, 1f),
                    Shader.TileMode.CLAMP
                )
                stroke.strokeWidth = maxOf(1f, min(b.width(), b.height()) * 0.012f)
                built = Rect(b)
            }
            val radius = min(b.width(), b.height()) / 2f
            val ox = b.exactCenterX()
            val oy = b.exactCenterY()
            canvas.drawCircle(ox, oy, radius, fill)
            canvas.drawCircle(ox, oy, radius - stroke.strokeWidth / 2f, stroke)
        }

        override fun setAlpha(a: Int) { fill.alpha = a; stroke.alpha = a }
        override fun setColorFilter(cf: android.graphics.ColorFilter?) {
            fill.colorFilter = cf; stroke.colorFilter = cf
        }
        @Suppress("OVERRIDE_DEPRECATION")
        override fun getOpacity(): Int = android.graphics.PixelFormat.TRANSLUCENT
    }

    fun sphere(): Drawable = SphereDrawable(orbHi, orbMid, orb, alpha(accent, 0.49f))

    /** The same sphere as a bitmap, for RemoteViews - a home-screen widget
     *  cannot be handed a Drawable. */
    fun sphereBitmap(sizePx: Int): Bitmap {
        val s = sizePx.coerceIn(24, 512)
        val bmp = Bitmap.createBitmap(s, s, Bitmap.Config.ARGB_8888)
        val d = sphere()
        d.setBounds(0, 0, s, s)
        d.draw(Canvas(bmp))
        return bmp
    }

    /** Offline is grey in every palette: a dead assistant must not look like a
     *  healthy one wearing today's colours. Mirrors the bubble orb exactly. */
    fun offlineSphere(): Drawable = SphereDrawable(
        0xFF8A8A96.toInt(), 0xFF565664.toInt(), 0xFF33333E.toInt(), 0x40FFFFFF.toInt())

    fun stateColor(state: String): Int = when (state) {
        "listening" -> GREEN
        "thinking", "working" -> accent3
        "speaking" -> accent2
        "idle" -> textDim
        else -> 0xFF6E6E7D.toInt()   // offline
    }
}
