#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 模型调用核心模块（智能模型探测）

功能：
  1. 用户只需在 config.txt 中填写 PROVIDER 简称（如 deepseek / doubao / glm）
  2. 启动时自动探测该厂商下所有模型，找到第一个实际可用的
  3. 探测结果缓存到 data/model_cache.json，下次启动直接使用
  4. 如果 PROVIDER 变更，自动重新探测

支持的厂商简称：
  deepseek  — DeepSeek 系列
  doubao    — 字节豆包系列
  glm       — 智谱 GLM 系列
  openai    — OpenAI 系列
  qwen      — 通义千问系列
  moonshot  — Kimi 系列
  minimax   — MiniMax 系列
  yi        — 零一万物 Yi 系列
"""

import os
import json
import logging
import requests
import time
from pathlib import Path
from typing import Optional, Generator, NamedTuple

from agent import prompts as PROMPTS

logger = logging.getLogger("ai_client")

# ==================== 模型注册表 ====================

class ProviderInfo(NamedTuple):
    base: str
    models: list[str]
    description: str
    balance_url: str = ""  # 余额查询接口，空表示暂不支持


MODEL_REGISTRY: dict[str, ProviderInfo] = {
    "deepseek": ProviderInfo(
        base="https://api.deepseek.com/v1",
        models=["deepseek-chat", "deepseek-reasoner", "deepseek-v3", "deepseek-chat-v2"],
        description="DeepSeek 系列",
        balance_url="https://api.deepseek.com/user/balance",
    ),
    "doubao": ProviderInfo(
        base="https://ark.cn-beijing.volces.com/api/v3",
        models=["doubao-1.5-pro-32k", "doubao-1.5-lite-32k", "doubao-pro-32k", "doubao-pro-128k"],
        description="字节豆包系列",
    ),
    "glm": ProviderInfo(
        base="https://open.bigmodel.cn/api/paas/v4",
        models=[
            # 免费/低价层优先（确定可用，启动快速命中）
            "glm-4.5-flash", "glm-4-flash",
            # 主力层
            "glm-4.5-air", "glm-4.5",
            # 较新旗舰
            "glm-4.6", "glm-4.7",
            # 最新 5.x 系列（如已开通则自动升级使用）
            "glm-5-turbo", "glm-5.1", "glm-5.2", "glm-5.3",
        ],
        description="智谱 GLM 系列",
    ),
    "openai": ProviderInfo(
        base="https://api.openai.com/v1",
        models=["gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo"],
        description="OpenAI 系列",
    ),
    "qwen": ProviderInfo(
        base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        models=["qwen-turbo", "qwen-plus", "qwen-max", "qwen-long"],
        description="通义千问系列",
    ),
    "moonshot": ProviderInfo(
        base="https://api.moonshot.cn/v1",
        models=["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        description="Kimi 系列",
    ),
    "minimax": ProviderInfo(
        base="https://api.minimax.chat/v1",
        models=["abab6.5s-chat", "abab6.5t-chat"],
        description="MiniMax 系列",
    ),
    "yi": ProviderInfo(
        base="https://api.lingyiwanwu.com/v1",
        models=["yi-large", "yi-medium", "yi-vision"],
        description="零一万物 Yi 系列",
    ),
}


# ==================== 配置加载 ====================

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_DIR / "config.txt"
CACHE_FILE = PROJECT_DIR / "data" / "model_cache.json"

# 全局配置变量
API_KEY = ""
PROVIDER = ""
TEMPERATURE = 0.8
MAX_TOKENS = 4096
TIMEOUT = 60

# 探测结果（运行时确定）
ACTIVE_MODEL = ""
ACTIVE_BASE_URL = ""


def load_config():
    """从 config.txt 加载配置"""
    global API_KEY, PROVIDER, TEMPERATURE, MAX_TOKENS, TIMEOUT

    if not CONFIG_FILE.exists():
        logger.warning(f"配置文件 {CONFIG_FILE} 不存在")
        return

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k == "API_KEY":
                    API_KEY = v
                elif k == "PROVIDER":
                    PROVIDER = v.lower()
                elif k == "TEMPERATURE":
                    TEMPERATURE = float(v)
                elif k == "MAX_TOKENS":
                    MAX_TOKENS = int(v)
                elif k == "TIMEOUT":
                    TIMEOUT = int(v)

    logger.info(f"配置加载: PROVIDER={PROVIDER}, API_KEY={'***' + API_KEY[-6:] if API_KEY and len(API_KEY)>6 else '未设置'}")


# ==================== 模型探测 ====================

def probe_model(base_url: str, api_key: str, model_name: str, timeout: int = 10) -> bool:
    """
    探测单个模型是否可用。

    发送一条极简消息，HTTP 200 即视为成功。
    """
    url = f"{base_url}/chat/completions"
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code == 200:
            return True
        else:
            err = ""
            try:
                err = resp.json().get("error", {}).get("message", resp.text[:100])
            except Exception:
                err = resp.text[:100]
            logger.info(f"  ✗ {model_name}: {resp.status_code} {err}")
            return False
    except requests.exceptions.Timeout:
        logger.info(f"  ✗ {model_name}: 超时")
        return False
    except Exception as e:
        logger.info(f"  ✗ {model_name}: {e}")
        return False


def auto_detect(force: bool = False) -> tuple[str, str]:
    """
    自动探测可用的模型。

    探测顺序：
      1. 检查缓存 — 如果 PROVIDER 没变且缓存有效，直接返回
      2. force=True 时跳过缓存，重新探测
      3. 按注册表顺序逐个探测模型，找到第一个可用的

    Returns:
        (model_name, base_url) 元组

    Raises:
        RuntimeError: 所有模型都不可用
    """
    global ACTIVE_MODEL, ACTIVE_BASE_URL

    if not PROVIDER:
        raise RuntimeError("未配置 PROVIDER，请在 config.txt 中设置")
    if not API_KEY or API_KEY == "your-api-key-here":
        raise RuntimeError("未配置 API_KEY，请在 config.txt 中设置")

    provider = MODEL_REGISTRY.get(PROVIDER)
    if provider is None:
        available = ", ".join(MODEL_REGISTRY.keys())
        raise RuntimeError(f"不支持的厂商: {PROVIDER}，可用: {available}")

    # 检查缓存
    if not force and CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("provider") == PROVIDER and cache.get("api_key_hash") == _hash_key(API_KEY):
                cached_model = cache.get("model")
                cached_base = cache.get("base_url")
                # 快速验证缓存是否仍然有效
                if cached_model and cached_base:
                    if probe_model(cached_base, API_KEY, cached_model, timeout=5):
                        ACTIVE_MODEL = cached_model
                        ACTIVE_BASE_URL = cached_base
                        logger.info(f"✅ 缓存命中: {PROVIDER}/{ACTIVE_MODEL}")
                        return ACTIVE_MODEL, ACTIVE_BASE_URL
                    else:
                        logger.info("缓存模型已失效，重新探测...")
        except Exception:
            pass

    # 开始探测
    logger.info(f"🔍 开始探测 {PROVIDER} ({provider.description}) 的可用模型...")
    models = provider.models

    for i, model in enumerate(models, 1):
        logger.info(f"  [{i}/{len(models)}] 测试: {model}...")
        if probe_model(provider.base, API_KEY, model, timeout=TIMEOUT):
            ACTIVE_MODEL = model
            ACTIVE_BASE_URL = provider.base
            logger.info(f"✅ 探测成功: {PROVIDER}/{model}")

            # 写入缓存
            _save_cache()
            return model, provider.base

    raise RuntimeError(
        f"❌ {PROVIDER} ({provider.description}) 下所有模型都不可用：\n"
        f"   已测试: {', '.join(models)}\n"
        f"   请检查 API_KEY 是否正确，或尝试更换 PROVIDER"
    )


def _save_cache():
    """保存探测结果到缓存文件"""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache = {
        "provider": PROVIDER,
        "api_key_hash": _hash_key(API_KEY),
        "model": ACTIVE_MODEL,
        "base_url": ACTIVE_BASE_URL,
        "detected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _hash_key(key: str) -> str:
    """对 API Key 做简单哈希，用于缓存校验"""
    import hashlib
    return hashlib.md5(key.encode()).hexdigest()[:8]


def get_registry_info() -> dict:
    """获取注册表信息（供 UI 展示）"""
    return {
        "providers": list(MODEL_REGISTRY.keys()),
        "active": {"provider": PROVIDER, "model": ACTIVE_MODEL, "base": ACTIVE_BASE_URL},
        "details": {
            name: {"description": info.description, "models": info.models}
            for name, info in MODEL_REGISTRY.items()
        },
    }


# ==================== 余额查询 ====================

def query_balance(provider_name: str = None) -> dict:
    """
    查询指定厂商的账户余额。

    目前支持：
      - deepseek: GET https://api.deepseek.com/user/balance

    Args:
        provider_name: 厂商简称，默认使用配置中的 PROVIDER

    Returns:
        {
            "provider": "deepseek",
            "is_available": bool,
            "currency": "CNY",
            "total_balance": "110.00",
            "granted_balance": "10.00",
            "topped_up_balance": "100.00",
        }

    Raises:
        RuntimeError: 厂商不支持余额查询、未配置 API_KEY 或请求失败
    """
    provider_name = (provider_name or PROVIDER).lower()
    provider = MODEL_REGISTRY.get(provider_name)
    if provider is None:
        available = ", ".join(MODEL_REGISTRY.keys())
        raise RuntimeError(f"不支持的厂商: {provider_name}，可用: {available}")

    if not provider.balance_url:
        raise RuntimeError(f"{provider_name} 暂不支持余额查询")

    key = API_KEY
    if not key or key == "your-api-key-here":
        raise RuntimeError("未配置 API_KEY，请在 config.txt 中设置")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.get(provider.balance_url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as e:
        err = ""
        try:
            err = e.response.json().get("error", {}).get("message", e.response.text[:100])
        except Exception:
            err = e.response.text[:100]
        raise RuntimeError(f"余额查询失败 ({e.response.status_code}): {err}")
    except requests.exceptions.Timeout:
        raise RuntimeError("余额查询超时")
    except Exception as e:
        raise RuntimeError(f"余额查询失败: {e}")

    return _parse_balance(provider_name, data)


def _parse_balance(provider_name: str, data: dict) -> dict:
    """解析不同厂商的余额返回格式，统一为标准结构"""
    if provider_name == "deepseek":
        infos = data.get("balance_infos", [])
        info = infos[0] if infos else {}
        return {
            "provider": provider_name,
            "is_available": data.get("is_available", False),
            "currency": info.get("currency", "CNY"),
            "total_balance": info.get("total_balance", "0.00"),
            "granted_balance": info.get("granted_balance", "0.00"),
            "topped_up_balance": info.get("topped_up_balance", "0.00"),
        }

    # 未适配的厂商，原样返回
    return {"provider": provider_name, "raw": data}


def format_balance(result: dict) -> str:
    """把余额结果格式化为可读文本"""
    if "raw" in result:
        return json.dumps(result["raw"], ensure_ascii=False, indent=2)

    currency = result.get("currency", "CNY")
    unit = {"CNY": "元", "USD": "USD"}.get(currency, currency)
    return (
        f"{result['provider']} 余额：\n"
        f"  可用状态: {'正常' if result.get('is_available') else '不可用'}\n"
        f"  总余额:   {result.get('total_balance', '0.00')} {unit}\n"
        f"  赠送余额: {result.get('granted_balance', '0.00')} {unit}\n"
        f"  充值余额: {result.get('topped_up_balance', '0.00')} {unit}"
    )


# ==================== Token 统计 ====================

class TokenStats:
    def __init__(self):
        self.total_calls = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0
        self.cache_hit_tokens = 0
        self.cache_miss_tokens = 0

    def update(self, usage: dict):
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        total = usage.get("total_tokens", 0)
        # 厂商侧上下文缓存命中统计（DeepSeek 等提供；无此字段时按 0 处理）
        hit = usage.get("prompt_cache_hit_tokens", 0) or 0
        miss = usage.get("prompt_cache_miss_tokens", 0) or 0
        self.total_calls += 1
        self.total_prompt_tokens += prompt
        self.total_completion_tokens += completion
        self.total_tokens += total
        self.cache_hit_tokens += hit
        self.cache_miss_tokens += miss
        logger.info(
            f"📊 API #{self.total_calls}: "
            f"入={prompt} 出={completion} 总={total} | "
            f"缓存命中={hit} 未命中={miss} | 累计={self.total_tokens}"
        )

    def get_summary(self) -> dict:
        hit = self.cache_hit_tokens
        miss = self.cache_miss_tokens
        denom = hit + miss
        return {
            "total_calls": self.total_calls,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_hit_tokens": hit,
            "cache_miss_tokens": miss,
            "cache_hit_rate": round(hit / denom, 4) if denom else 0.0,
        }


token_stats = TokenStats()


# ==================== AI 调用核心 ====================

class AIClient:
    """AI 模型调用客户端"""

    def __init__(
        self,
        api_key: str = None,
        model_name: str = None,
        base_url: str = None,
        temperature: float = None,
        max_tokens: int = None,
        timeout: int = None,
    ):
        self.api_key = api_key or API_KEY
        self.model_name = model_name or ACTIVE_MODEL
        self.base_url = base_url or ACTIVE_BASE_URL
        self.temperature = temperature if temperature is not None else TEMPERATURE
        self.max_tokens = max_tokens or MAX_TOKENS
        self.timeout = timeout or TIMEOUT
        # 客户端节流：同一实例连续请求间的最小间隔（秒），避免 A1~A4 爆发式连打触发厂商 QPM 限流
        self.min_request_interval = 0.5
        self._last_request_time = 0.0

    def _throttle(self):
        """在请求发出前强制满足最小间隔，平滑多 Agent 连打带来的瞬时请求峰值"""
        now = time.time()
        gap = now - self._last_request_time
        if self._last_request_time and gap < self.min_request_interval:
            time.sleep(self.min_request_interval - gap)
        self._last_request_time = time.time()

    @property
    def api_url(self):
        return self.base_url

    def _build_payload(self, messages: list[dict], temperature: float = None, max_tokens: int = None, stream: bool = False, frequency_penalty: float = None) -> dict:
        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": stream,
        }
        # 频率惩罚：抑制重复用词（A4 摘要等需要凝练的场景使用）；不传则不附加该字段
        if frequency_penalty is not None:
            payload["frequency_penalty"] = frequency_penalty
        return payload

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def chat(
        self,
        system_prompt: str = "",
        user_message: str = "",
        messages: list[dict] = None,
        temperature: float = None,
        max_tokens: int = None,
        cache_pad: bool = False,
        frequency_penalty: float = None,
    ) -> str:
        # 仅当 cache_pad=True 时前置缓存填充块，使固定前缀超过厂商缓存可靠命中线（约 1024+ token）。
        # CACHE_PAD 是字节级固定的占位文本（带"忽略"提示），不影响输出质量。
        if system_prompt and cache_pad:
            system_prompt = PROMPTS.CACHE_PAD + system_prompt

        if messages is None:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_message})

        payload = self._build_payload(messages, temperature, max_tokens, stream=False, frequency_penalty=frequency_penalty)
        url = f"{self.base_url}/chat/completions"

        t0 = time.time()
        self._throttle()
        # 限流(429)/服务端错误(5xx) 指数退避重试；超时/连接重置也各重试一次
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(url, headers=self._build_headers(), json=payload, timeout=self.timeout)
                response.raise_for_status()
                result = response.json()
                content = result["choices"][0]["message"]["content"].strip()
                if "usage" in result:
                    token_stats.update(result["usage"])
                elapsed = (time.time() - t0) * 1000
                self._log_call(messages, content, elapsed)
                _record_trace(messages, content, elapsed)
                return content
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                # 400/401/403/404 为业务/鉴权类错误，重试也不会成功，直接抛出
                if status in (400, 401, 403, 404):
                    raise RuntimeError(f"HTTP错误({status}): {e}")
                # 429 限流 / 5xx 服务端错误：按指数退避重试，缓解 GLM 等免费层激进的 QPM 限流
                wait = 1 << (attempt - 1)  # 1s, 2s, 4s
                if attempt < max_attempts:
                    logger.warning(
                        f"HTTP {status}，第 {attempt}/{max_attempts} 次，{wait}s 后重试..."
                    )
                    time.sleep(wait)
                    continue
                error_msg = f"HTTP错误: {e}"
                try:
                    error_detail = e.response.json()
                    if isinstance(error_detail, dict) and "error" in error_detail:
                        error_msg += f" | {error_detail['error'].get('message', '')}"
                except Exception:
                    pass
                logger.error(error_msg)
                raise RuntimeError(error_msg)
            except requests.exceptions.Timeout:
                if attempt < max_attempts:
                    logger.warning(f"API调用超时 ({self.timeout}s)，第 {attempt}/{max_attempts} 次，重试中...")
                    time.sleep(1)
                    continue
                raise RuntimeError(f"API调用超时 ({self.timeout}s)，已重试 {max_attempts} 次")
            except requests.exceptions.ConnectionError as e:
                if attempt < max_attempts:
                    logger.warning(f"连接错误: {e}，第 {attempt}/{max_attempts} 次，重试中...")
                    time.sleep(1)
                    continue
                raise RuntimeError(f"连接错误: {str(e)}")
            except Exception as e:
                raise RuntimeError(f"API调用失败: {str(e)}")

    def _log_call(self, messages: list[dict], content: str, elapsed_ms: float):
        """8.24b 全局文件日志埋点：每次 LLM 调用记录耗时/出入规模/调用方线索。

        调用方通过系统提示词前 50 字识别（各 Agent 的 system prompt 开头各不相同）；
        用户消息取末尾 80 字（最接近玩家本轮输入）。失败路径已有 warning/error 落盘。
        """
        try:
            sys_head = ""
            for m in messages:
                if m.get("role") == "system":
                    sys_head = str(m.get("content", ""))[:50].replace("\n", " ")
                    break
            user_tail = ""
            in_chars = 0
            for m in reversed(messages):
                c = str(m.get("content", ""))
                in_chars += len(c)
                if m.get("role") == "user" and not user_tail:
                    user_tail = c[-80:].replace("\n", " ")
            logger.info(
                f"🤖 LLM调用完成: 模型={self.model_name} 耗时={elapsed_ms:.0f}ms "
                f"入={in_chars}字 出={len(content)}字 | 系统[{sys_head}] | 用户尾[{user_tail}]"
            )
        except Exception:
            pass  # 日志埋点失败不影响主流程

    def chat_with_tools(
        self,
        system_prompt: str = "",
        user_message: str = "",
        tools: list = None,
        tool_choice: str = "auto",
        temperature: float = None,
        max_tokens: int = None,
    ) -> tuple:
        """8.24f：带 function calling 的调用（OpenAI 兼容协议）。

        返回 (content, tool_calls)：
          content     助手正文（可能为空串——模型可以只调工具不说话）
          tool_calls  [{"name": 工具名, "arguments": dict}, ...]
                      arguments 已 json.loads；解析失败时保留原始字符串交调用方容错
        仅剧情状态总分析（A4 打标协议化）使用；现有 chat()/chat_stream() 一行未动，
        普通身份调用零影响。重试策略与 chat() 一致（429/5xx 指数退避 + 超时/连接重置重试）。
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})

        payload = self._build_payload(messages, temperature, max_tokens, stream=False)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        url = f"{self.base_url}/chat/completions"

        t0 = time.time()
        self._throttle()
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(url, headers=self._build_headers(), json=payload, timeout=self.timeout)
                response.raise_for_status()
                result = response.json()
                message = result["choices"][0]["message"]
                content = (message.get("content") or "").strip()
                calls = []
                for tc in message.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    raw_args = fn.get("arguments", "")
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args else {}
                    except json.JSONDecodeError:
                        args = raw_args  # arguments 不是合法 JSON → 保留原文交调用方容错
                    calls.append({"name": fn.get("name", ""), "arguments": args})
                if "usage" in result:
                    token_stats.update(result["usage"])
                elapsed = (time.time() - t0) * 1000
                self._log_call(messages, content + f" [tools={len(calls)}]", elapsed)
                _record_trace(messages, content, elapsed)
                return content, calls
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if status in (400, 401, 403, 404):
                    raise RuntimeError(f"HTTP错误({status}): {e}")
                wait = 1 << (attempt - 1)
                if attempt < max_attempts:
                    logger.warning(f"chat_with_tools HTTP {status}，第 {attempt}/{max_attempts} 次，{wait}s 后重试...")
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"HTTP错误: {e}")
            except requests.exceptions.Timeout:
                if attempt < max_attempts:
                    logger.warning(f"chat_with_tools 超时，第 {attempt}/{max_attempts} 次，重试中...")
                    time.sleep(1)
                    continue
                raise RuntimeError(f"API调用超时 ({self.timeout}s)")
            except requests.exceptions.ConnectionError as e:
                if attempt < max_attempts:
                    logger.warning(f"chat_with_tools 连接错误，第 {attempt}/{max_attempts} 次，重试中...")
                    time.sleep(1)
                    continue
                raise RuntimeError(f"连接错误: {str(e)}")

    def chat_stream(
        self,
        system_prompt: str = "",
        user_message: str = "",
        messages: list[dict] = None,
        temperature: float = None,
        max_tokens: int = None,
        cache_pad: bool = False,
    ) -> Generator[str, None, None]:
        # 仅当 cache_pad=True 时前置缓存填充块（与 chat() 保持一致）
        if system_prompt and cache_pad:
            system_prompt = PROMPTS.CACHE_PAD + system_prompt

        if messages is None:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_message})

        payload = self._build_payload(messages, temperature, max_tokens, stream=True)
        url = f"{self.base_url}/chat/completions"

        self._throttle()
        try:
            in_chars = sum(len(str(m.get("content", ""))) for m in messages)
            logger.info(f"🤖 LLM流式调用开始: 模型={self.model_name} 入={in_chars}字")
            response = requests.post(url, headers=self._build_headers(), json=payload, timeout=self.timeout, stream=True)
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8")
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except json.JSONDecodeError:
                        continue
        except requests.exceptions.HTTPError as e:
            logger.error(f"流式HTTP错误: {e}")
            raise RuntimeError(f"流式调用失败: {e}")
        except Exception as e:
            raise RuntimeError(f"流式调用失败: {str(e)}")

    def chat_json(
        self,
        system_prompt: str = "",
        user_message: str = "",
        messages: list[dict] = None,
        temperature: float = None,
        max_tokens: int = None,
        retries: int = 2,
    ) -> dict:
        for attempt in range(retries + 1):
            try:
                if attempt == 0:
                    text = self.chat(system_prompt, user_message, messages, temperature, max_tokens)
                else:
                    retry_msg = user_message + "\n\n请务必只输出合法的JSON格式，不要包含任何其他文字。"
                    text = self.chat(system_prompt, retry_msg, messages, temperature, max_tokens)
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    pass
                import re
                match = re.search(r'\{[\s\S]*\}', text)
                if match:
                    return json.loads(match.group())
                if attempt < retries:
                    logger.warning(f"JSON解析失败，重试 {attempt+1}/{retries}")
                else:
                    logger.error(f"JSON解析最终失败: {text[:200]}")
                    return {"_parse_error": True, "raw": text}
            except RuntimeError:
                raise
        return {"_parse_error": True, "raw": ""}

    def get_balance(self, provider_name: str = None) -> dict:
        """便捷方法：查询当前（或指定）厂商的账户余额"""
        return query_balance(provider_name)


