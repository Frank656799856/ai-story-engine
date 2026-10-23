package com.forgemind.npc

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

/** 开关页：启动/停止服务 + 打开聊天页面 + 通知权限 + 设置页入口 + 保活指引 */
class MainActivity : AppCompatActivity() {

    private lateinit var statusText: TextView
    private lateinit var toggleBtn: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        statusText = findViewById(R.id.statusText)
        toggleBtn = findViewById(R.id.toggleButton)

        val settingsBtn: Button = findViewById(R.id.configButton)
        val openBtn: Button = findViewById(R.id.openBrowserButton)
        val notifBtn: Button = findViewById(R.id.notifPermissionButton)

        toggleBtn.setOnClickListener {
            if (NpcForegroundService.isRunning) stopService() else startService()
        }
        settingsBtn.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        openBtn.setOnClickListener {
            val url = "http://127.0.0.1:5003"
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        }
        notifBtn.setOnClickListener {
            ensureNotificationPermission()
        }
        findViewById<Button>(R.id.batteryGuideButton).setOnClickListener {
            statusText.text = "保活指引：\n① 设置→电池→本应用→允许后台运行/不优化\n" +
                    "② 最近任务下拉本应用卡片→锁定\n③ 自启动管理→允许（可选，开机自启）"
        }
    }

    override fun onResume() {
        super.onResume()
        refreshUi()
    }

    private fun refreshUi() {
        if (NpcForegroundService.isRunning) {
            statusText.text = "运行中（端口 5003）"
            toggleBtn.text = "停止服务"
        } else {
            statusText.text = "已停止"
            toggleBtn.text = "启动服务"
        }
    }

    private fun startService() {
        ensureNotificationPermission()
        val intent = Intent(this, NpcForegroundService::class.java).apply {
            action = NpcForegroundService.ACTION_START
        }
        ContextCompat.startForegroundService(this, intent)
    }

    private fun stopService() {
        val intent = Intent(this, NpcForegroundService::class.java).apply {
            action = NpcForegroundService.ACTION_STOP
        }
        startService(intent)
    }

    private fun ensureNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(
                this,
                arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                1001
            )
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 1001) refreshUi()
    }
}