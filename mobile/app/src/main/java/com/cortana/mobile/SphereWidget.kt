package com.cortana.mobile

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews

/**
 * 2x2 home-screen sphere: one tap opens the talk screen. If the phone isn't
 * paired yet, TalkActivity redirects to pairing, so the widget is always safe
 * to tap.
 *
 * The sphere is drawn from the dashboard's current palette rather than loaded
 * from a static drawable, so it tracks the board like the in-app spheres do.
 * A widget lives in the launcher's process and can only be handed a Bitmap,
 * never a Drawable - hence Theme.sphereBitmap().
 *
 * This is also as close as the launcher gets to a themed icon: Android
 * resolves the real launcher icon from the manifest at install time and gives
 * an app no way to repaint it at runtime.
 */
class SphereWidget : AppWidgetProvider() {

    override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) {
        Theme.load(context)
        for (id in ids) {
            val views = RemoteViews(context.packageName, R.layout.widget_sphere)
            // Fixed render size: the widget can be resized and options may be
            // unset on first placement, so a generous square is scaled down by
            // the ImageView rather than risking a 0-size bitmap.
            views.setImageViewBitmap(R.id.widget_sphere, Theme.sphereBitmap(288))
            val intent = Intent(context, TalkActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            views.setOnClickPendingIntent(R.id.widget_root,
                PendingIntent.getActivity(context, 0, intent,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE))
            manager.updateAppWidget(id, views)
        }
    }

    companion object {
        /** Repaint every placed widget. Called when a new palette arrives -
         *  without it the home-screen sphere keeps the old colours until
         *  Android next happens to update the widget, which can be hours. */
        fun refresh(context: Context) {
            try {
                val manager = AppWidgetManager.getInstance(context) ?: return
                val ids = manager.getAppWidgetIds(
                    ComponentName(context.applicationContext, SphereWidget::class.java))
                if (ids == null || ids.isEmpty()) return
                SphereWidget().onUpdate(context.applicationContext, manager, ids)
            } catch (e: Exception) {
                // No widget placed, or the launcher refused - never worth
                // taking the app down over.
            }
        }
    }
}
