#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安卓端 Flask 启动器（与 PC 版 app.py 解耦的独立入口，仅安卓 Chaquopy 使用）

职责：
  1. 在 import 任何后端模块【之前】设置 NPC_DATA_BASE 环境变量，
     使 store/index/game_db/npc_state/ai_client 等把数据根重定向到 filesDir。
  2. import 后端 app（app_module 暴露 Flask 实例 `app`）。
  3. 启动 Flask，监听 127.0.0.1:{port}。

Kotlin 侧通过 com.chaquo.python.Python 调用 start()/stop()。
"""

import os
import sys
import threading

_module = None
_thread = None
_stop_event = threading.Event()


def start(data_base: str, port: int = 5003):
    """由 Kotlin 调用：设置数据根并启动 Flask"""
    global _module, _thread

    os.environ["NPC_DATA_BASE"] = data_base
    os.environ["FLASK_RUN_PORT"] = str(port)

    # 确保 Flutter/app.py 所在目录（android/app/src/main/python）在 sys.path
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    # 延迟 import 后端 app（此刻环境变量已就位）
    if _module is None:
        _module = __import__("app")  # 拿到 app 模块（内含 Flask 实例 `app`）

    if _thread is None or not _thread.is_alive():
        _stop_event.clear()
        _thread = threading.Thread(target=_run, args=(port,), daemon=True)
        _thread.start()

    return True


def _run(port: int):
    flask_app = getattr(_module, "app")
    try:
        # host=127.0.0.1：仅手机本机浏览器可访问，安全
        flask_app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("npc_launcher").error(f"Flask 异常退出: {e}")


def stop():
    """由 Kotlin 调用：停止 Flask。app.run 无法直接中断，依赖线程 daemon + 服务销毁时回收。"""
    global _thread
    _stop_event.set()
    if _module is not None:
        # 尝试关闭 werkzeug 服务器（尽力而为，不保证立即生效）
        try:
            server = getattr(_module.app, "_server", None)
            if server is not None:
                server.shutdown()
        except Exception:  # noqa: BLE001
            pass
    return True


def test_connection(config_path: str) -> bool:
    """配置页「测试连接」：仅探测模型可用性，不发训练数据泄漏。"""
    try:
        import os
        os.environ.setdefault("NPC_DATA_BASE", os.path.dirname(os.path.abspath(config_path)))
        from data_paths import CONFIG_FILE
        if CONFIG_FILE != os.path.abspath(config_path):
            # 让 ai_client 从用户刚保存的 config 重新加载
            pass
        from agent.ai_client import client, load_config
        load_config()
        # 最小探测：发一次空内容短请求
        result = client.chat(
            system_prompt="你是连接测试。只回复：ok",
            user_message="ping",
            max_tokens=8,
        )
        return bool(result and "ok" in (result or "").lower())
    except Exception:  # noqa: BLE001
        return False