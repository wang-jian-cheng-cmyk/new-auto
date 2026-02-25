package com.example.newauto

import android.app.AlertDialog
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.graphics.PixelFormat
import android.net.Uri
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

class FloatingControlService : Service() {
    companion object {
        const val ACTION_SHOW = "show"
        private const val CHANNEL_ID = "new_auto_channel"
        private const val NOTIFICATION_ID = 8
    }

    private lateinit var windowManager: WindowManager
    private var rootView: View? = null
    private val serviceScope = CoroutineScope(Dispatchers.Main + SupervisorJob())
    private var loopJob: Job? = null
    private val captureMutex = Mutex()
    @Volatile
    private var captureHideActive = false

    private val learningClient by lazy { LearningClient(baseUrl = "http://127.0.0.1:8788") }
    private val decisionClient by lazy { DecisionClient(baseUrl = "http://127.0.0.1:8788") }
    private val engine by lazy { AutomationEngine(this, decisionClient) { captureFrameWithoutOverlay() } }

    private var learningMode = false
    private var learningBeforePng: ByteArray? = null
    private var learningAfterPng: ByteArray? = null
    private var learnX = 0
    private var learnY = 0
    private var learnWaitMs = 1200
    private var learnGoalId = "daily_loop"
    private var learnActionType = "click"
    private var learnIntent = "observe_state"
    private var learnSkillTags = ""
    private var learnSceneTags = ""

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(NOTIFICATION_ID, buildNotification("悬浮服务运行中"))
        windowManager = getSystemService(WINDOW_SERVICE) as WindowManager
        showFloatingWindow()
    }

    override fun onDestroy() {
        super.onDestroy()
        stopLoop("服务停止")
        rootView?.let { windowManager.removeView(it) }
        rootView = null
        serviceScope.cancel()
    }

    private fun showFloatingWindow() {
        if (rootView != null) return

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(0xCC101820.toInt())
            setPadding(24, 24, 24, 24)
        }

        val headerLayout = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        val statusText = TextView(this).apply {
            text = "状态: 待机"
            setTextColor(0xFFFFFFFF.toInt())
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }
        val toggleBtn = Button(this).apply { text = "收起" }
        val controlsLayout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val controlsScroll = ScrollView(this)
        var wmParams: WindowManager.LayoutParams? = null

        val startBtn = Button(this).apply { text = "开始自动" }
        val pauseBtn = Button(this).apply { text = "暂停自动" }
        val learnModeBtn = Button(this).apply { text = "学习模式: 关" }
        val testTapBtn = Button(this).apply { text = "测试点击" }
        val probeRawUiBtn = Button(this).apply { text = "测试:原始UI分析" }
        val screenInfoBtn = Button(this).apply { text = "查看屏幕大小" }
        val openA11yBtn = Button(this).apply { text = "打开无障碍设置" }
        val diagnoseBtn = Button(this).apply { text = "连接诊断" }
        val learnBeforeBtn = Button(this).apply { text = "学:记录前截图" }
        val learnAfterBtn = Button(this).apply { text = "学:记录后截图" }
        val learnSubmitBtn = Button(this).apply { text = "学:提交步骤" }
        val learnFinishBtn = Button(this).apply { text = "学:结束序列" }
        val closeBtn = Button(this).apply { text = "关闭悬浮窗" }

        var collapsed = false
        fun setCollapsed(value: Boolean) {
            collapsed = value
            controlsScroll.visibility = if (collapsed) View.GONE else View.VISIBLE
            statusText.visibility = if (collapsed) View.GONE else View.VISIBLE
            toggleBtn.text = if (collapsed) "◉" else "收起"
            layout.setPadding(
                if (collapsed) 8 else 24,
                if (collapsed) 8 else 24,
                if (collapsed) 8 else 24,
                if (collapsed) 8 else 24
            )
            wmParams?.let { p ->
                p.x = if (collapsed) -10 else 30
                windowManager.updateViewLayout(layout, p)
            }
        }

        toggleBtn.setOnClickListener { setCollapsed(!collapsed) }

        var dragStartX = 0
        var dragStartY = 0
        var touchStartRawX = 0f
        var touchStartRawY = 0f
        statusText.setOnTouchListener { _, event ->
            val params = wmParams ?: return@setOnTouchListener false
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    dragStartX = params.x
                    dragStartY = params.y
                    touchStartRawX = event.rawX
                    touchStartRawY = event.rawY
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = (event.rawX - touchStartRawX).toInt()
                    val dy = (event.rawY - touchStartRawY).toInt()
                    params.x = dragStartX - dx
                    params.y = dragStartY + dy
                    windowManager.updateViewLayout(layout, params)
                    true
                }
                else -> false
            }
        }

        startBtn.setOnClickListener {
            if (learningMode) {
                statusText.text = "状态: 学习模式中"
                return@setOnClickListener
            }
            if (loopJob?.isActive == true) return@setOnClickListener
            statusText.text = "状态: 自动运行中"
            startLoop(statusText)
            setCollapsed(true)
        }

        pauseBtn.setOnClickListener {
            stopLoop("用户暂停")
            statusText.text = "状态: 已暂停"
        }

        learnModeBtn.setOnClickListener {
            learningMode = !learningMode
            learnModeBtn.text = if (learningMode) "学习模式: 开" else "学习模式: 关"
            if (learningMode) stopLoop("进入学习模式")
            statusText.text = if (learningMode) "状态: 学习模式已开启" else "状态: 待机"
        }

        testTapBtn.setOnClickListener {
            val width = resources.displayMetrics.widthPixels
            val height = resources.displayMetrics.heightPixels
            val svc = AutomationAccessibilityService.instance
            if (!AutomationAccessibilityService.isServiceReady(this)) {
                statusText.text = "状态: ${diagnoseAccessibilityState()}"
                return@setOnClickListener
            }
            val ok = svc?.tap((width * 0.5).toInt(), (height * 0.6).toInt(), 120) == true
            statusText.text = if (ok) "状态: 测试点击已发送" else "状态: 测试点击失败"
        }

        probeRawUiBtn.setOnClickListener {
            serviceScope.launch(Dispatchers.IO) {
                if (!AutomationAccessibilityService.isServiceReady(this@FloatingControlService)) {
                    withContext(Dispatchers.Main) {
                        statusText.text = "状态: ${diagnoseAccessibilityState()}"
                    }
                    return@launch
                }

                val ok = ScreenCaptureManager.initIfNeeded(this@FloatingControlService)
                withContext(Dispatchers.Main) {
                    if (!ok) {
                        statusText.text = "状态: 请先在主界面申请录屏权限"
                    } else {
                        statusText.text = "状态: 截图中"
                    }
                }
                if (!ok) return@launch

                val png = captureFrameWithoutOverlay()
                if (png == null) {
                    withContext(Dispatchers.Main) {
                        statusText.text = "状态: 调试截图失败"
                    }
                    return@launch
                }

                val rawNodes = AutomationAccessibilityService.instance?.dumpRawNodes().orEmpty()
                val xmlLike = buildXmlLike(rawNodes)

                withContext(Dispatchers.Main) {
                    statusText.text = "状态: 等待模型分析(${rawNodes.size}节点)"
                }

                val result = decisionClient.debugProbe(
                    goalId = "daily_loop",
                    rawNodes = rawNodes,
                    xmlLike = xmlLike,
                    screenshotPngBytes = png
                )

                withContext(Dispatchers.Main) {
                    statusText.text = result.fold(
                        onSuccess = { "状态: ${it.summary}" },
                        onFailure = { "状态: 调试失败(${it.message?.take(80)})" }
                    )
                    ensureOverlayVisible()
                }
            }
        }

        screenInfoBtn.setOnClickListener {
            statusText.text = "状态: ${currentScreenInfo()}"
        }

        openA11yBtn.setOnClickListener {
            val intent = Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            startActivity(intent)
        }

        diagnoseBtn.setOnClickListener {
            statusText.text = "状态: ${diagnoseAccessibilityState()}"
        }

        learnBeforeBtn.setOnClickListener {
            serviceScope.launch(Dispatchers.IO) {
                val ok = ScreenCaptureManager.initIfNeeded(this@FloatingControlService)
                withContext(Dispatchers.Main) {
                    if (!ok) {
                        statusText.text = "状态: 请先在主界面申请录屏权限"
                    }
                }
                if (!ok) return@launch

                val png = captureFrameWithoutOverlay()
                withContext(Dispatchers.Main) {
                    if (png == null) {
                        statusText.text = "状态: 前截图失败"
                    } else {
                        learningBeforePng = png
                        statusText.text = "状态: 已记录前截图"
                    }
                    ensureOverlayVisible()
                }
            }
        }

        learnAfterBtn.setOnClickListener {
            serviceScope.launch(Dispatchers.IO) {
                val ok = ScreenCaptureManager.initIfNeeded(this@FloatingControlService)
                withContext(Dispatchers.Main) {
                    if (!ok) {
                        statusText.text = "状态: 请先在主界面申请录屏权限"
                    }
                }
                if (!ok) return@launch

                val png = captureFrameWithoutOverlay()
                withContext(Dispatchers.Main) {
                    if (png == null) {
                        statusText.text = "状态: 后截图失败"
                    } else {
                        learningAfterPng = png
                        statusText.text = "状态: 已记录后截图"
                    }
                    ensureOverlayVisible()
                }
            }
        }

        learnSubmitBtn.setOnClickListener {
            if (!learningMode) {
                statusText.text = "状态: 请先开启学习模式"
                return@setOnClickListener
            }
            showLearningInputDialog(statusText, sequenceDone = false)
        }

        learnFinishBtn.setOnClickListener {
            if (!learningMode) {
                statusText.text = "状态: 请先开启学习模式"
                return@setOnClickListener
            }
            showLearningInputDialog(statusText, sequenceDone = true)
        }

        closeBtn.setOnClickListener { stopSelf() }

        headerLayout.addView(statusText)
        headerLayout.addView(toggleBtn)
        controlsLayout.addView(startBtn)
        controlsLayout.addView(pauseBtn)
        controlsLayout.addView(learnModeBtn)
        controlsLayout.addView(learnBeforeBtn)
        controlsLayout.addView(learnAfterBtn)
        controlsLayout.addView(learnSubmitBtn)
        controlsLayout.addView(learnFinishBtn)
        controlsLayout.addView(testTapBtn)
        controlsLayout.addView(probeRawUiBtn)
        controlsLayout.addView(screenInfoBtn)
        controlsLayout.addView(openA11yBtn)
        controlsLayout.addView(diagnoseBtn)
        controlsLayout.addView(closeBtn)
        layout.addView(headerLayout)

        val maxControlsHeight = (resources.displayMetrics.heightPixels * 0.62f).toInt().coerceAtLeast(240)
        controlsScroll.layoutParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT,
            maxControlsHeight
        )
        controlsScroll.addView(controlsLayout)
        layout.addView(controlsScroll)

        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            } else {
                WindowManager.LayoutParams.TYPE_PHONE
            },
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.END
            x = 30
            y = 160
        }
        wmParams = params

        windowManager.addView(layout, params)
        rootView = layout
    }

    private fun startLoop(statusText: TextView) {
        loopJob = serviceScope.launch(Dispatchers.IO) {
            while (isActive) {
                if (!AutomationAccessibilityService.isServiceReady(this@FloatingControlService)) {
                    withContext(Dispatchers.Main) {
                        statusText.text = "状态: ${diagnoseAccessibilityState()}"
                    }
                    delay(1000)
                    continue
                }

                val decision = engine.decideNextAction()
                val response = when (decision) {
                    is DecisionResult.Success -> decision.response
                    is DecisionResult.Failure -> {
                        withContext(Dispatchers.Main) {
                            statusText.text = "状态: ${decision.errorCode} ${decision.errorMessage}"
                        }
                        delay(1000)
                        continue
                    }
                }

                val executed = engine.executeDecision(response)
                val effect = if (response.action == "wait") "observe" else "pending"
                engine.commitOutcome(executed, effect)

                withContext(Dispatchers.Main) {
                    statusText.text = when {
                        !executed -> "状态: 无障碍未连接"
                        response.skillId.isNotEmpty() -> "状态: ${response.intent} [${response.skillId}] ${response.waitMs}ms"
                        else -> "状态: ${response.intent} ${response.waitMs}ms"
                    }
                }
                delay(response.waitMs.coerceIn(300, 5000).toLong())
            }
        }
    }

    private fun stopLoop(reason: String) {
        loopJob?.cancel()
        loopJob = null
        val manager = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        manager.notify(NOTIFICATION_ID, buildNotification(reason))
    }

    private fun diagnoseAccessibilityState(): String {
        val enabled = AutomationAccessibilityService.isServiceEnabled(this)
        val connected = AutomationAccessibilityService.isConnected
        return when {
            !enabled -> "无障碍未启用"
            enabled && !connected -> "无障碍已启用但服务未运行"
            !AutomationAccessibilityService.isServiceReady(this) -> "无障碍状态异常"
            else -> "无障碍已连接"
        }
    }

    private fun currentScreenInfo(): String {
        val metrics = resources.displayMetrics
        val orientation = if (metrics.widthPixels > metrics.heightPixels) "横屏" else "竖屏"
        return "${metrics.widthPixels}x${metrics.heightPixels} ($orientation)"
    }

    private fun buildXmlLike(rawNodes: List<RawUiNode>): String {
        val sb = StringBuilder()
        sb.append("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>")
        sb.append("<hierarchy rotation=\"1\">")
        rawNodes.forEachIndexed { idx, n ->
            sb.append("<node ")
            sb.append("index=\"").append(idx).append("\" ")
            sb.append("text=\"\" ")
            sb.append("resource-id=\"\" ")
            sb.append("class=\"").append(escapeXml(n.className)).append("\" ")
            sb.append("package=\"").append(escapeXml(n.packageName)).append("\" ")
            sb.append("content-desc=\"\" ")
            sb.append("clickable=\"").append(n.clickable).append("\" ")
            sb.append("enabled=\"").append(n.enabled).append("\" ")
            sb.append("focusable=\"").append(n.focusable).append("\" ")
            sb.append("child-count=\"").append(n.childCount).append("\" ")
            sb.append("bounds=\"[")
                .append(n.x1).append(",").append(n.y1)
                .append("][")
                .append(n.x2).append(",").append(n.y2)
                .append("]\" />")
        }
        sb.append("</hierarchy>")
        return sb.toString()
    }

    private fun escapeXml(raw: String): String {
        return raw.replace("&", "&amp;")
            .replace("\"", "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    }

    private suspend fun captureFrameWithoutOverlay(): ByteArray? {
        return captureMutex.withLock {
            val view = rootView
            if (view == null) return@withLock ScreenCaptureManager.capturePngBytes()
            val previousVisibility = view.visibility
            try {
                withContext(Dispatchers.Main) {
                    captureHideActive = true
                    view.visibility = View.GONE
                }
                delay(80)
                ScreenCaptureManager.capturePngBytes()
            } finally {
                withContext(Dispatchers.Main) {
                    captureHideActive = false
                    if (view.parent != null) {
                        view.visibility = previousVisibility
                    } else {
                        ensureOverlayVisible()
                    }
                }
            }
        }
    }

    private fun ensureOverlayVisible() {
        if (captureHideActive) return
        val view = rootView
        if (view == null) {
            showFloatingWindow()
            return
        }
        if (view.parent == null) {
            rootView = null
            showFloatingWindow()
            return
        }
        if (view.visibility != View.VISIBLE) {
            view.visibility = View.VISIBLE
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(CHANNEL_ID, "New Auto", NotificationManager.IMPORTANCE_LOW)
        val manager = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("New Auto")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setOngoing(true)
            .build()
    }

    private fun showLearningInputDialog(statusText: TextView, sequenceDone: Boolean) {
        val before = learningBeforePng
        val after = learningAfterPng
        if (before == null || after == null) {
            statusText.text = "状态: 请先记录前后截图"
            return
        }

        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(20, 20, 20, 20)
        }
        val scroll = ScrollView(this).apply {
            addView(container)
        }
        val descInput = EditText(this).apply { hint = "步骤描述（手动输入）" }
        val goalInput = EditText(this).apply {
            hint = "goal_id"
            setText(learnGoalId)
        }
        val actionInput = EditText(this).apply {
            hint = "action_type (click/wait)"
            setText(learnActionType)
        }
        val intentInput = EditText(this).apply {
            hint = "intent (tap_skip/toggle_auto_battle/observe_state)"
            setText(learnIntent)
        }
        val skillTagsInput = EditText(this).apply {
            hint = "skill_tags (comma-separated)"
            setText(learnSkillTags)
        }
        val sceneTagsInput = EditText(this).apply {
            hint = "scene_tags (comma-separated)"
            setText(learnSceneTags)
        }
        val xInput = EditText(this).apply {
            hint = "x"
            setText(learnX.toString())
        }
        val yInput = EditText(this).apply {
            hint = "y"
            setText(learnY.toString())
        }
        val waitInput = EditText(this).apply {
            hint = "wait_ms"
            setText(learnWaitMs.toString())
        }

        container.addView(descInput)
        container.addView(goalInput)
        container.addView(actionInput)
        container.addView(intentInput)
        container.addView(skillTagsInput)
        container.addView(sceneTagsInput)
        container.addView(xInput)
        container.addView(yInput)
        container.addView(waitInput)

        val dialog = AlertDialog.Builder(this)
            .setTitle(if (sequenceDone) "学习: 结束并生成序列" else "学习: 追加步骤")
            .setView(scroll)
            .setNegativeButton("取消", null)
            .setPositiveButton("提交") { _, _ ->
                val description = descInput.text?.toString().orEmpty().trim()
                val goalId = goalInput.text?.toString().orEmpty().trim().ifEmpty { "daily_loop" }
                val actionType = actionInput.text?.toString().orEmpty().trim().ifEmpty { "click" }
                val intent = intentInput.text?.toString().orEmpty().trim().ifEmpty { "observe_state" }
                val skillTags = skillTagsInput.text?.toString().orEmpty().trim()
                val sceneTags = sceneTagsInput.text?.toString().orEmpty().trim()
                val x = xInput.text?.toString()?.toIntOrNull()?.coerceAtLeast(0) ?: 0
                val y = yInput.text?.toString()?.toIntOrNull()?.coerceAtLeast(0) ?: 0
                val waitMs = waitInput.text?.toString()?.toIntOrNull()?.coerceIn(300, 5000) ?: 1200

                learnGoalId = goalId
                learnActionType = actionType
                learnIntent = intent
                learnSkillTags = skillTags
                learnSceneTags = sceneTags
                learnX = x
                learnY = y
                learnWaitMs = waitMs

                serviceScope.launch(Dispatchers.IO) {
                    val result = learningClient.submitSample(
                        sessionId = "device-local",
                        goalId = goalId,
                        description = description,
                        actionType = actionType,
                        intent = intent,
                        skillTags = skillTags,
                        sceneTags = sceneTags,
                        x = x,
                        y = y,
                        waitMs = waitMs,
                        beforePng = before,
                        afterPng = after,
                        sequenceDone = sequenceDone
                    )
                    withContext(Dispatchers.Main) {
                        when (result) {
                            is LearningResult.Success -> {
                                statusText.text = "状态: 学习已保存"
                                learningBeforePng = null
                                learningAfterPng = null
                            }
                            is LearningResult.Failure -> {
                                statusText.text = "状态: 学习失败(${result.errorCode})"
                            }
                        }
                        ensureOverlayVisible()
                    }
                }
            }
            .create()

        dialog.show()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            dialog.window?.setType(WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY)
        }
    }
}
