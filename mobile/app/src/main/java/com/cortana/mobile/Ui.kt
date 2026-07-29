package com.cortana.mobile

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.View
import android.widget.LinearLayout
import android.widget.TextView

/** Tiny programmatic-view kit that mimics the Dusk dashboard's card look:
 *  rounded dark glass cards, mono labels with letter-spacing, warm text. */
object Ui {
    const val BG = 0xFF221D33.toInt()
    const val CARD = 0xE62C2542.toInt()
    const val TEXT = 0xFFFDF3EC.toInt()
    const val DIM = 0xFF9B93A8.toInt()
    const val ACCENT = 0xFFFFAB8F.toInt()
    const val ROSE = 0xFFF08A9B.toInt()
    const val LAVENDER = 0xFFC9B8E8.toInt()
    const val GREEN = 0xFF9BE8B8.toInt()

    fun dp(ctx: Context, v: Int): Int =
        (v * ctx.resources.displayMetrics.density).toInt()

    fun card(ctx: Context): LinearLayout = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL
        val p = dp(ctx, 16)
        setPadding(p, p, p, p)
        background = GradientDrawable().apply {
            cornerRadius = dp(ctx, 14).toFloat()
            setColor(CARD)
        }
        layoutParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT
        ).apply { setMargins(dp(ctx, 14), dp(ctx, 6), dp(ctx, 14), dp(ctx, 6)) }
    }

    /** Section label: mono, spaced, lavender - the dashboard's house style. */
    fun label(ctx: Context, text: String, color: Int = LAVENDER): TextView =
        TextView(ctx).apply {
            this.text = text
            typeface = Typeface.MONOSPACE
            textSize = 11f
            letterSpacing = 0.18f
            setTextColor(color)
        }

    fun value(ctx: Context, text: String, size: Float = 15f, color: Int = TEXT,
              mono: Boolean = false): TextView = TextView(ctx).apply {
        this.text = text
        textSize = size
        setTextColor(color)
        if (mono) typeface = Typeface.MONOSPACE
    }

    fun row(ctx: Context, vararg children: View): LinearLayout =
        LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            children.forEach { addView(it) }
        }

    fun spacer(ctx: Context): View = View(ctx).apply {
        layoutParams = LinearLayout.LayoutParams(0, 1, 1f)
    }

    fun dot(ctx: Context, color: Int, sizeDp: Int = 9): View = View(ctx).apply {
        background = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(color)
        }
        layoutParams = LinearLayout.LayoutParams(dp(ctx, sizeDp), dp(ctx, sizeDp))
            .apply { rightMargin = dp(ctx, 8) }
    }

    fun gap(ctx: Context, h: Int): View = View(ctx).apply {
        layoutParams = LinearLayout.LayoutParams(1, dp(ctx, h))
    }

    fun pillButton(ctx: Context, text: String, color: Int = ACCENT,
                   onClick: () -> Unit): TextView = TextView(ctx).apply {
        this.text = text
        typeface = Typeface.MONOSPACE
        textSize = 12f
        letterSpacing = 0.12f
        setTextColor(color)
        gravity = Gravity.CENTER
        val ph = dp(ctx, 14); val pv = dp(ctx, 8)
        setPadding(ph, pv, ph, pv)
        background = GradientDrawable().apply {
            cornerRadius = dp(ctx, 10).toFloat()
            setStroke(dp(ctx, 1), (color and 0x00FFFFFF) or 0x80000000.toInt())
            setColor(Color.TRANSPARENT)
        }
        setOnClickListener { onClick() }
    }

    /** The "?" affordance: tap for the Help entry explaining this surface.
     *  Deliberately low-contrast - present when wanted, quiet when not. */
    fun helpIcon(ctx: Context, topic: String): TextView = TextView(ctx).apply {
        text = "?"
        typeface = Typeface.MONOSPACE
        textSize = 12f
        setTextColor(DIM)
        gravity = Gravity.CENTER
        val s = dp(ctx, 22)
        layoutParams = LinearLayout.LayoutParams(s, s).apply { leftMargin = dp(ctx, 6) }
        background = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setStroke(dp(ctx, 1), DIM and 0x60FFFFFF.toInt())
        }
        contentDescription = "Help"
        setOnClickListener { Help.show(ctx, topic) }
    }

    /** Standard card header: label on the left, "?" and optional right-hand
     *  status on the right. Keeps every module visually identical. */
    fun cardHeader(ctx: Context, label: String, topic: String,
                   labelColor: Int = LAVENDER, leading: View? = null,
                   trailing: View? = null): LinearLayout {
        val row = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        leading?.let { row.addView(it) }
        row.addView(label(ctx, label, labelColor))
        row.addView(helpIcon(ctx, topic))
        row.addView(spacer(ctx))
        trailing?.let { row.addView(it) }
        return row
    }

    fun stateColor(state: String): Int = when (state) {
        "listening" -> GREEN
        "thinking", "working" -> LAVENDER
        "speaking" -> 0xFFAAC8FF.toInt()
        "idle" -> DIM
        else -> 0xFF6E6E7D.toInt()   // offline
    }
}
