package com.forgemind.npc

import android.content.Context
import java.io.File

/**
 * 统一计算数据根目录（filesDir）并解包打包在 APK assets 里的静态资源。
 *
 * 数据布局（与 PC 版 pro_AI2/data 对齐）：
 *   filesDir/
 *     config.txt          ← NPC_DATA_BASE 指向 filesDir，config.txt 落在根
 *     data/
 *       identities.json
 *       memory/{身份}/…   （core/episodic/working + index.json + history.json）
 *       game_db/{身份}.json
 *       npc_state/npc_state.json
 *     static/             ← 从 assets/static 解包，供 Flask 托管网页 UI
 *
 * Kotlin 启动 Python 前把 NPC_DATA_BASE 设为 filesDir 绝对路径，
 * 后端所有 data_paths 的读/写都会落到下面，与 PC 版彻底隔离。
 */
object AppPaths {

    const val ENV_NPC_DATA_BASE = "NPC_DATA_BASE"
    const val ASSETS_STATIC = "static"
    const val ASSETS_CONFIG = "config_default.txt"

    /** 数据根目录 = context.filesDir */
    fun dataRoot(context: Context): File = context.filesDir

    /** 是否已解包静态资源（幂等：只解一次） */
    fun isStaticUnpacked(context: Context): Boolean {
        return File(dataRoot(context), "static/index.html").exists()
    }

    /**
     * 把 assets/static 整个目录递归解包到 filesDir/static。
     * assets 目录与 filesDir 可能分布在不同的物理分区/存储，直接读取 assets 逐个写出。
     */
    fun unpackStatic(context: Context): File {
        val destRoot = File(dataRoot(context), ASSETS_STATIC)
        if (isStaticUnpacked(context)) return destRoot
        val assets = context.assets
        try {
            val names = assets.list(ASSETS_STATIC) ?: emptyArray()
            for (name in names) {
                unpackAssetDir(assets, ASSETS_STATIC + "/" + name, destRoot)
            }
        } catch (e: Exception) {
            android.util.Log.e("AppPaths", "解包 static 失败: ${e.message}")
        }
        return destRoot
    }

    private fun unpackAssetDir(assets: android.content.res.AssetManager, path: String, destDir: File) {
        val rel = path.removePrefix(ASSETS_STATIC + "/")
        // 父目录：顶层文件（无"/"）直接落 destDir，否则落到对应子目录
        val parent = rel.substringBeforeLast("/", "")
        val target = if (parent.isEmpty()) destDir else File(destDir, parent)
        val fileName = rel.substringAfterLast("/")
        val children = assets.list(path)
        if (children != null && children.isNotEmpty()) {
            // 目录
            val dir = File(target, fileName)
            dir.mkdirs()
            for (child in children) {
                unpackAssetDir(assets, path + "/" + child, dir)
            }
        } else {
            // 文件（target 已是 File，直接 mkdirs；顶层文件也落 destDir 本身）
            target.mkdirs()
            val out = File(target, fileName)
            assets.open(path).use { input ->
                out.outputStream().use { output -> input.copyTo(output) }
            }
        }
    }

    /**
     * 首次启动时生成 config.txt（若不存在）。默认模板与 PC 版 config.txt 一致——
     * 优先复制 APK 内 assets/config_default.txt（配置页也会更新它），否则写内置默认。
     */
    fun ensureConfig(context: Context): File {
        val cfg = File(dataRoot(context), "config.txt")
        if (cfg.exists()) return cfg
        val defaults = buildString {
            appendLine("# AI NPC ForgeMind 配置文件")
            appendLine("# 在设置页修改并保存，服务会读取此文件")
            appendLine("# 支持的厂商简称：deepseek / doubao / glm / openai / qwen / moonshot / minimax / yi")
            appendLine("PROVIDER=deepseek")
            appendLine("# API Key（必填）")
            appendLine("API_KEY=")
            appendLine("TEMPERATURE=0.8")
            appendLine("MAX_TOKENS=4096")
            appendLine("TIMEOUT=60")
        }
        cfg.writeText(defaults)
        return cfg
    }
}