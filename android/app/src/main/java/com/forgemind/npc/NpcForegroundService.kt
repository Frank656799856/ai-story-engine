package com.forgemind.npc

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlin.concurrent.thread

/**
 * 前台服务：在后台线程运行 Chaquopy 内嵌的 Flask 服务（127.0.0.1:5003）。
 *
 * 关键：必须在「其它后端模块 import 之前」就把 NPC_DATA_BASE 环境变量注入，
 * 这样各模块计算 MEMORY_BASE / config.txt 等路径时才会重定向到 filesDir。
 * 因此统一走后端入口模块 npc_launcher.start(dataBase, port)：
 *   先 os.environ['NPC_DATA_BASE'] = <filesDir>
 *   再 import app（此时 store/index/game_db 等才计算路径）
 *   再启动 Flask。
 */
class NpcForegroundService : Service() {

    companion object {
        const val CHANNEL_ID = "npc_forgemind"
        const val NOTIFICATION_ID = 1
        const val ACTION_START = "com.forgemind.npc.START"
        const val ACTION_STOP = "com.forgemind.npc.STOP"
        const val TAG = "NpcService"
        const val PORT = 5003

        @Volatile
        var isRunning: Boolean = false
            private set

        private var serverThread: Thread? = null
        private val startLock = Any()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> { stopSelf(); return START_NOT_STICKY }
            else -> startForegroundServer()
        }
        return START_STICKY
    }

    private fun startForegroundServer() {
        createChannel()
        startForeground(NOTIFICATION_ID, buildNotification("NPC 服务启动中…"))

        // synchronized + isRunning 双重防护：连点按钮/连续 startService intent 时，
        // 第二次进入会等待锁并看到 isRunning=true 直接返回。
        // 否则两个线程可能并发通过 Python.isStarted() 检查，双重初始化解释器导致死锁
        // （表现为端口监听但永不 accept，浏览器 Failed to fetch）。
        synchronized(startLock) {
            if (serverThread?.isAlive == true || isRunning) {
                updateNotification("NPC 服务运行中")
                return
            }
            isRunning = true
            serverThread = thread(name = "FlaskServer") {
                try {
                    val dataBase = AppPaths.dataRoot(this@NpcForegroundService).absolutePath
                    // 后台线程解包 + 建 config（前台线程已完成，这里幂等再确认）
                    AppPaths.unpackStatic(this@NpcForegroundService)
                    AppPaths.ensureConfig(this@NpcForegroundService)
                    // 启动 Python 后端（内部设置 NPC_DATA_BASE 再 import app）
                    // Chaquopy 规矩：任何 Python API 前必须先 start，绑定 Android 平台
                    if (!Python.isStarted()) {
                        Python.start(AndroidPlatform(applicationContext))
                    }
                    val launcher = Python.getInstance().getModule("npc_launcher")
                    val ok = launcher.callAttr("start", dataBase, PORT)
                    Log.i(TAG, "Flask started, ok=$ok")
                } catch (e: Throwable) {
                    Log.e(TAG, "Flask 启动失败: ${e.message}", e)
                    isRunning = false
                    stopForeground(STOP_FOREGROUND_REMOVE)
                    stopSelf()
                }
            }
        }
        updateNotification("NPC 服务运行中（端口 $PORT）")
    }

    private fun createChannel() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val channel = NotificationChannel(
            CHANNEL_ID,
            "NPC 服务",
            NotificationManager.IMPORTANCE_LOW
        ).apply { description = "本地 Flask 服务（聊天 + 记忆）" }
        nm.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification =
        android.app.Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("AI-NPC-Forgemind")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .setOngoing(true)
            .build()

    private fun updateNotification(text: String) {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.notify(NOTIFICATION_ID, buildNotification(text))
    }

    override fun onDestroy() {
        isRunning = false
        if (Python.isStarted()) {
            try {
                Python.getInstance().getModule("npc_launcher").callAttr("stop")
            } catch (e: Throwable) {
                Log.e(TAG, "停止 flask 失败: ${e.message}")
            }
        }
        super.onDestroy()
    }
}