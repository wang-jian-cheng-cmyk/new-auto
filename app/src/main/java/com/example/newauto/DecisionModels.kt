package com.example.newauto

data class DecisionResponse(
    val action: String,
    val intent: String,
    val x: Int,
    val y: Int,
    val waitMs: Int,
    val goalId: String,
    val reason: String,
    val skillId: String = "",
    val stepIndex: Int = -1
)

data class ActionHistoryItem(
    val action: String,
    val intent: String,
    val x: Int,
    val y: Int,
    val waitMs: Int,
    val result: String,
    val effect: String,
    val reason: String,
    val timestampMs: Long
)

data class UiActionableNode(
    val nodeId: String,
    val x1: Int,
    val y1: Int,
    val x2: Int,
    val y2: Int,
    val centerX: Int,
    val centerY: Int,
    val className: String,
    val packageName: String,
    val clickable: Boolean,
    val enabled: Boolean,
    val actionable: Boolean,
    val source: String
)

sealed class DecisionResult {
    data class Success(val response: DecisionResponse) : DecisionResult()
    data class Failure(
        val errorCode: String,
        val errorMessage: String,
        val requestId: String,
        val httpStatus: Int
    ) : DecisionResult()
}