# ==================== 初始化 ====================

# 启动时加载配置
load_config()

# 如果有 PROVIDER，自动探测
if PROVIDER and API_KEY and API_KEY != "your-api-key-here":
    try:
        auto_detect()
    except RuntimeError as e:
        logger.warning(f"模型探测失败: {e}")
        logger.warning("系统将以降级模式运行，AI 调用将返回 Mock 回复")
else:
    logger.info("PROVIDER 或 API_KEY 未配置，跳过模型探测")

# 全局客户端实例
client = AIClient()


# ==================== 追踪钩子 ====================

def _record_trace(messages: list[dict], output: str, elapsed_ms: float):
    """记录 LLM 调用到追踪系统"""
    try:
        from agent.trace import trace
        # 提取最后一个 user message 作为输入摘要
        user_msgs = [m["content"] for m in messages if m.get("role") == "user"]
        input_summary = user_msgs[-1] if user_msgs else ""
        trace.add_llm(
            agent="LLM",
            step="chat",
            llm_input=input_summary[:800],
            llm_output=output[:800],
            llm_type="chat",
            llm_time_ms=elapsed_ms,
        )
    except Exception:
        pass  # 追踪失败不影响主流程


# ==================== 命令行便捷入口 ====================

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    provider = sys.argv[1] if len(sys.argv) > 1 else PROVIDER
    try:
        result = query_balance(provider)
        print(format_balance(result))
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)