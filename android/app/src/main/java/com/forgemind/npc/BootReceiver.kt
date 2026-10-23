package com.forgemind.npc

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences

/** 开机自启（可选）：BOOT_COMPLETED 后若用户开启了自启开关，则启动服务 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            val prefs: SharedPreferences =
                context.getSharedPreferences("npc_prefs", Context.MODE_PRIVATE)
            if (prefs.getBoolean("auto_start", false)) {
                val start = Intent(context, NpcForegroundService::class.java).apply {
                    action = NpcForegroundService.ACTION_START
                }
                if (android.os.Build.VERSION.SDK_INT >= 26) {
                    context.startForegroundService(start)
                } else {
                    context.startService(start)
                }
            }
        }
    }
}