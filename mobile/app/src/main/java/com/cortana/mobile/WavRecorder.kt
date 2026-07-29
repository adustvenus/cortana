package com.cortana.mobile

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Records 16 kHz mono 16-bit PCM into a WAV file - exactly what the bridge's
 * Whisper STT expects (same format as the desk mic in voice/mic.py).
 * VOICE_RECOGNITION source: the platform applies AGC/noise suppression tuned
 * for speech, which keeps quiet phone audio above the bridge's silence gate.
 */
class WavRecorder(private val outFile: File) {
    companion object { const val SAMPLE_RATE = 16000 }

    private var record: AudioRecord? = null
    private var thread: Thread? = null
    @Volatile private var running = false

    /** RMS of the latest buffer, 0..1-ish - drives the talk screen's pulse. */
    @Volatile var level = 0f
        private set

    @SuppressLint("MissingPermission")   // caller checks RECORD_AUDIO first
    fun start() {
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION,
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
            maxOf(minBuf * 2, 8192))
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            throw IllegalStateException("microphone unavailable")
        }
        record = rec
        running = true
        rec.startRecording()
        thread = Thread {
            val buf = ShortArray(2048)
            outFile.outputStream().use { out ->
                out.write(wavHeader(0))          // placeholder, patched on stop
                while (running) {
                    val n = rec.read(buf, 0, buf.size)
                    if (n <= 0) continue
                    var sum = 0.0
                    val bytes = ByteBuffer.allocate(n * 2).order(ByteOrder.LITTLE_ENDIAN)
                    for (i in 0 until n) {
                        bytes.putShort(buf[i])
                        sum += buf[i].toDouble() * buf[i]
                    }
                    level = (Math.sqrt(sum / n) / 6000.0).toFloat().coerceIn(0f, 1f)
                    out.write(bytes.array())
                }
            }
        }.apply { name = "wav-recorder"; start() }
    }

    /** Stops and finalizes the WAV. Returns the file, or null if nothing was captured. */
    fun stop(): File? {
        running = false
        thread?.join(2000)
        record?.let { try { it.stop() } catch (e: Exception) {}; it.release() }
        record = null
        val dataLen = outFile.length() - 44
        if (dataLen <= 0) { outFile.delete(); return null }
        RandomAccessFile(outFile, "rw").use { f ->
            f.seek(0)
            f.write(wavHeader(dataLen.toInt()))
        }
        return outFile
    }

    private fun wavHeader(dataLen: Int): ByteArray {
        val byteRate = SAMPLE_RATE * 2
        return ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN).apply {
            put("RIFF".toByteArray()); putInt(36 + dataLen)
            put("WAVE".toByteArray()); put("fmt ".toByteArray())
            putInt(16); putShort(1); putShort(1)
            putInt(SAMPLE_RATE); putInt(byteRate)
            putShort(2); putShort(16)
            put("data".toByteArray()); putInt(dataLen)
        }.array()
    }
}
