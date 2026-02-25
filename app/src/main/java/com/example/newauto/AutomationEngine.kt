package com.example.newauto

import android.content.Context
import java.security.MessageDigest
import java.util.ArrayDeque

class AutomationEngine(
    context: Context,
    private val decisionClient: DecisionClient
) {
    private val appContext = context.applicationContext
    private val history = ArrayDeque<ActionHistoryItem>()
    private var lastFrameHash: String? = null
    private var pendingDecision: DecisionResponse? = null
    private var currentGoalId: String = "daily_loop"

    fun decideNextAction(): DecisionResult {
        if (!ScreenCaptureManager.initIfNeeded(appContext)) {
            return DecisionResult.Failure(
                errorCode = "capture_not_ready",
                errorMessage = "screen capture not initialized",
                requestId = "",
                httpStatus = -1
            )
        }

        val metrics = appContext.resources.displayMetrics
        if (metrics.widthPixels <= metrics.heightPixels) {
            return DecisionResult.Failure(
                errorCode = "orientation_not_landscape",
                errorMessage = "landscape required (${metrics.widthPixels}x${metrics.heightPixels})",
                requestId = "",
                httpStatus = -1
            )
        }

        val png = ScreenCaptureManager.capturePngBytes() ?: return DecisionResult.Failure(
            errorCode = "capture_frame_empty",
            errorMessage = "failed to capture frame",
            requestId = "",
            httpStatus = -1
        )

        val frameHash = sha1Hex(png)
        updateLatestHistoryEffect(frameHash)
        lastFrameHash = frameHash

        return decisionClient.decide(
            sessionId = "device-local",
            currentGoalId = currentGoalId,
            history = history.toList(),
            screenshotPngBytes = png,
            screenW = metrics.widthPixels,
            screenH = metrics.heightPixels,
            orientation = "landscape"
        )
    }

    fun executeDecision(response: DecisionResponse): Boolean {
        val svc = AutomationAccessibilityService.instance ?: return false
        pendingDecision = response
        currentGoalId = response.goalId

        return when (response.action) {
            "click" -> {
                val metrics = appContext.resources.displayMetrics
                val x = response.x.coerceIn(0, metrics.widthPixels - 1)
                val y = response.y.coerceIn(0, metrics.heightPixels - 1)
                svc.tap(x, y, 120)
            }
            "wait" -> true
            else -> true
        }
    }

    fun commitOutcome(executed: Boolean, effect: String) {
        val decision = pendingDecision ?: return
        pushHistory(
            ActionHistoryItem(
                action = decision.action,
                intent = decision.intent,
                x = decision.x,
                y = decision.y,
                waitMs = decision.waitMs,
                result = if (executed) "ok" else "failed",
                effect = effect,
                reason = decision.reason,
                timestampMs = System.currentTimeMillis()
            )
        )
        pendingDecision = null
    }

    private fun updateLatestHistoryEffect(currentFrameHash: String) {
        if (history.isEmpty()) return
        val last = history.removeLast()
        val effect = if (lastFrameHash != null && lastFrameHash == currentFrameHash) "no_change" else "changed"
        history.addLast(last.copy(effect = effect))
    }

    private fun pushHistory(item: ActionHistoryItem) {
        if (history.size >= 8) history.removeFirst()
        history.addLast(item)
    }

    private fun sha1Hex(bytes: ByteArray): String {
        val digest = MessageDigest.getInstance("SHA-1").digest(bytes)
        return digest.joinToString("") { "%02x".format(it) }
    }
}
