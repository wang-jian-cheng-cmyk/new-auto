package com.example.newauto

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ComponentName
import android.content.Context
import android.graphics.Path
import android.graphics.Rect
import android.provider.Settings
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import java.security.MessageDigest

class AutomationAccessibilityService : AccessibilityService() {
    companion object {
        @Volatile
        var instance: AutomationAccessibilityService? = null
            private set

        @Volatile
        var isConnected: Boolean = false
            private set

        fun isServiceEnabled(context: Context): Boolean {
            val enabled = Settings.Secure.getString(
                context.contentResolver,
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
            ) ?: return false
            val component = ComponentName(context, AutomationAccessibilityService::class.java)
            val full = component.flattenToString()
            val short = component.flattenToShortString()
            return enabled.split(':').any {
                it.equals(full, ignoreCase = true) || it.equals(short, ignoreCase = true)
            }
        }

        fun isServiceReady(context: Context): Boolean {
            return isConnected && instance != null && isServiceEnabled(context)
        }
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        isConnected = true
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit

    override fun onInterrupt() {
        isConnected = false
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        isConnected = false
        if (instance === this) instance = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        super.onDestroy()
        isConnected = false
        if (instance === this) instance = null
    }

    fun tap(x: Int, y: Int, durationMs: Long): Boolean {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val stroke = GestureDescription.StrokeDescription(path, 0L, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        return dispatchGesture(gesture, null, null)
    }

    fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Long): Boolean {
        val path = Path().apply {
            moveTo(x1.toFloat(), y1.toFloat())
            lineTo(x2.toFloat(), y2.toFloat())
        }
        val stroke = GestureDescription.StrokeDescription(path, 0L, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        return dispatchGesture(gesture, null, null)
    }

    fun goBack(): Boolean {
        return performGlobalAction(GLOBAL_ACTION_BACK)
    }

    fun dumpActionableNodes(maxNodes: Int = 120): List<UiActionableNode> {
        val root = rootInActiveWindow ?: return emptyList()
        val out = ArrayList<UiActionableNode>()
        val seen = HashSet<String>()

        fun walk(node: AccessibilityNodeInfo?) {
            if (node == null || out.size >= maxNodes) return

            val rect = Rect()
            node.getBoundsInScreen(rect)
            val validRect = rect.right > rect.left && rect.bottom > rect.top
            val clickable = node.isClickable
            val enabled = node.isEnabled
            if (validRect && clickable && enabled) {
                val key = "${rect.left},${rect.top},${rect.right},${rect.bottom}|${node.className}|${node.packageName}"
                if (seen.add(key)) {
                    out.add(
                        UiActionableNode(
                            nodeId = sha1Hex(key).take(16),
                            x1 = rect.left,
                            y1 = rect.top,
                            x2 = rect.right,
                            y2 = rect.bottom,
                            centerX = (rect.left + rect.right) / 2,
                            centerY = (rect.top + rect.bottom) / 2,
                            className = node.className?.toString().orEmpty(),
                            packageName = node.packageName?.toString().orEmpty(),
                            clickable = true,
                            enabled = true
                        )
                    )
                }
            }

            for (i in 0 until node.childCount) {
                walk(node.getChild(i))
            }
        }

        walk(root)
        return out
    }

    private fun sha1Hex(raw: String): String {
        val digest = MessageDigest.getInstance("SHA-1").digest(raw.toByteArray())
        return digest.joinToString("") { "%02x".format(it) }
    }
}
