#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MIT License
#
# Copyright (c) 2026 3MF Manager Contributors
# SPDX-License-Identifier: MIT
# See LICENSE file for full license text.
"""LLM 客户端（OpenAI 兼容接口）+ 多轮对话上下文管理。

支持任意 OpenAI 兼容的 API（本地 Ollama/vLLM、OpenAI、Azure、国产模型等）。
通过配置：base_url + api_key + model。
"""
import json, os, re
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")

DEFAULT_CONFIG = {
    "llm": {
        "base_url": "",      # 例如 http://127.0.0.1:11434/v1 或 https://api.openai.com/v1
        "api_key": "",
        "model": "",
    },
    "paths": {
        "library_root": os.path.join(os.path.expanduser("~"), "Downloads", "3mf_data"),
    },
}


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        # 兜底合并默认
        for k, v in DEFAULT_CONFIG.items():
            data.setdefault(k, v)
        for k, v in DEFAULT_CONFIG["llm"].items():
            data["llm"].setdefault(k, v)
        for k, v in DEFAULT_CONFIG["paths"].items():
            data["paths"].setdefault(k, v)
        return data
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(data):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def llm_configured():
    c = load_config()["llm"]
    return bool(c.get("base_url") and c.get("model"))


def chat(messages, temperature=0.3, max_tokens=2000):
    """调用 LLM，返回文本。messages: [{"role":..,"content":..}]"""
    c = load_config()["llm"]
    if not c.get("base_url") or not c.get("model"):
        raise RuntimeError("LLM 未配置，请在设置中填写模型地址与模型名")
    base = c["base_url"].rstrip("/")
    url = base + "/chat/completions"
    payload = {
        "model": c["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if c.get("api_key"):
        headers["Authorization"] = f"Bearer {c['api_key']}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise RuntimeError("LLM 返回格式异常: " + json.dumps(body)[:200])


def extract_json(text):
    """从 LLM 输出中稳健提取 JSON。"""
    text = text.strip()
    # 去掉代码围栏
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    # 找到第一个 { 到最后一个 }
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end+1])
        except Exception:
            pass
    raise ValueError("无法从 LLM 输出解析 JSON: " + text[:200])


# 简易对话会话存储（进程内；重启丢失，可持久化扩展）
_sessions = {}

def session_get(sid):
    return _sessions.get(sid, [])

def session_add(sid, role, content, maxlen=30):
    _sessions.setdefault(sid, [])
    _sessions[sid].append({"role": role, "content": content})
    if len(_sessions[sid]) > maxlen:
        _sessions[sid] = _sessions[sid][-maxlen:]

def session_clear(sid):
    _sessions.pop(sid, None)
