#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
链接内容读取工具

原多引擎 Web 搜索功能已关闭（无法获取厂商搜索 API）。
现在仅当用户在消息中提供了确切的链接时，才提取链接并读取网页内容供 LLM 阅读；
否则不触发任何网络请求。

流程：
  1. 从用户消息中完整提取链接（支持裸链接与 Markdown 链接 [文字](url)）
  2. 逐个读取链接内容，提取可读文本
  3. 格式化为 LLM 可读文本

依赖: pip install requests beautifulsoup4
"""

import logging
import re

logger = logging.getLogger("web_search")

# 裸链接：http/https 开头的连续非空白、非成对闭合符字符
URL_PATTERN = re.compile(
    r'https?://[^\s<>"\'\)\]\}，。；、！？,;]+',
    re.IGNORECASE,
)
# Markdown 链接：[文字](https://...)
MARKDOWN_LINK_PATTERN = re.compile(
    r'\[[^\]]*\]\((https?://[^)\s]+)\)',
    re.IGNORECASE,
)


class WebSearchTool:
    """链接内容读取工具（原 Web 搜索已关闭）"""

    def __init__(self, timeout: float = 15.0, max_chars: int = 4000, max_links: int = 3):
        self.timeout = timeout          # 单次请求超时（秒）
        self.max_chars = max_chars      # 每个链接最多保留的文本长度
        self.max_links = max_links      # 最多读取的链接数

    def extract_links(self, text: str) -> list[str]:
        """从文本中完整提取链接列表（去重、保序）。

        优先匹配 Markdown 链接 [文字](url)，再匹配裸链接。
        """
        if not text:
            return []

        links: list[str] = []
        seen: set[str] = set()

        def add(url: str):
            url = url.strip().rstrip(".,;!?，。；！？)")
            if url and url not in seen:
                seen.add(url)
                links.append(url)

        # ① Markdown 链接
        for m in MARKDOWN_LINK_PATTERN.finditer(text):
            add(m.group(1))
        # ② 裸链接
        for m in URL_PATTERN.finditer(text):
            add(m.group(0))

        return links[: self.max_links]

    def fetch_content(self, url: str) -> dict:
        """读取单个链接的内容。

        Returns:
            {"url": ..., "title": ..., "text": ...}
        """
        import requests
        from bs4 import BeautifulSoup

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"

        soup = BeautifulSoup(resp.text, "html.parser")

        # 移除脚本/样式/导航等无关标签
        for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside"]):
            tag.decompose()

        title = soup.title.get_text(strip=True) if soup.title else ""
        text = soup.get_text("\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text[: self.max_chars]

        return {"url": url, "title": title, "text": text}

    def read_links(self, text: str) -> tuple:
        """提取消息中的链接并读取内容。

        Returns:
            (results: list[dict], llm_text: str)
            - 消息中无链接时返回 ([], "")
        """
        links = self.extract_links(text)
        if not links:
            return [], ""

        results = []
        for url in links:
            try:
                results.append(self.fetch_content(url))
                logger.info(f"链接内容读取成功: {url}")
            except Exception as e:
                logger.warning(f"链接内容读取失败 {url}: {e}")
                results.append({"url": url, "title": "", "text": f"（链接内容读取失败: {e}）"})

        lines = []
        for i, r in enumerate(results, 1):
            title = r.get("title") or r.get("url", "")
            lines.append(f"{i}. **{title}**\n"
                         f"   {r.get('url', '')}\n"
                         f"   > {r.get('text', '')[:3000]}\n")
        return results, "\n".join(lines)

    def search(self, query: str) -> list:
        """兼容占位：原搜索已关闭，仅支持链接内容读取。"""
        results, _ = self.read_links(query)
        return results


# 全局实例
web_search = WebSearchTool()
