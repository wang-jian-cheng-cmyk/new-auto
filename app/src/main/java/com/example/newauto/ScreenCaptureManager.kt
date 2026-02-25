package com.example.newauto

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.Looper
import java.io.ByteArrayOutputStream

object ScreenCaptureManager {
    private var mediaProjection: MediaProjection? = null
    private var imageReader: ImageReader? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var projectionResultCode: Int? = null
    private var projectionData: Intent? = null

    private val projectionCallback = object : MediaProjection.Callback() {
        override fun onStop() {
            release()
        }
    }

    @Synchronized
    fun setProjectionPermission(resultCode: Int, data: Intent) {
        projectionResultCode = resultCode
        projectionData = Intent(data)
    }

    @Synchronized
    fun hasProjectionPermission(): Boolean {
        return projectionResultCode != null && projectionData != null
    }

    @Synchronized
    fun initIfNeeded(context: Context): Boolean {
        if (mediaProjection != null && imageReader != null && virtualDisplay != null) {
            return true
        }
        val resultCode = projectionResultCode ?: return false
        val data = projectionData ?: return false

        release()
        val manager = context.getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        mediaProjection = manager.getMediaProjection(resultCode, data)
        if (mediaProjection == null) return false
        mediaProjection?.registerCallback(projectionCallback, Handler(Looper.getMainLooper()))

        val metrics = context.resources.displayMetrics
        imageReader = ImageReader.newInstance(metrics.widthPixels, metrics.heightPixels, PixelFormat.RGBA_8888, 2)
        virtualDisplay = mediaProjection?.createVirtualDisplay(
            "new_auto_capture",
            metrics.widthPixels,
            metrics.heightPixels,
            metrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            imageReader?.surface,
            null,
            null
        )
        return virtualDisplay != null
    }

    @Synchronized
    fun capturePngBytes(): ByteArray? {
        val reader = imageReader ?: return null
        val image = reader.acquireLatestImage() ?: return null
        image.use { img ->
            val plane = img.planes[0]
            val buffer = plane.buffer
            val pixelStride = plane.pixelStride
            val rowStride = plane.rowStride
            val rowPadding = rowStride - pixelStride * img.width

            val bitmap = Bitmap.createBitmap(
                img.width + rowPadding / pixelStride,
                img.height,
                Bitmap.Config.ARGB_8888
            )
            bitmap.copyPixelsFromBuffer(buffer)

            val cropped = Bitmap.createBitmap(bitmap, 0, 0, img.width, img.height)
            bitmap.recycle()

            val out = ByteArrayOutputStream()
            cropped.compress(Bitmap.CompressFormat.PNG, 100, out)
            cropped.recycle()
            return out.toByteArray()
        }
    }

    @Synchronized
    fun release() {
        mediaProjection?.unregisterCallback(projectionCallback)
        virtualDisplay?.release()
        virtualDisplay = null
        imageReader?.close()
        imageReader = null
        mediaProjection?.stop()
        mediaProjection = null
    }
}
