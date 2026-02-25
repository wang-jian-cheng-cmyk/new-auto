package com.example.newauto

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.net.SocketTimeoutException
import java.util.concurrent.TimeUnit

class DecisionClient(private val baseUrl: String) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(2, TimeUnit.SECONDS)
        .readTimeout(45, TimeUnit.SECONDS)
        .callTimeout(50, TimeUnit.SECONDS)
        .build()

    fun decide(
        sessionId: String,
        currentGoalId: String,
        history: List<ActionHistoryItem>,
        actionableNodes: List<UiActionableNode>,
        screenshotPngBytes: ByteArray,
        screenW: Int,
        screenH: Int,
        orientation: String = "landscape"
    ): DecisionResult {
        return try {
            val historyJson = JSONArray().apply {
                history.forEach { item ->
                    put(
                        JSONObject().apply {
                            put("action", item.action)
                            put("intent", item.intent)
                            put("x", item.x)
                            put("y", item.y)
                            put("wait_ms", item.waitMs)
                            put("result", item.result)
                            put("effect", item.effect)
                            put("reason", item.reason)
                            put("timestamp_ms", item.timestampMs)
                        }
                    )
                }
            }

            val body = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("session_id", sessionId)
                .addFormDataPart("timestamp_ms", System.currentTimeMillis().toString())
                .addFormDataPart("current_goal_id", currentGoalId)
                .addFormDataPart("screen_w", screenW.toString())
                .addFormDataPart("screen_h", screenH.toString())
                .addFormDataPart("orientation", orientation)
                .addFormDataPart("history_json", historyJson.toString())
                .addFormDataPart(
                    "ui_nodes_json",
                    JSONArray().apply {
                        actionableNodes.forEach { n ->
                            put(
                                JSONObject().apply {
                                    put("node_id", n.nodeId)
                                    put("x1", n.x1)
                                    put("y1", n.y1)
                                    put("x2", n.x2)
                                    put("y2", n.y2)
                                    put("center_x", n.centerX)
                                    put("center_y", n.centerY)
                                    put("class", n.className)
                                    put("package", n.packageName)
                                    put("clickable", n.clickable)
                                    put("enabled", n.enabled)
                                    put("actionable", n.actionable)
                                    put("source", n.source)
                                }
                            )
                        }
                    }.toString()
                )
                .addFormDataPart(
                    "screenshot_file",
                    "frame.png",
                    screenshotPngBytes.toRequestBody("image/png".toMediaType())
                )
                .build()

            val request = Request.Builder()
                .url("$baseUrl/decide_v2")
                .post(body)
                .build()

            client.newCall(request).execute().use { response ->
                val rawBody = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    val json = runCatching { JSONObject(rawBody) }.getOrNull()
                    val detail = json?.optJSONObject("detail")
                    return DecisionResult.Failure(
                        errorCode = detail?.optString("error_code", "gateway_http_error") ?: "gateway_http_error",
                        errorMessage = detail?.optString("error_message", rawBody.take(180)) ?: rawBody.take(180),
                        requestId = detail?.optString("request_id", "") ?: "",
                        httpStatus = response.code
                    )
                }

                val json = JSONObject(rawBody)
                return DecisionResult.Success(
                    DecisionResponse(
                        action = json.optString("action", "wait"),
                        intent = json.optString("intent", "observe_state"),
                        x = json.optInt("x", 0),
                        y = json.optInt("y", 0),
                        waitMs = json.optInt("wait_ms", 1000),
                        goalId = json.optString("goal_id", currentGoalId),
                        reason = json.optString("reason", ""),
                        skillId = json.optString("skill_id", ""),
                        stepIndex = json.optInt("step_index", -1)
                    )
                )
            }
        } catch (e: SocketTimeoutException) {
            DecisionResult.Failure(
                errorCode = "gateway_client_timeout",
                errorMessage = e.message ?: "gateway request timeout",
                requestId = "",
                httpStatus = -1
            )
        } catch (e: Exception) {
            DecisionResult.Failure(
                errorCode = "gateway_client_exception",
                errorMessage = e.message ?: "gateway request failed",
                requestId = "",
                httpStatus = -1
            )
        }
    }
}
