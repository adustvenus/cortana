package com.cortana.mobile

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.os.Bundle
import android.speech.tts.TextToSpeech
import android.view.Gravity
import android.view.animation.AlphaAnimation
import android.view.animation.Animation
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.io.File
import java.util.Locale
import kotlin.concurrent.thread

/**
 * Talking to Cortana from the phone. Tap the sphere to start recording, tap
 * again to send. The audio goes to the bridge, which runs Cortana's real
 * STT -> orchestrator -> reply, then her actual ElevenLabs voice streams back
 * and plays here. If the voice pipeline is down (or "phone voice" is disabled
 * in settings), the reply text is spoken with Android's TTS instead - the
 * conversation never dead-ends. A typed input is also available for quiet
 * environments. Tapping the sphere while a reply is playing stops playback.
 */
class TalkActivity : Activity(), TextToSpeech.OnInitListener {

    private lateinit var sphere: ImageView
    private lateinit var stateTxt: TextView
    private lateinit var transcript: TextView
    private lateinit var replyTxt: TextView
    private lateinit var typedIn: EditText

    private var recorder: WavRecorder? = null
    private var player: MediaPlayer? = null
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    private var busy = false
    private var pulse: Animation? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!Prefs.paired(this)) {
            startActivity(Intent(this, PairActivity::class.java))
            finish()
            return
        }
        tts = TextToSpeech(this, this)

        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setBackgroundColor(Ui.BG)
            setPadding(Ui.dp(context, 20), Ui.dp(context, 26), Ui.dp(context, 20), Ui.dp(context, 20))
        }
        val back = TextView(this).apply {
            text = "‹"
            textSize = 30f
            setTextColor(Ui.DIM)
            setPadding(Ui.dp(context, 4), 0, Ui.dp(context, 18), Ui.dp(context, 4))
            contentDescription = "Back to the dashboard"
            setOnClickListener { finish() }
        }
        val header = TextView(this).apply {
            text = "CORTANA · ${Prefs.dashName(context).ifEmpty { Prefs.host(context) }}"
            typeface = Typeface.MONOSPACE
            textSize = 12f
            letterSpacing = 0.2f
            setTextColor(Ui.LAVENDER)
        }
        col.addView(Ui.row(this, back, header, Ui.helpIcon(this, "talk"), Ui.spacer(this)).apply {
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
        })
        col.addView(Ui.gap(this, 14))

        sphere = ImageView(this).apply {
            setImageResource(R.drawable.sphere)
            layoutParams = LinearLayout.LayoutParams(Ui.dp(context, 190), Ui.dp(context, 190))
            contentDescription = "Tap to talk"
            setOnClickListener { onSphereTap() }
        }
        col.addView(sphere)
        col.addView(Ui.gap(this, 16))

        stateTxt = TextView(this).apply {
            text = "tap the sphere to talk"
            typeface = Typeface.MONOSPACE
            textSize = 13f
            letterSpacing = 0.14f
            setTextColor(Ui.DIM)
        }
        col.addView(stateTxt)
        col.addView(Ui.gap(this, 20))

        transcript = Ui.value(this, "", 15f, Ui.TEXT)
        replyTxt = Ui.value(this, "", 15f, Ui.LAVENDER)
        val feed = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(transcript)
            addView(Ui.gap(context, 10))
            addView(replyTxt)
        }
        col.addView(ScrollView(this).apply { addView(feed) },
            LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))

        typedIn = EditText(this).apply {
            hint = "or type to Cortana…"
            setTextColor(Ui.TEXT)
            setHintTextColor(Ui.DIM)
            textSize = 14f
            maxLines = 3
            imeOptions = android.view.inputmethod.EditorInfo.IME_ACTION_SEND
            inputType = android.text.InputType.TYPE_CLASS_TEXT
            setOnEditorActionListener { _, _, _ ->
                val t = text.toString().trim()
                if (t.isNotEmpty() && !busy) { setText(""); sendText(t) }
                true
            }
        }
        col.addView(typedIn, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))

        setContentView(col)

        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1)
    }

    override fun onInit(status: Int) {
        ttsReady = status == TextToSpeech.SUCCESS
        if (ttsReady) tts?.language = Locale.US
    }

    private fun onSphereTap() {
        // Playing a reply? Tap = stop it (barge-in).
        player?.let { p ->
            if (p.isPlaying) { p.stop(); p.release(); player = null; setIdle(); return }
        }
        if (busy) { Toast.makeText(this, "Working on the last one…", Toast.LENGTH_SHORT).show(); return }
        if (recorder == null) startRecording() else stopAndSend()
    }

    private fun startRecording() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1)
            return
        }
        try {
            val rec = WavRecorder(File(cacheDir, "utterance.wav"))
            rec.start()
            recorder = rec
            stateTxt.text = "listening… tap to send"
            stateTxt.setTextColor(Ui.GREEN)
            pulse = AlphaAnimation(1f, 0.55f).apply {
                duration = 550
                repeatMode = Animation.REVERSE
                repeatCount = Animation.INFINITE
            }
            sphere.startAnimation(pulse)
        } catch (e: Exception) {
            Toast.makeText(this, "Mic error: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    private fun stopAndSend() {
        val rec = recorder ?: return
        recorder = null
        sphere.clearAnimation()
        val wav = rec.stop()
        if (wav == null || wav.length() < 8000) {   // < ~0.25s of audio
            setIdle("didn't catch that - tap and speak")
            return
        }
        runTurn(wav, null)
    }

    private fun sendText(text: String) {
        transcript.text = "YOU: $text"
        runTurn(null, text)
    }

    private fun runTurn(wav: File?, text: String?) {
        busy = true
        stateTxt.text = "thinking…"
        stateTxt.setTextColor(Ui.LAVENDER)
        replyTxt.text = ""
        thread {
            try {
                val r = LinkClient.converse(this, wav, text)
                val heard = r.optString("transcript", text ?: "")
                val reply = r.optString("reply")
                val err = r.optString("error")
                runOnUiThread {
                    if (heard.isNotEmpty()) transcript.text = "YOU: $heard"
                    when {
                        r.optBoolean("canceled") -> setIdle("superseded by a newer request")
                        err.isNotEmpty() -> { replyTxt.text = err; setIdle() }
                        reply.isEmpty() -> setIdle("no reply")
                        else -> { replyTxt.text = "CORTANA: $reply"; speak(reply) }
                    }
                }
            } catch (e: LinkClient.AuthException) {
                runOnUiThread {
                    replyTxt.text = "CORTANA: This phone's access was revoked on " +
                        "the dashboard - re-pair me from Settings and we're back."
                    setIdle("access revoked")
                }
            } catch (e: Exception) {
                // Link failure answered in Cortana's voice (on screen), with the
                // real error attached so it's debuggable - not a bare toast.
                runOnUiThread {
                    replyTxt.text = "CORTANA: I can't reach the workstation right " +
                        "now - this phone probably isn't on its network. Make sure " +
                        "Tailscale is connected (or you're on the home Wi-Fi), then " +
                        "try me again.\n\n[link error: ${e.message}]"
                    setIdle("link down - tap to retry when connected")
                }
            } finally {
                busy = false
                wav?.delete()
            }
        }
    }

    private fun speak(text: String) {
        stateTxt.text = "speaking…"
        stateTxt.setTextColor(0xFFAAC8FF.toInt())
        thread {
            val audio = if (Prefs.localTtsOnly(this)) null else try {
                LinkClient.tts(this, text)
            } catch (e: Exception) { null }
            runOnUiThread {
                if (audio != null && audio.isNotEmpty()) playBytes(audio)
                else if (ttsReady) {
                    tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "cortana")
                    setIdle()
                } else setIdle()
            }
        }
    }

    private fun playBytes(audio: ByteArray) {
        try {
            val f = File(cacheDir, "reply.mp3")
            f.writeBytes(audio)
            player?.release()
            player = MediaPlayer().apply {
                setAudioAttributes(AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ASSISTANT)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                setDataSource(f.absolutePath)
                setOnCompletionListener { setIdle(); it.release(); player = null }
                setOnErrorListener { mp, _, _ -> setIdle(); mp.release(); player = null; true }
                prepare()
                start()
            }
        } catch (e: Exception) {
            if (ttsReady) tts?.speak(replyTxt.text.toString().removePrefix("CORTANA: "),
                TextToSpeech.QUEUE_FLUSH, null, "cortana")
            setIdle()
        }
    }

    private fun setIdle(msg: String = "tap the sphere to talk") {
        stateTxt.text = msg
        stateTxt.setTextColor(Ui.DIM)
        sphere.clearAnimation()
    }

    override fun onStart() {
        super.onStart()
        // Hold the link open without taking over as listener: MainActivity is
        // stopped while this screen is up, and without this the socket would
        // be torn down exactly when the user is talking to her.
        if (Prefs.paired(this)) LinkClient.retain(this)
    }

    override fun onResume() {
        super.onResume()
        Announcer.onResume(Announcer.SCREEN_TALK)
        // On the AI screen a completion belongs in the conversation, not in a
        // toast over the top of it.
        Announcer.inlineSink = { text -> runOnUiThread { replyTxt.text = "CORTANA: $text" } }
    }

    override fun onPause() {
        super.onPause()
        Announcer.onPause(Announcer.SCREEN_TALK)
        Announcer.inlineSink = null
    }

    override fun onStop() {
        super.onStop()
        LinkClient.stop()
        recorder?.stop()?.also { it.delete() }
        recorder = null
        sphere.clearAnimation()
    }

    override fun onDestroy() {
        super.onDestroy()
        player?.release()
        tts?.shutdown()
    }
}
