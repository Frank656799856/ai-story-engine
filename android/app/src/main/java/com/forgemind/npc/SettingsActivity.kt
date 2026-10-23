package com.forgemind.npc

import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import java.io.File
import java.util.Properties

/** 配置页：读写 filesDir/config.txt（格式与 PC 版一致：KEY=VALUE，支持注释） */
class SettingsActivity : AppCompatActivity() {

    private lateinit var provider: EditText
    private lateinit var apiKey: EditText
    private lateinit var temperature: EditText
    private lateinit var maxTokens: EditText
    private lateinit var timeout: EditText
    private lateinit var cfgFile: File

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)

        cfgFile = AppPaths.ensureConfig(this)

        provider = findViewById(R.id.providerInput)
        apiKey = findViewById(R.id.apiKeyInput)
        temperature = findViewById(R.id.temperatureInput)
        maxTokens = findViewById(R.id.maxTokensInput)
        timeout = findViewById(R.id.timeoutInput)

        loadConfig()

        findViewById<Button>(R.id.saveButton).setOnClickListener { saveConfig() }
        findViewById<Button>(R.id.testButton).setOnClickListener { testConnection() }
    }

    private fun loadConfig() {
        val props = Properties()
        if (cfgFile.exists()) {
            cfgFile.inputStream().use { props.load(it) }
        }
        provider.setText(props.getProperty("PROVIDER", "deepseek"))
        apiKey.setText(props.getProperty("API_KEY", ""))
        temperature.setText(props.getProperty("TEMPERATURE", "0.8"))
        maxTokens.setText(props.getProperty("MAX_TOKENS", "4096"))
        timeout.setText(props.getProperty("TIMEOUT", "60"))
    }

    private fun saveConfig() {
        val sb = StringBuilder()
        sb.appendLine("# AI NPC ForgeMind 配置文件")
        sb.appendLine("PROVIDER=${provider.text?.trim()}")
        sb.appendLine("API_KEY=${apiKey.text?.trim()}")
        sb.appendLine("TEMPERATURE=${temperature.text?.trim()}")
        sb.appendLine("MAX_TOKENS=${maxTokens.text?.trim()}")
        sb.appendLine("TIMEOUT=${timeout.text?.trim()}")
        cfgFile.writeText(sb.toString())
        Toast.makeText(this, "已保存", Toast.LENGTH_SHORT).show()
    }

    private fun testConnection() {
        // 先保存，再用 Python 调一次最小 LLM 探测
        saveConfig()
        try {
            val py = com.chaquo.python.Python.getInstance()
            val ok = py.getModule("npc_launcher").callAttr(
                "test_connection",
                cfgFile.absolutePath
            )
            val okBool = (ok as? Boolean) ?: false
            Toast.makeText(this, if (okBool) "连接成功" else "连接失败（请检查 KEY/网络）", Toast.LENGTH_LONG).show()
        } catch (e: Exception) {
            Toast.makeText(this, "测试异常: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }
}