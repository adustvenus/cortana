package com.cortana.mobile

import android.Manifest
import android.content.Context
import android.net.Uri
import android.provider.ContactsContract

/**
 * Names, in both directions.
 *
 * Without this the comms hub only ever spoke in phone numbers: "who texted me"
 * answered with a bare number, and sending required dictating one digit at a
 * time. Nobody thinks of the person in their life as +15550100.
 *
 * Both lookups go through the CONTENT_FILTER_URI variants rather than a LIKE on
 * the raw column, because the provider does the matching the platform's own
 * dialer does. For numbers that means carrier normalisation - "+1 555 0100",
 * "5550100" and "(555) 0100" all resolve to one contact, which no string
 * comparison would manage. For names it means token matching, so "Little Demon"
 * finds "Little Demon from 528" without the user reciting the whole label.
 *
 * READ_CONTACTS is optional throughout. Without it every function here degrades
 * to "no name known", which is exactly the behaviour before this file existed -
 * a missing permission must never turn into a crash or a wrong recipient.
 */
object Contacts {

    /** True when a string is a dialable number rather than somebody's name. */
    fun looksLikeNumber(s: String): Boolean {
        val t = s.trim()
        if (t.isEmpty()) return false
        // At least three digits, and nothing that is not plausibly part of a
        // dialable string. Deliberately strict: misreading a NAME as a number
        // sends a message into the void, so anything doubtful is treated as a
        // name and goes through the contact lookup, which can fail loudly.
        var digits = 0
        for (ch in t) {
            when {
                ch.isDigit() -> digits++
                ch == '+' || ch == '-' || ch == ' ' || ch == '(' || ch == ')' || ch == '.' -> {}
                else -> return false
            }
        }
        return digits >= 3
    }

    /** Display name saved for [number], or null when unknown or not permitted. */
    fun nameFor(ctx: Context, number: String): String? {
        if (number.isBlank() || !granted(ctx)) return null
        return try {
            val uri = Uri.withAppendedPath(
                ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(number))
            ctx.contentResolver.query(
                uri, arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME),
                null, null, null)?.use { c ->
                if (c.moveToFirst()) c.getString(0)?.takeIf { it.isNotBlank() } else null
            }
        } catch (e: Exception) {
            // A locked or absent contacts provider is a "no name", never a crash.
            null
        }
    }

    /** (name, number) pairs whose contact name matches [q]. Possibly empty. */
    fun numbersFor(ctx: Context, q: String): List<Pair<String, String>> {
        val out = ArrayList<Pair<String, String>>()
        if (q.isBlank() || !granted(ctx)) return out
        try {
            val uri = Uri.withAppendedPath(
                ContactsContract.CommonDataKinds.Phone.CONTENT_FILTER_URI, Uri.encode(q))
            val cols = arrayOf(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
                               ContactsContract.CommonDataKinds.Phone.NUMBER)
            ctx.contentResolver.query(uri, cols, null, null, null)?.use { c ->
                val seen = HashSet<String>()
                while (c.moveToNext() && out.size < 8) {
                    val name = c.getString(0) ?: continue
                    val num = c.getString(1) ?: continue
                    // One contact with a mobile and a home number saved the same
                    // way is ONE choice, not two - asking "which of these two
                    // identical numbers" would be a worse answer than guessing.
                    val key = num.filter { it.isDigit() }.takeLast(9)
                    if (key.isEmpty() || !seen.add(key)) continue
                    out.add(Pair(name, num))
                }
            }
        } catch (e: Exception) {
            return out
        }
        return out
    }

    private fun granted(ctx: Context) =
        Comms.has(ctx, Manifest.permission.READ_CONTACTS)
}
