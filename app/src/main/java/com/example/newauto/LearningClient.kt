package com.example.newauto

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class LearningClient(private val baseUrl: String) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(2, TimeUnit.SECONDS)
        .readTimeout(130, TimeUnit.SECONDS)
        .callTimeout(140, TimeUnit.SECONDS)
        .build()

    fun submitSample(
        sessionId: String,
        goalId: String,
        description: String,
        actionType: String,
        intent: String,
        skillTags: String,
        sceneTags: String,
        x: Int,
        y: Int,
        waitMs: Int,
        beforePng: ByteArray,
        afterPng: ByteArray,
        sequenceDone: Boolean
    ): LearningResult {
        return try {
            val body = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("session_id", sessionId)
                .addFormDataPart("goal_id", goalId)
                .addFormDataPart("description", description)
                .addFormDataPart("action_type", actionType)
                .addFormDataPart("intent", intent)
                .addFormDataPart("skill_tags", skillTags)
                .addFormDataPart("scene_tags", sceneTags)
                .addFormDataPart("x", x.toString())
                .addFormDataPart("y", y.toString())
                .addFormDataPart("wait_ms", waitMs.toString())
                .addFormDataPart("sequence_done", sequenceDone.toString())
                .addFormDataPart(
                    "before_file",
                    "before.png",
                    beforePng.toRequestBody("image/png".toMediaType())
                )
                .addFormDataPart(
                    "after_file",
                    "after.png",
                    afterPng.toRequestBody("image/png".toMediaType())
                )
                .build()

            val request = Request.Builder()
                .url("$baseUrl/learn")
                .post(body)
                .build()

            client.newCall(request).execute().use { response ->
                val responseBody = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    val json = runCatching { JSONObject(responseBody) }.getOrNull()
                    val detail = json?.optJSONObject("detail")
                    return LearningResult.Failure(
                        errorCode = detail?.optString("error_code", "learn_http_error") ?: "learn_http_error",
                        errorMessage = detail?.optString("error_message", responseBody.take(180)) ?: responseBody.take(180),
                        requestId = detail?.optString("request_id", "") ?: "",
                        httpStatus = response.code
                    )
                }

                val json = runCatching { JSONObject(responseBody) }.getOrElse { JSONObject() }
                LearningResult.Success(LearningAck(message = json.optString("message", "learn_saved")))
            }
        } catch (e: Exception) {
            LearningResult.Failure(
                errorCode = "learn_client_exception",
                errorMessage = e.message ?: "learning request failed",
                requestId = "",
                httpStatus = -1
            )
        }
    }
}
