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

sealed class DecisionResult {
    data class Success(val response: DecisionResponse) : DecisionResult()
    data class Failure(
        val errorCode: String,
        val errorMessage: String,
        val requestId: String,
        val httpStatus: Int
    ) : DecisionResult()
}
