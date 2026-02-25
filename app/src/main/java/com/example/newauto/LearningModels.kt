package com.example.newauto

data class LearningAck(
    val message: String
)

sealed class LearningResult {
    data class Success(val ack: LearningAck) : LearningResult()
    data class Failure(
        val errorCode: String,
        val errorMessage: String,
        val requestId: String,
        val httpStatus: Int
    ) : LearningResult()
}
