package com.crosscopy.crosscopy

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import androidx.core.content.FileProvider
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.ByteArrayOutputStream
import java.io.File

/**
 * Image half of the clipboard, exposed to Dart as `crosscopy/clipboard_image`.
 *
 * Android can only put an image on the clipboard as a content:// URI, so writes
 * go through a [FileProvider] backed by the app cache. Every image is normalised
 * to PNG so the desktop and the phone agree on a digest.
 */
class MainActivity : FlutterActivity() {
    private var cachedSignature: String? = null
    private var cachedPng: ByteArray? = null
    private var cachedWidth = 0
    private var cachedHeight = 0

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL)
            .setMethodCallHandler { call, result ->
                try {
                    when (call.method) {
                        "read" -> result.success(readImage())
                        "write" -> {
                            val bytes = call.argument<ByteArray>("bytes")
                            if (bytes == null || bytes.isEmpty()) {
                                result.error("bad_args", "missing image bytes", null)
                            } else {
                                writeImage(bytes)
                                result.success(null)
                            }
                        }
                        else -> result.notImplemented()
                    }
                } catch (error: Exception) {
                    result.error("clipboard_error", error.message ?: "clipboard error", null)
                }
            }
    }

    private fun clipboard(): ClipboardManager =
        getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager

    private fun readImage(): Map<String, Any>? {
        val clipboard = clipboard()
        val clip = clipboard.primaryClip ?: return null
        if (clip.itemCount == 0) return null

        val item = clip.getItemAt(0)
        val uri = item.uri ?: return null
        val mime = clip.description?.let { description ->
            if (description.mimeTypeCount > 0) description.getMimeType(0) else ""
        } ?: ""
        if (!mime.startsWith("image/")) return null

        // Decoding a full screenshot every 600 ms would be wasteful, so cache by
        // URI + size and only re-decode when the clipboard really changed.
        val signature = "$mime|$uri|${contentSize(uri)}"
        if (signature == cachedSignature) {
            val png = cachedPng ?: return null
            return mapOf("bytes" to png, "width" to cachedWidth, "height" to cachedHeight)
        }

        val raw = contentResolver.openInputStream(uri)?.use { it.readBytes() } ?: return null
        val bitmap = BitmapFactory.decodeByteArray(raw, 0, raw.size) ?: return null
        val png = ByteArrayOutputStream().use { output ->
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)
            output.toByteArray()
        }

        cachedSignature = signature
        cachedPng = png
        cachedWidth = bitmap.width
        cachedHeight = bitmap.height
        return mapOf("bytes" to png, "width" to bitmap.width, "height" to bitmap.height)
    }

    private fun contentSize(uri: Uri): Long = try {
        contentResolver.openAssetFileDescriptor(uri, "r")?.use { it.length } ?: -1L
    } catch (_: Exception) {
        -1L
    }

    private fun writeImage(bytes: ByteArray) {
        val directory = File(cacheDir, "clipboard").apply { mkdirs() }
        val file = File(directory, "clipboard.png")
        file.writeBytes(bytes)

        val uri = FileProvider.getUriForFile(this, "$packageName.fileprovider", file)
        clipboard().setPrimaryClip(ClipData.newUri(contentResolver, "CrossCopy image", uri))

        // The next poll must re-read the file we just wrote.
        cachedSignature = null
        cachedPng = null
    }

    companion object {
        private const val CHANNEL = "crosscopy/clipboard_image"
    }
}
