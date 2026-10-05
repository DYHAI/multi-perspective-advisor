#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多视角人生参谋 · Multi-Perspective Advisor

一个本地运行的小工具：
  遭遇（过往与当下） / 问题（想要解决） / 未来的期待  —— 三项都可以留空任意部分
  -> 多个不同厂商的大模型，各自以不同「视角」独立作答
  -> 一个汇总模型收集所有意见，输出关系分析 + 3~5 条建议 + 最终多视角意见

零第三方依赖，只用 Python 标准库。运行： python3 app.py
"""

from __future__ import annotations

import json
import hashlib
import hmac
import html as html_mod
import mimetypes
import os
import queue
import secrets
import socket
import sqlite3
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.environ.get("MPA_DATA_DIR") or (BASE_DIR / "data"))
HISTORY_DIR = DATA_DIR / "history"
CONFIG_PATH = DATA_DIR / "config.json"

DISPLAY_NAME = "多视角人生参谋"


def build_version() -> str:
    """用静态资源的最新修改时间作为页面版本号：改了代码，页面就能发现自己是旧的。"""
    latest = 0
    for f in list(STATIC_DIR.glob("*")) + [Path(__file__)]:
        try:
            latest = max(latest, int(f.stat().st_mtime))
        except OSError:
            continue
    return time.strftime("%m%d-%H%M%S", time.localtime(latest)) if latest else "0"

# --------------------------------------------------------------------------
# 供应商预设：只要是 OpenAI 兼容接口，就能用「一个 Key 调很多模型」
# --------------------------------------------------------------------------

PROVIDER_PRESETS = {
    "openrouter": {
        "label": "OpenRouter（一个 Key → 460+ 模型，首选）",
        "base_url": "https://openrouter.ai/api/v1",
        "hint": "https://openrouter.ai/keys 注册后充值，即可调用 GPT / Claude / Gemini / Grok / DeepSeek / Qwen / Kimi / GLM 等几乎所有主流模型。",
        "extra_headers": {"HTTP-Referer": "http://localhost", "X-Title": DISPLAY_NAME},
        "supports_model_list": True,
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow（国内，一个 Key 多模型）",
        "base_url": "https://api.siliconflow.cn/v1",
        "hint": "https://cloud.siliconflow.cn 注册，可调用 DeepSeek / Qwen / GLM / Kimi 等国产模型，国内直连快。",
        "extra_headers": {},
        "supports_model_list": True,
    },
    "dashscope": {
        "label": "阿里云百炼（通义千问 / DeepSeek / Kimi 等）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "hint": "https://bailian.console.aliyun.com 开通后获取 API Key。",
        "extra_headers": {},
        "supports_model_list": False,
    },
    "volcengine": {
        "label": "火山方舟（豆包 / DeepSeek 等）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "hint": "https://console.volcengine.com/ark 开通模型服务，模型名通常需要填「接入点 ID」或模型 ID。",
        "extra_headers": {},
        "supports_model_list": False,
    },
    "deepseek": {
        "label": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com/v1",
        "hint": "https://platform.deepseek.com 官方直连，只有 DeepSeek 自家模型。",
        "extra_headers": {},
        "supports_model_list": True,
    },
    "moonshot": {
        "label": "Moonshot / Kimi 官方",
        "base_url": "https://api.moonshot.cn/v1",
        "hint": "https://platform.moonshot.cn 获取 Key。",
        "extra_headers": {},
        "supports_model_list": True,
    },
    "zhipu": {
        "label": "智谱 GLM 官方",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "hint": "https://open.bigmodel.cn 获取 Key。",
        "extra_headers": {},
        "supports_model_list": False,
    },
    "openai": {
        "label": "OpenAI 官方",
        "base_url": "https://api.openai.com/v1",
        "hint": "官方直连，只有 OpenAI 自家模型，且部分地区需要网络代理。",
        "extra_headers": {},
        "supports_model_list": True,
    },
    "custom": {
        "label": "自定义 OpenAI 兼容接口",
        "base_url": "",
        "hint": "任何提供 /chat/completions 的服务都可以，例如自建的 one-api / new-api 聚合网关。",
        "extra_headers": {},
        "supports_model_list": True,
    },
}

# 模型候选：price 为展示文案，cin/cout 为每百万 token 价格（用于费用估算）。
# 硅基流动为人民币，OpenRouter 为美元。价格抓取自各平台官方价格页（2026-10）。
MODEL_CATALOG = [
    # ---------- 硅基流动 · 免费 ----------
    {"id": "Qwen/Qwen3-8B", "label": "Qwen3-8B（免费）", "vendor": "阿里", "provider": "siliconflow",
     "price": "免费", "cin": 0, "cout": 0},
    {"id": "THUDM/GLM-4-9B-0414", "label": "GLM-4-9B（免费）", "vendor": "智谱", "provider": "siliconflow",
     "price": "免费", "cin": 0, "cout": 0},
    {"id": "Qwen/Qwen2.5-7B-Instruct", "label": "Qwen2.5-7B（免费）", "vendor": "阿里", "provider": "siliconflow",
     "price": "免费", "cin": 0, "cout": 0},
    {"id": "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B", "label": "DeepSeek-R1-8B（免费）", "vendor": "DeepSeek", "provider": "siliconflow",
     "price": "免费", "cin": 0, "cout": 0},
    # ---------- 硅基流动 · 超便宜 ----------
    {"id": "inclusionAI/Ling-mini-2.0", "label": "Ling-mini 2.0", "vendor": "蚂蚁", "provider": "siliconflow",
     "price": "￥0.5/￥2", "cin": 0.5, "cout": 2},
    {"id": "Qwen/Qwen3-14B", "label": "Qwen3-14B", "vendor": "阿里", "provider": "siliconflow",
     "price": "￥0.5/￥2", "cin": 0.5, "cout": 2},
    {"id": "stepfun-ai/Step-3.5-Flash", "label": "Step-3.5-Flash", "vendor": "阶跃星辰", "provider": "siliconflow",
     "price": "￥0.7/￥2.1", "cin": 0.7, "cout": 2.1},
    {"id": "Qwen/Qwen3-30B-A3B-Instruct-2507", "label": "Qwen3-30B-A3B", "vendor": "阿里", "provider": "siliconflow",
     "price": "￥0.7/￥2.8", "cin": 0.7, "cout": 2.8},
    {"id": "deepseek-ai/DeepSeek-V4-Flash", "label": "DeepSeek V4 Flash", "vendor": "DeepSeek", "provider": "siliconflow",
     "price": "￥1/￥2", "cin": 1, "cout": 2},
    {"id": "tencent/Hunyuan-A13B-Instruct", "label": "Hunyuan A13B", "vendor": "腾讯", "provider": "siliconflow",
     "price": "￥1/￥4", "cin": 1, "cout": 4},
    {"id": "Qwen/Qwen3-32B", "label": "Qwen3-32B", "vendor": "阿里", "provider": "siliconflow",
     "price": "￥1/￥4", "cin": 1, "cout": 4},
    {"id": "ByteDance-Seed/Seed-OSS-36B-Instruct", "label": "Seed-OSS-36B", "vendor": "字节", "provider": "siliconflow",
     "price": "￥1.5/￥4", "cin": 1.5, "cout": 4},
    {"id": "zai-org/GLM-4.5-Air", "label": "GLM-4.5-Air", "vendor": "智谱", "provider": "siliconflow",
     "price": "￥1/￥6", "cin": 1, "cout": 6},
    {"id": "Qwen/Qwen2.5-72B-Instruct-128K", "label": "Qwen2.5-72B", "vendor": "阿里", "provider": "siliconflow",
     "price": "￥4.13/￥4.13", "cin": 4.13, "cout": 4.13},
    # ---------- 硅基流动 · 稍贵但更强 ----------
    {"id": "deepseek-ai/DeepSeek-V3.2", "label": "DeepSeek V3.2", "vendor": "DeepSeek", "provider": "siliconflow",
     "price": "￥2/￥3", "cin": 2, "cout": 3},
    {"id": "deepseek-ai/DeepSeek-V3", "label": "DeepSeek V3", "vendor": "DeepSeek", "provider": "siliconflow",
     "price": "￥2/￥8", "cin": 2, "cout": 8},
    {"id": "deepseek-ai/DeepSeek-R1", "label": "DeepSeek R1（强推理）", "vendor": "DeepSeek", "provider": "siliconflow",
     "price": "￥4/￥16", "cin": 4, "cout": 16},
    {"id": "zai-org/GLM-5.3", "label": "GLM-5.3（旗舰）", "vendor": "智谱", "provider": "siliconflow",
     "price": "￥6/￥28", "cin": 6, "cout": 28},
    # ---------- OpenRouter（需另配 Key）----------
    {"id": "openai/gpt-5.6-sol", "label": "GPT-5.6 Sol", "vendor": "OpenAI", "provider": "openrouter",
     "price": "$2/M", "cin": 2, "cout": 2},
    {"id": "anthropic/claude-sonnet-4.6", "label": "Claude Sonnet 4.6", "vendor": "Anthropic", "provider": "openrouter",
     "price": "$3/M", "cin": 3, "cout": 3},
    {"id": "google/gemini-2.5-pro", "label": "Gemini 2.5 Pro", "vendor": "Google", "provider": "openrouter",
     "price": "$1.25/M", "cin": 1.25, "cout": 1.25},
    {"id": "google/gemini-2.5-flash", "label": "Gemini 2.5 Flash", "vendor": "Google", "provider": "openrouter",
     "price": "$0.3/M", "cin": 0.3, "cout": 0.3},
    {"id": "x-ai/grok-4.7", "label": "Grok 4.7", "vendor": "xAI", "provider": "openrouter",
     "price": "$2/M", "cin": 2, "cout": 2},
    {"id": "deepseek/deepseek-chat-v3.1", "label": "DeepSeek V3.1", "vendor": "DeepSeek", "provider": "openrouter",
     "price": "$0.25/M", "cin": 0.25, "cout": 0.25},
    {"id": "qwen/qwen3-max", "label": "Qwen3 Max", "vendor": "阿里", "provider": "openrouter",
     "price": "$0.78/M", "cin": 0.78, "cout": 0.78},
    {"id": "moonshotai/kimi-k2-thinking", "label": "Kimi K2 Thinking", "vendor": "月之暗面", "provider": "openrouter",
     "price": "$0.6/M", "cin": 0.6, "cout": 0.6},
    {"id": "z-ai/glm-4.6", "label": "GLM-4.6", "vendor": "智谱", "provider": "openrouter",
     "price": "$0.43/M", "cin": 0.43, "cout": 0.43},
]

# 「视角」= 让不同模型的回答真正呈现不同立场的核心技巧
LENSES = [
    {
        "key": "rational",
        "short": "理性",
        "name": "理性分析师",
        "desc": "把处境拆成事实、因果链和变量，冷峻但清楚。",
        "style": (
            "你是冷静的结构化分析师。先区分「事实 / 推断 / 情绪」，再把遭遇→问题→期待的因果链摊开，"
            "指出其中最关键的 1-2 个杠杆变量。用词精确，不做无根据的安慰。"
        ),
    },
    {
        "key": "empathic",
        "short": "共情",
        "name": "共情陪伴者",
        "desc": "先接住情绪与未被满足的需要，再谈办法。",
        "style": (
            "你是温暖而敏锐的心理陪伴者。先准确说出对方可能有的情绪与未被满足的需要（不夸张、不煽情），"
            "再温和地给出可行的下一步。尊重对方的节奏，避免评判与说教。"
        ),
    },
    {
        "key": "realist",
        "short": "风控",
        "name": "现实主义风控官",
        "desc": "盯住成本、约束和最坏情况，先保证不翻车。",
        "style": (
            "你是务实的风险控制者。明确列出真实约束（时间、金钱、关系、身体、机会成本），"
            "指出最坏情况与不可逆的风险，给出「保底方案」。不画饼，不接受空泛的励志。"
        ),
    },
    {
        "key": "strategist",
        "short": "战略",
        "name": "长期战略家",
        "desc": "把当下放进 3~10 年尺度，看复利与路径。",
        "style": (
            "你以 3~10 年的长周期视角思考。关注这件事在人生轨迹中的位置、可积累的资产（能力/关系/声誉/健康）、"
            "以及哪条路会让未来的选择变多。愿意给出「用现在换未来」的具体取舍。"
        ),
    },
    {
        "key": "socratic",
        "short": "追问",
        "name": "苏格拉底提问者",
        "desc": "追问隐藏假设，指出对方可能没看见的盲区。",
        "style": (
            "你不急着给答案，而是提出 3~5 个真正有穿透力的追问，暴露对方叙述里的隐藏假设、"
            "自我欺骗和未验证的推断，然后给出一个颠覆性的替代视角。"
        ),
    },
    {
        "key": "coach",
        "short": "行动",
        "name": "行动教练",
        "desc": "把一切落成今夜、本周、本月能做的小动作。",
        "style": (
            "你是行动教练。拒绝宏大叙事，只输出可执行动作：今天能做的第一步、本周的验证动作、"
            "判断是否该继续的检验标准。每条动作都要具体到「什么时候、和谁、做什么」。"
        ),
    },
    {
        "key": "systems",
        "short": "系统",
        "name": "系统关系观察者",
        "desc": "把个人问题放回家庭/组织/环境结构中看。",
        "style": (
            "你关注系统与关系：这段处境里有哪些他人、规则、文化期待在起作用？"
            "对方的困扰有多少来自个人选择、多少来自结构约束？给出调整「关系结构」而非只调整「自己」的建议。"
        ),
    },
    {
        "key": "evidence",
        "short": "证据",
        "name": "证据与概率审查者",
        "desc": "区分已知、未知与不可知，给出概率判断。",
        "style": (
            "你审查信息质量。指出叙述中哪些是关键但缺失的信息，对主要可能走向给出粗略概率估计与依据，"
            "并明确说明「在什么新信息出现时应该改变判断」。"
        ),
    },
    {
        "key": "health",
        "short": "身体",
        "name": "身心状态顾问",
        "desc": "把睡眠、精力和身体信号当成决策的前提条件。",
        "style": (
            "你关注身体与心理的承载能力。指出长期压力、睡眠、精力、社交退缩对身体的实际影响，"
            "说明在状态变差时人的判断会如何失真，并给出恢复精力的具体做法。"
            "你认为任何计划都必须先通过「身体能不能撑住」这一关。"
        ),
    },
    {
        "key": "money",
        "short": "财务",
        "name": "经济与现金流分析师",
        "desc": "算清钱能撑多久，用财务约束倒推时间表。",
        "style": (
            "你只谈钱和资源：现有存款能支撑多少个月、每月刚性支出多少、"
            "每种选择的最坏财务后果是什么、什么时候必须启动保底收入来源。"
            "给出可执行的财务红线与时间点，不谈情绪与理想。"
        ),
    },
    {
        "key": "reframe",
        "short": "破局",
        "name": "反直觉破局者",
        "desc": "质疑「二选一」本身，提出第三种可能。",
        "style": (
            "你专门挑战题目前提。当对方在 A 和 B 之间纠结时，你要指出这可能是个假选择，"
            "提出至少一个第三选项（兼职过渡、降级试水、先解决别的瓶颈等），"
            "并解释为什么原来的选择题会让人瘫痪。"
        ),
    },
    {
        "key": "timing",
        "short": "时机",
        "name": "时机与节奏专家",
        "desc": "判断什么时候该快、什么时候该慢，窗口期在哪。",
        "style": (
            "你判断时机与节奏。回答：这件事现在做和三个月后做，代价差在哪里；"
            "哪些窗口正在关闭（年龄、行业周期、空窗期长度），哪些反而不急；"
            "并给出「什么时候必须动、什么时候可以等」的分界点。"
        ),
    },
]

DEFAULT_CONFIG = {
    "active_provider": "siliconflow",
    "providers": {
        "openrouter": {"api_key": "", "base_url": PROVIDER_PRESETS["openrouter"]["base_url"]},
        "siliconflow": {"api_key": "", "base_url": PROVIDER_PRESETS["siliconflow"]["base_url"]},
    },
    # 8 个模型 × 3 个视角 = 24 份独立意见，单次总成本约 ¥0.1
    "default_models": [
        {"provider": "siliconflow", "id": "deepseek-ai/DeepSeek-V4-Flash", "label": "DeepSeek V4 Flash",
         "lenses": ["rational", "empathic", "realist"]},
        {"provider": "siliconflow", "id": "Qwen/Qwen3-30B-A3B-Instruct-2507", "label": "Qwen3-30B-A3B",
         "lenses": ["rational", "empathic", "realist"]},
        {"provider": "siliconflow", "id": "Qwen/Qwen3-14B", "label": "Qwen3-14B",
         "lenses": ["rational", "empathic", "realist"]},
        {"provider": "siliconflow", "id": "inclusionAI/Ling-mini-2.0", "label": "Ling-mini 2.0",
         "lenses": ["rational", "empathic", "realist"]},
        {"provider": "siliconflow", "id": "zai-org/GLM-4.5-Air", "label": "GLM-4.5-Air",
         "lenses": ["rational", "empathic", "realist"]},
        {"provider": "siliconflow", "id": "tencent/Hunyuan-A13B-Instruct", "label": "Hunyuan A13B",
         "lenses": ["rational", "empathic", "realist"]},
        {"provider": "siliconflow", "id": "ByteDance-Seed/Seed-OSS-36B-Instruct", "label": "Seed-OSS-36B",
         "lenses": ["rational", "empathic", "realist"]},
        {"provider": "siliconflow", "id": "Qwen/Qwen2.5-72B-Instruct-128K", "label": "Qwen2.5-72B",
         "lenses": ["rational", "empathic", "realist"]},
    ],
    # 汇总模型要快：实测 Qwen3-30B-A3B-Instruct 用 25 秒，DeepSeek-V3.2 要 3 分钟
    "default_aggregator": {"provider": "siliconflow", "id": "Qwen/Qwen3-30B-A3B-Instruct-2507",
                           "label": "Qwen3-30B-A3B"},
    "temperature": 0.85,
    "language": "中文",
    "max_tokens": 650,
    "max_concurrency": 12,
    "idle_timeout": 40,
    "hard_timeout": 100,
    "layered_summary": False,
    "random_count": 12,
    # 暴露到公网前一定要设置密码，否则任何人都能用你的 API Key
    "access": {"password": "", "secret": ""},
    # 使用统计与防滥用
    "stats": {
        "admin_token": "",          # 后台统计页的访问密钥（留空则首次启动自动生成）
        "per_visitor_hourly": 10,   # 每个访客每小时最多跑几次
        "daily_cost_cap": 20.0,     # 每天总花费上限（元），防止被刷爆
        "record_content": True,     # 是否记录输入与结果内容
    },
    "network": {"force_ipv4": True, "proxy": ""},
}

# 默认视角组合（每个模型都用这几个角色各答一次）
DEFAULT_LENSES = ["rational", "empathic", "realist"]

# 随机抽模型用的池子：全部实测可用、便宜、且单次响应在 25 秒以内。
# speed 为实测用时（秒），仅用于前端展示与自动挑选。
MODEL_POOL = [
    {"id": "inclusionAI/Ling-mini-2.0", "label": "Ling-mini 2.0", "vendor": "蚂蚁", "cin": 0.5, "cout": 2, "speed": 3.4},
    {"id": "THUDM/GLM-4-9B-0414", "label": "GLM-4-9B", "vendor": "智谱", "cin": 0, "cout": 0, "speed": 4.8},
    {"id": "Qwen/Qwen2.5-7B-Instruct", "label": "Qwen2.5-7B", "vendor": "阿里", "cin": 0, "cout": 0, "speed": 7.0},
    {"id": "Qwen/Qwen2.5-14B-Instruct", "label": "Qwen2.5-14B", "vendor": "阿里", "cin": 0.7, "cout": 0.7, "speed": 7.2},
    {"id": "deepseek-ai/DeepSeek-V4-Flash", "label": "DeepSeek V4 Flash", "vendor": "DeepSeek", "cin": 1, "cout": 2, "speed": 8.9},
    {"id": "Qwen/Qwen3-14B", "label": "Qwen3-14B", "vendor": "阿里", "cin": 0.5, "cout": 2, "speed": 9.2},
    {"id": "zai-org/GLM-4.5-Air", "label": "GLM-4.5-Air", "vendor": "智谱", "cin": 1, "cout": 6, "speed": 10.4},
    {"id": "Qwen/Qwen2.5-72B-Instruct-128K", "label": "Qwen2.5-72B", "vendor": "阿里", "cin": 4.13, "cout": 4.13, "speed": 10.5},
    {"id": "Qwen/Qwen3-30B-A3B-Instruct-2507", "label": "Qwen3-30B-A3B", "vendor": "阿里", "cin": 0.7, "cout": 2.8, "speed": 13.4},
    {"id": "tencent/Hunyuan-A13B-Instruct", "label": "Hunyuan A13B", "vendor": "腾讯", "cin": 1, "cout": 4, "speed": 14.4},
    {"id": "inclusionAI/Ling-flash-2.0", "label": "Ling-flash 2.0", "vendor": "蚂蚁", "cin": 1, "cout": 4, "speed": 17.0},
    {"id": "Qwen/Qwen3-32B", "label": "Qwen3-32B", "vendor": "阿里", "cin": 1, "cout": 4, "speed": 20.2},
    {"id": "THUDM/GLM-Z1-9B-0414", "label": "GLM-Z1-9B（免费）", "vendor": "智谱", "cin": 0, "cout": 0, "speed": 20.1},
    {"id": "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B", "label": "DeepSeek-R1-8B（免费）", "vendor": "DeepSeek", "cin": 0, "cout": 0, "speed": 25.2},
]

FAST_AGGREGATOR = {"provider": "siliconflow", "id": "Qwen/Qwen3-30B-A3B-Instruct-2507",
                   "label": "Qwen3-30B-A3B"}


def draw_random_round(count: int = 12, seed: int | None = None) -> dict:
    """随机抽一批：每个角色配一个不同的模型，模型与角色的配对每次都不一样。"""
    import random as _random
    rng = _random.Random(seed) if seed is not None else _random.Random()
    count = max(1, min(count, len(MODEL_POOL)))
    pool = list(MODEL_POOL)
    rng.shuffle(pool)
    picked = pool[:count]
    lenses = list(LENSES)
    rng.shuffle(lenses)
    models = []
    for m, lens in zip(picked, lenses[:count]):
        models.append({
            "provider": "siliconflow", "id": m["id"], "label": m["label"],
            "vendor": m["vendor"], "speed": m["speed"], "price": ("免费" if not m["cin"] else f"￥{m['cin']}/￥{m['cout']}"),
            "lenses": [lens["key"]],
        })
    labels = [f"{m['label']} × {next(l['name'] for l in LENSES if l['key'] == m['lenses'][0])}" for m in models]
    return {"models": models, "summary": labels,
            "estimated_seconds": max((x["speed"] for x in models), default=20)}

# --------------------------------------------------------------------------
# 网络：国内直连 OpenRouter 等海外服务时，IPv6 回程常常不通，
# 会导致连接卡住直到超时。默认强制走 IPv4（实测稳定）。
# --------------------------------------------------------------------------

_ORIG_GETADDRINFO = socket.getaddrinfo
_NET = {"force_ipv4": True}


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if _NET["force_ipv4"] and family in (0, socket.AF_UNSPEC):
        family = socket.AF_INET
    return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)


socket.getaddrinfo = _patched_getaddrinfo


def apply_network_options(cfg: dict) -> None:
    net = cfg.get("network") or {}
    _NET["force_ipv4"] = bool(net.get("force_ipv4", True))


def build_opener(cfg: dict):
    """按配置构造支持可选 HTTP/HTTPS 代理的 opener。"""
    proxy = ((cfg.get("network") or {}).get("proxy") or "").strip()
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))  # 忽略系统环境变量代理
    return urllib.request.build_opener(*handlers)


# --------------------------------------------------------------------------
# 访问密码：把服务暴露到公网前必须开启，否则任何人都能花你的 API 额度
# --------------------------------------------------------------------------

COOKIE_NAME = "mpa_auth"


def access_config(cfg: dict) -> dict:
    access = cfg.get("access") or {}
    if not access.get("secret"):
        access["secret"] = secrets.token_hex(16)
        cfg["access"] = access
        save_config(cfg)
    return access


def auth_token(cfg: dict) -> str:
    access = access_config(cfg)
    return hmac.new(access["secret"].encode(), access["password"].encode(), hashlib.sha256).hexdigest()


def auth_enabled(cfg: dict) -> bool:
    return bool(((cfg.get("access") or {}).get("password") or "").strip())


LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>多视角人生参谋</title>
<style>
  :root{color-scheme:dark}
  body{margin:0;min-height:100vh;display:grid;place-items:center;
    background:#08070f;color:#eceaf6;font-family:-apple-system,"PingFang SC",sans-serif;
    padding:24px;padding:calc(24px + env(safe-area-inset-top)) 24px calc(24px + env(safe-area-inset-bottom));}
  .box{width:100%;max-width:340px;background:rgba(255,255,255,.045);border:1px solid rgba(255,255,255,.09);
    border-radius:20px;padding:24px;backdrop-filter:blur(14px)}
  .orb{width:52px;height:52px;border-radius:50%;margin-bottom:18px;
    background:conic-gradient(from 0deg,#7c6bff,#ff6fae,#3fd0c2,#7c6bff)}
  h1{font-size:17px;margin:0 0 6px}
  p{font-size:12.5px;color:#a49fc0;margin:0 0 18px;line-height:1.7}
  input{width:100%;box-sizing:border-box;background:rgba(0,0,0,.3);color:#eceaf6;
    border:1px solid rgba(255,255,255,.12);border-radius:13px;padding:13px 14px;font-size:16px}
  input:focus{outline:none;border-color:rgba(139,123,255,.6);box-shadow:0 0 0 3px rgba(139,123,255,.15)}
  button{width:100%;margin-top:12px;padding:14px;border:none;border-radius:13px;font-size:15px;
    font-weight:600;color:#fff;background:linear-gradient(135deg,#7c6bff,#b06bff 45%,#ff6fae);cursor:pointer}
  .err{color:#ffb3c4;font-size:12.5px;margin-top:10px;min-height:18px}
</style></head>
<body>
<div class="box">
  <div class="orb"></div>
  <h1>多视角人生参谋</h1>
  <p>这是私人服务，请输入访问密码。</p>
  <input id="pw" type="password" placeholder="访问密码" autocomplete="current-password"/>
  <button id="go">进入</button>
  <div class="err" id="err"></div>
</div>
<script>
  const submit = async () => {
    const r = await fetch("/api/login", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({password: document.getElementById("pw").value})});
    if (r.ok) { location.href = "/"; }
    else { document.getElementById("err").textContent = "密码不对，再试一次"; }
  };
  document.getElementById("go").onclick = submit;
  document.getElementById("pw").addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
</script>
</body></html>
"""


# --------------------------------------------------------------------------
# 配置读写
# --------------------------------------------------------------------------

def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    ensure_dirs()
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in saved.items() if v is not None})
            cfg["providers"] = {**DEFAULT_CONFIG["providers"], **(saved.get("providers") or {})}
        except Exception as exc:  # 配置损坏时不阻塞启动
            print(f"[warn] 配置读取失败，使用默认配置：{exc}")
    # 环境变量兜底（可选）：OPENROUTER_API_KEY / SILICONFLOW_API_KEY ...
    for pid, preset in PROVIDER_PRESETS.items():
        env_key = os.environ.get(f"{pid.upper()}_API_KEY")
        if env_key:
            cfg["providers"].setdefault(pid, {})
            cfg["providers"][pid]["api_key"] = env_key
            cfg["providers"][pid].setdefault("base_url", preset["base_url"])
    return cfg


def save_config(cfg: dict) -> None:
    ensure_dirs()
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_provider(cfg: dict, provider_id: str) -> dict:
    provider_id = provider_id or cfg.get("active_provider") or "openrouter"
    preset = PROVIDER_PRESETS.get(provider_id, PROVIDER_PRESETS["custom"])
    saved = (cfg.get("providers") or {}).get(provider_id, {})
    return {
        "id": provider_id,
        "label": preset["label"],
        "base_url": (saved.get("base_url") or preset["base_url"] or "").rstrip("/"),
        "api_key": saved.get("api_key") or "",
        "extra_headers": preset.get("extra_headers", {}),
    }


def public_config(cfg: dict) -> dict:
    """回给前端时不泄露完整 Key。"""
    providers = {}
    for pid, preset in PROVIDER_PRESETS.items():
        saved = (cfg.get("providers") or {}).get(pid, {})
        key = saved.get("api_key") or ""
        providers[pid] = {
            "label": preset["label"],
            "hint": preset["hint"],
            "default_base_url": preset["base_url"],
            "supports_model_list": preset.get("supports_model_list", False),
            "base_url": saved.get("base_url") or preset["base_url"],
            "has_key": bool(key),
            "key_tail": key[-4:] if len(key) >= 8 else "",
            "api_key": key,  # 本地工具，便于在设置面板里回显编辑
        }
    return {
        "active_provider": cfg.get("active_provider"),
        "providers": providers,
        "models": cfg.get("default_models"),
        "aggregator": cfg.get("default_aggregator"),
        "temperature": cfg.get("temperature", 0.85),
        "language": cfg.get("language", "中文"),
        "max_tokens": cfg.get("max_tokens", 1600),
        "max_concurrency": cfg.get("max_concurrency", 6),
        "idle_timeout": cfg.get("idle_timeout", 45),
        "hard_timeout": cfg.get("hard_timeout", 150),
        "random_count": cfg.get("random_count", 12),
        "has_password": auth_enabled(cfg),
        "layered_summary": cfg.get("layered_summary", True),
        "default_lenses": DEFAULT_LENSES,
        "network": cfg.get("network", {"force_ipv4": True, "proxy": ""}),
    }


# --------------------------------------------------------------------------
# 使用统计：谁在用、用了几次、问了什么、最后得出什么结论
# 数据存在本机 data/stats.db（SQLite，标准库，无额外依赖）
# --------------------------------------------------------------------------

STATS_DB = DATA_DIR / "stats.db"
VISITOR_COOKIE = "mpa_vid"


def _stats_conn() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(STATS_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_stats_db() -> None:
    conn = _stats_conn()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS visitors (
            vid TEXT PRIMARY KEY,
            first_seen TEXT, last_seen TEXT,
            visits INTEGER DEFAULT 0,
            runs INTEGER DEFAULT 0,
            ip TEXT, ua TEXT, last_path TEXT
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            vid TEXT, created_at TEXT, duration REAL,
            ip TEXT, ua TEXT, referer TEXT,
            encounter TEXT, problem TEXT, expectation TEXT,
            final_text TEXT, reviews_json TEXT, models_json TEXT,
            cost REAL, status TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);
        CREATE INDEX IF NOT EXISTS idx_runs_vid ON runs(vid);
        CREATE TABLE IF NOT EXISTS visits (
            vid TEXT, ts TEXT, ip TEXT, ua TEXT, path TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_visits_vid ON visits(vid, ts);
        """)
        conn.commit()
    finally:
        conn.close()


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def touch_visitor(vid: str, ip: str, ua: str, path: str) -> None:
    """记录一次访问（同一个访客 5 分钟内重复刷新不重复计数）。"""
    now = now_str()
    conn = _stats_conn()
    try:
        row = conn.execute("SELECT last_seen FROM visitors WHERE vid=?", (vid,)).fetchone()
        if row:
            conn.execute(
                "UPDATE visitors SET last_seen=?, ip=?, ua=?, last_path=? WHERE vid=?",
                (now, ip, ua, path, vid))
            recent = conn.execute(
                "SELECT 1 FROM visits WHERE vid=? AND ts > datetime('now','localtime','-5 minutes') LIMIT 1",
                (vid,)).fetchone()
            if not recent:
                conn.execute("UPDATE visitors SET visits=visits+1 WHERE vid=?", (vid,))
                conn.execute("INSERT INTO visits(vid, ts, ip, ua, path) VALUES(?,?,?,?,?)",
                             (vid, now, ip, ua, path))
        else:
            conn.execute(
                "INSERT INTO visitors(vid, first_seen, last_seen, visits, runs, ip, ua, last_path)"
                " VALUES(?,?,?,?,0,?,?,?)", (vid, now, now, 1, ip, ua, path))
            conn.execute("INSERT INTO visits(vid, ts, ip, ua, path) VALUES(?,?,?,?,?)",
                         (vid, now, ip, ua, path))
        conn.commit()
    finally:
        conn.close()


def runs_last_hour(vid: str) -> int:
    conn = _stats_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) c FROM runs WHERE vid=? AND created_at > datetime('now','localtime','-1 hour')",
            (vid,)).fetchone()
        return int(row["c"] if row else 0)
    finally:
        conn.close()


def cost_today() -> float:
    conn = _stats_conn()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost),0) s FROM runs WHERE created_at >= date('now','localtime')").fetchone()
        return float(row["s"] if row else 0)
    finally:
        conn.close()


def record_run(job: "Job", duration: float, status: str) -> None:
    cfg = job.cfg
    stats_cfg = cfg.get("stats") or {}
    keep_content = bool(stats_cfg.get("record_content", True))
    if keep_content:
        parts = job.parts or {}
        enc = parts.get("encounter", "") or ""
        prob = parts.get("problem", "") or ""
        exp = parts.get("expectation", "") or ""
        final_text = job.final_text or ""
        reviews_json = json.dumps(
            [{k: r.get(k) for k in ("label", "lens_name", "model", "provider", "text", "phase")}
             for r in job.results.values()], ensure_ascii=False)
    else:
        enc = prob = exp = "[未记录]"
        final_text = "[未记录]"
        reviews_json = "[]"
    conn = _stats_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO runs(id, vid, created_at, duration, ip, ua, referer,"
            " encounter, problem, expectation, final_text, reviews_json, models_json, cost, status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job.id, job.visitor_id or "", job.created_at_str, round(duration, 1),
             job.client_ip or "", job.client_ua or "", job.client_referer or "",
             enc, prob, exp, final_text, reviews_json,
             json.dumps([{k: m.get(k) for k in ("id", "label", "lenses")} for m in (job.models or [])],
                        ensure_ascii=False),
             job.cost, status))
        conn.execute("UPDATE visitors SET runs = runs + 1, last_seen=? WHERE vid=?",
                     (now_str(), job.visitor_id or ""))
        conn.commit()
    finally:
        conn.close()


def stats_overview() -> dict:
    conn = _stats_conn()
    try:
        g = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        return {
            "visitors": g("SELECT COUNT(*) FROM visitors"),
            "runs": g("SELECT COUNT(*) FROM runs"),
            "runs_today": g("SELECT COUNT(*) FROM runs WHERE created_at >= date('now','localtime')"),
            "visitors_today": g("SELECT COUNT(DISTINCT vid) FROM runs WHERE created_at >= date('now','localtime')"),
            "cost_total": round(float(g("SELECT COALESCE(SUM(cost),0) FROM runs")), 3),
            "cost_today": round(cost_today(), 3),
            "failed": g("SELECT COUNT(*) FROM runs WHERE status != 'done'"),
        }
    finally:
        conn.close()


def list_visitors(limit: int = 200) -> list:
    conn = _stats_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM visitors ORDER BY last_seen DESC LIMIT ?", (limit,))]
    finally:
        conn.close()


def list_runs(limit: int = 200, vid: str | None = None) -> list:
    conn = _stats_conn()
    try:
        if vid:
            rows = conn.execute("SELECT * FROM runs WHERE vid=? ORDER BY created_at DESC LIMIT ?",
                                (vid, limit))
        else:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_run(run_id: str) -> dict | None:
    conn = _stats_conn()
    try:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def client_ip_of(handler) -> str:
    """Cloudflare 隧道会把真实访客 IP 放在 CF-Connecting-IP 里。"""
    for header in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
        val = handler.headers.get(header)
        if val:
            return val.split(",")[0].strip()
    try:
        return handler.client_address[0]
    except Exception:
        return ""


def stats_config(cfg: dict) -> dict:
    st = cfg.setdefault("stats", {})
    if not st.get("admin_token"):
        st["admin_token"] = secrets.token_urlsafe(18)
        save_config(cfg)
    return st


# --------------------------------------------------------------------------
# 后台统计页面（服务端渲染，不依赖任何前端框架）
# --------------------------------------------------------------------------

ADMIN_STYLE = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#08070f;color:#eceaf6;
  font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.7}
a{color:#a79bff;text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#8d88a8;font-size:12.5px;margin-bottom:22px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:26px}
.card{background:rgba(255,255,255,.045);border:1px solid rgba(255,255,255,.09);
  border-radius:16px;padding:14px 16px}
.card b{display:block;font-size:24px;font-weight:600;letter-spacing:.5px}
.card span{font-size:11.5px;color:#8d88a8}
h2{font-size:15px;margin:30px 0 12px;padding-left:10px;border-left:3px solid #8b7bff}
table{width:100%;border-collapse:collapse;font-size:13px;
  background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:14px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid rgba(255,255,255,.06);vertical-align:top}
th{background:rgba(139,123,255,.13);color:#cfc7ff;font-weight:600;font-size:12px;white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.025)}
.muted{color:#8d88a8}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.tag{display:inline-block;padding:1px 8px;border-radius:7px;font-size:11px;
  background:rgba(139,123,255,.18);color:#b9aeff;border:1px solid rgba(139,123,255,.3)}
.tag.ok{background:rgba(79,209,197,.15);color:#8fe6dc;border-color:rgba(79,209,197,.3)}
.tag.bad{background:rgba(255,122,140,.14);color:#ffb3c4;border-color:rgba(255,122,140,.3)}
.box{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);
  border-radius:14px;padding:14px 16px;margin-bottom:14px}
.box h3{margin:0 0 8px;font-size:13px;color:#cfc7ff}
pre{white-space:pre-wrap;word-break:break-word;margin:0;font-size:13px;
  font-family:inherit;color:#ddd9f0}
details{margin-bottom:10px;background:rgba(255,255,255,.03);
  border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:10px 14px}
summary{cursor:pointer;font-size:13px;color:#cfc7ff}
.bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
.btn{display:inline-block;padding:8px 14px;border-radius:11px;font-size:13px;
  background:rgba(139,123,255,.16);border:1px solid rgba(139,123,255,.34)}
.empty{color:#8d88a8;padding:18px;text-align:center}
"""


def short_ua(ua: str) -> str:
    if not ua:
        return "未知"
    device = "电脑"
    if "iPhone" in ua:
        device = "iPhone"
    elif "iPad" in ua:
        device = "iPad"
    elif "Android" in ua:
        device = "Android"
    elif "Macintosh" in ua:
        device = "Mac"
    elif "Windows" in ua:
        device = "Windows"
    browser = "浏览器"
    for key, name in (("Edg", "Edge"), ("Chrome", "Chrome"), ("Safari", "Safari"),
                      ("Firefox", "Firefox"), ("curl", "curl")):
        if key in ua:
            browser = name
            break
    return f"{device} · {browser}"


def esc(s) -> str:
    return html_mod.escape(str(s if s is not None else ""))


def admin_page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{ADMIN_STYLE}</style></head>
<body><div class="wrap">{body}</div></body></html>"""


ADMIN_404 = admin_page("找不到", "<h1>404</h1><p class='sub'>地址不对，或者后台密钥已更换。</p>")


def admin_dashboard() -> str:
    ov = stats_overview()
    visitors = list_visitors(300)
    runs = list_runs(200)
    token = stats_config(load_config())["admin_token"]
    base = f"/admin/{token}"

    cards = f"""
    <div class="cards">
      <div class="card"><b>{ov['visitors']}</b><span>访客总数</span></div>
      <div class="card"><b>{ov['runs']}</b><span>分析总次数</span></div>
      <div class="card"><b>{ov['runs_today']}</b><span>今日分析次数</span></div>
      <div class="card"><b>{ov['visitors_today']}</b><span>今日活跃人数</span></div>
      <div class="card"><b>￥{ov['cost_today']}</b><span>今日花费</span></div>
      <div class="card"><b>￥{ov['cost_total']}</b><span>累计花费</span></div>
      <div class="card"><b>{ov['failed']}</b><span>失败次数</span></div>
    </div>
    <div class="bar">
      <a class="btn" href="{base}/export.csv">导出 CSV（含问答内容）</a>
      <a class="btn" href="{base}/export.json">导出 JSON</a>
    </div>"""

    vt = "".join(
        f"<tr><td class='mono'><a href='{base}/visitor/{esc(v['vid'])}'>{esc(v['vid'])}</a></td>"
        f"<td class='muted'>{esc(v['first_seen'])}</td>"
        f"<td class='muted'>{esc(v['last_seen'])}</td>"
        f"<td>{v['visits']}</td><td>{v['runs']}</td>"
        f"<td class='mono'>{esc(v['ip'])}</td>"
        f"<td class='muted'>{esc(short_ua(v['ua'] or ''))}</td></tr>"
        for v in visitors) or "<tr><td colspan='7' class='empty'>还没有访客</td></tr>"

    def summarize(r: dict) -> str:
        text = (r["problem"] or r["encounter"] or r["expectation"] or r["final_text"] or "").strip()
        text = text.replace("\n", " ")
        return esc(text[:60]) + ("…" if len(text) > 60 else "")

    rt = "".join(
        f"<tr><td class='muted'>{esc(r['created_at'])}</td>"
        f"<td class='mono'>{esc(r['vid'])}</td>"
        f"<td><a href='{base}/run/{esc(r['id'])}'>{summarize(r) or '（无内容）'}</a></td>"
        f"<td>{r['duration'] if r['duration'] is not None else '-'}s</td>"
        f"<td>￥{r['cost'] if r['cost'] is not None else '-'}</td>"
        f"<td><span class='tag {'ok' if r['status'] == 'done' else 'bad'}'>"
        f"{'完成' if r['status'] == 'done' else esc(r['status'])}</span></td></tr>"
        for r in runs) or "<tr><td colspan='6' class='empty'>还没有人跑过分析</td></tr>"

    return admin_page("使用统计", f"""
      <h1>多视角人生参谋 · 使用统计</h1>
      <div class="sub">数据存在本机 data/stats.db，只有拿着这个网址的人能看到</div>
      {cards}
      <h2>访客（{len(visitors)}）</h2>
      <table><thead><tr><th>访客</th><th>首次访问</th><th>最近访问</th><th>访问次数</th>
      <th>分析次数</th><th>IP</th><th>设备</th></tr></thead><tbody>{vt}</tbody></table>
      <h2>最近的分析（{len(runs)}）</h2>
      <table><thead><tr><th>时间</th><th>访客</th><th>问题</th><th>用时</th><th>花费</th>
      <th>状态</th></tr></thead><tbody>{rt}</tbody></table>
    """)


def admin_visitor(vid: str) -> str:
    token = stats_config(load_config())["admin_token"]
    base = f"/admin/{token}"
    runs = list_runs(500, vid=vid)
    rows = "".join(
        f"<tr><td class='muted'>{esc(r['created_at'])}</td>"
        f"<td><a href='{base}/run/{esc(r['id'])}'>"
        f"{esc(((r['problem'] or r['encounter'] or '')[:60]).replace(chr(10), ' '))}</a></td>"
        f"<td>￥{r['cost']}</td></tr>" for r in runs)
    return admin_page(f"访客 {vid}", f"""
      <h1>访客 <span class="mono">{esc(vid)}</span></h1>
      <div class="sub"><a href="{base}">← 返回总览</a></div>
      <h2>TA 的分析记录（{len(runs)}）</h2>
      <table><thead><tr><th>时间</th><th>问题</th><th>花费</th></tr></thead>
      <tbody>{rows or "<tr><td colspan='3' class='empty'>没有记录</td></tr>"}</tbody></table>
    """)


def admin_run_detail(run: dict) -> str:
    token = stats_config(load_config())["admin_token"]
    base = f"/admin/{token}"
    try:
        reviews = json.loads(run.get("reviews_json") or "[]")
    except Exception:
        reviews = []
    try:
        models = json.loads(run.get("models_json") or "[]")
    except Exception:
        models = []
    review_html = "".join(
        f"<details><summary>{esc(r.get('label'))} · {esc(r.get('lens_name'))}</summary>"
        f"<pre>{esc(r.get('text'))}</pre></details>" for r in reviews)

    return admin_page("分析详情", f"""
      <h1>分析详情</h1>
      <div class="sub">
        {esc(run['created_at'])} · 访客 <span class="mono">{esc(run['vid'])}</span> ·
        {esc(run['ip'])} · {esc(short_ua(run['ua'] or ''))} ·
        用时 {run['duration']}s · 花费 ￥{run['cost']} ·
        <a href="{base}">← 返回总览</a>
      </div>
      <div class="box"><h3>遭遇（过往与当下）</h3><pre>{esc(run['encounter'])}</pre></div>
      <div class="box"><h3>问题（想要解决）</h3><pre>{esc(run['problem'])}</pre></div>
      <div class="box"><h3>未来的期待</h3><pre>{esc(run['expectation'])}</pre></div>
      <div class="box" style="border-color:rgba(139,123,255,.35)">
        <h3>最终报告</h3><pre>{esc(run['final_text'])}</pre></div>
      <h2>本次上场的模型（{len(models)}）</h2>
      <div class="sub">{esc('、'.join(f"{m.get('label')}" for m in models))}</div>
      <h2>12 份独立意见（点击展开）</h2>
      {review_html or "<div class='empty'>没有记录（可能已关闭内容记录）</div>"}
    """)


# --------------------------------------------------------------------------
# 调用大模型（OpenAI 兼容 /chat/completions，流式）
# --------------------------------------------------------------------------

class UpstreamError(Exception):
    pass


def stream_chat(provider: dict, model_id: str, messages: list, *, temperature: float = 0.85,
                max_tokens: int = 1600, timeout: int = 60, stop_event: threading.Event | None = None,
                opener=None):
    """向上游发起流式请求，逐段 yield 文本增量。"""
    if not provider.get("api_key"):
        raise UpstreamError(f"还没有配置 {provider.get('label') or provider.get('id')} 的 API Key，请在「设置」里填写。")
    if not provider.get("base_url"):
        raise UpstreamError("缺少 base_url，请在「设置」里填写接口地址。")

    url = f"{provider['base_url']}/chat/completions"
    payload = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider['api_key']}",
        "Accept": "text/event-stream",
        **(provider.get("extra_headers") or {}),
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    open_fn = opener.open if opener is not None else urllib.request.urlopen
    try:
        resp = open_fn(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:800]
        # 少数服务不支持 stream_options，去掉后重试一次
        if exc.code == 400 and "stream_options" in body:
            payload.pop("stream_options", None)
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers=headers, method="POST")
            try:
                resp = open_fn(req, timeout=timeout)
            except urllib.error.HTTPError as exc2:
                raise UpstreamError(f"上游返回 HTTP {exc2.code}：{exc2.read().decode('utf-8', 'replace')[:800]}") from exc2
        else:
            raise UpstreamError(f"上游返回 HTTP {exc.code}：{body}") from exc
    except urllib.error.URLError as exc:
        raise UpstreamError(f"网络连接失败：{exc.reason}") from exc

    with resp:
        for raw in resp:
            if stop_event is not None and stop_event.is_set():
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise UpstreamError(str(chunk["error"])[:500])
            if chunk.get("usage"):
                yield ("usage", chunk["usage"])
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    yield ("reasoning", delta["reasoning_content"])
                piece = delta.get("content")
                if piece:
                    yield ("content", piece)


def list_upstream_models(provider: dict, opener=None) -> list:
    url = f"{provider['base_url']}/models"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {provider['api_key']}", "Accept": "application/json"},
    )
    open_fn = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_fn(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise UpstreamError(f"获取模型列表失败：HTTP {exc.code} {detail}") from exc
    except Exception as exc:
        raise UpstreamError(f"获取模型列表失败：{exc}") from exc
    items = body.get("data") or body.get("models") or []
    out = []
    for item in items:
        mid = item.get("id") or item.get("name")
        if mid:
            out.append({"id": mid, "label": item.get("name") or mid})
    return sorted(out, key=lambda x: x["id"])


# --------------------------------------------------------------------------
# 提示词：让每个模型以不同视角作答
# --------------------------------------------------------------------------

def render_user_brief(parts: dict) -> str:
    encounter = (parts.get("encounter") or "").strip()
    problem = (parts.get("problem") or "").strip()
    expectation = (parts.get("expectation") or "").strip()

    blocks = []
    if encounter:
        blocks.append(f"【遭遇｜过往与当下】\n{encounter}")
    else:
        blocks.append("【遭遇｜过往与当下】\n（对方没有提供，请不要编造具体经历，只做必要的谨慎推断，并说明你在推断什么）")
    if problem:
        blocks.append(f"【问题｜想要解决】\n{problem}")
    else:
        blocks.append("【问题｜想要解决】\n（对方没有明确说出问题，请你从「遭遇」和「期待」中推断最可能的 2 个核心问题，并标明这是你的推断）")
    if expectation:
        blocks.append(f"【未来的期待】\n{expectation}")
    else:
        blocks.append("【未来的期待】\n（对方没有提供，请在回答中提出 1~2 个澄清期待的问题）")
    return "\n\n".join(blocks)


def reviewer_messages(lens: dict, parts: dict, language: str) -> list:
    system = (
        f"{lens['style']}\n\n"
        "你正在参加一场「多视角圆桌」，会有其他视角不同的模型同时给出意见，之后由一位主持人汇总。"
        "所以你的价值在于：把你自己这个视角看到的东西说透，而不是面面俱到。"
        "如果某个判断和主流视角很可能不同，请明确说出来，别为了稳妥而模糊。\n\n"
        "输出要求：\n"
        "1. 直接开始，不要客套、不要重复对方的话。\n"
        "2. 用 Markdown，二级标题固定为：## 我看到的处境 / ## 我的关键判断 / ## 给你的建议 / ## 一句话提醒。\n"
        "3. 「给你的建议」写 2~4 条，每条一句话讲清「做什么 + 为什么」。\n"
        "4. 全文 400~700 字，信息密度高，不要空话。\n"
        f"5. 用{language}回答。"
    )
    user = (
        "下面是一个人提供的三部分信息（可能不完整，缺失的部分已注明）。请按你的视角分析：\n\n"
        f"{render_user_brief(parts)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def cross_exam_messages(lens: dict, parts: dict, others_md: str, language: str) -> list:
    system = (
        f"{lens['style']}\n\n"
        "现在是圆桌第二轮：你已经看到其他视角模型第一轮的意见。请只做三件事，不重复第一轮内容：\n"
        "1. 哪一条其他意见你认为最有价值，为什么；\n"
        "2. 哪一条你明确不同意，给出你的理由；\n"
        "3. 看到他们的意见后，你要修正或补充什么。\n"
        "用 Markdown，二级标题为：## 我认同的 / ## 我反对的 / ## 我要修正的。全文 250~450 字。"
        f"用{language}回答。"
    )
    user = (
        f"原始信息：\n\n{render_user_brief(parts)}\n\n"
        f"其他视角的第一轮意见：\n\n{others_md}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def aggregator_messages(parts: dict, reviewer_outputs: list, cross_outputs: list, language: str,
                        want_suggestions: int = 4) -> list:
    system = (
        "你是一场「多视角圆桌」的主持人与总分析师。几位立场不同的顾问已经独立给出意见"
        "（第二轮还有交叉讨论）。你的任务不是简单拼接，而是把它们整合成一份对当事人真正有用的最终稿："
        "既要指出共识，也要诚实呈现分歧的价值，并给出你自己的最终判断。\n\n"
        "严格按以下 Markdown 结构输出，不要增删一级标题，不要写前言：\n"
        "## 一、三者关系分析\n"
        "分析「遭遇（无论过往还是当下）」「问题」「未来期待」这三者之间的因果关系与张力："
        "遭遇如何塑造了今天的问题，问题又怎样挡住期待；"
        "并指出这个链条中真正的卡点在哪一环。若某项信息缺失，明确说明它缺失带来的不确定性。\n"
        "## 二、多模型共识\n"
        "所有人几乎一致认同的判断（3~5 条，每条一句话）。\n"
        "## 三、分歧与多视角对照\n"
        "用一个 Markdown 表格列出 3~5 条主要分歧，列为：| 议题 | 视角A（含模型/视角名） | 视角B | 分歧的本质 |。"
        "表格后补一段：分歧说明这件事的哪种可能性更大。\n"
        f"## 四、给你的 {want_suggestions} 条建议\n"
        f"恰好 {want_suggestions} 条。每条用「**建议N｜一句话结论**」开头，下面三行小字："
        "「为什么」/「怎么做（具体到动作与时间）」/「风险与代价」。建议要有先后顺序与轻重缓急。\n"
        "## 五、最后的分析与意见\n"
        "综合所有视角，给出你作为总分析师的最终判断：这件事最重要的一个变量是什么、"
        "最可能的两种走向、以及如果只能做一件事应该做什么。语气诚实、有温度、不说套话。\n\n"
        f"全文 900~1400 字，用{language}回答。"
    )
    others = []
    for item in reviewer_outputs:
        others.append(f"### {item['label']}（{item['lens_name']}）\n{item['text']}")
    cross_block = ""
    if cross_outputs:
        cross_lines = [f"### {c['label']}（{c['lens_name']}）的补充\n{c['text']}" for c in cross_outputs]
        cross_block = "\n\n第二轮交叉讨论：\n" + "\n\n".join(cross_lines)
    user = (
        f"原始信息：\n\n{render_user_brief(parts)}\n\n"
        f"第一轮各视角意见：\n\n" + "\n\n".join(others) + cross_block
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def lens_summary_messages(lens: dict, parts: dict, outputs: list, language: str) -> list:
    """分层汇总第一层：把「同一个视角、不同模型」的意见合并成一份视角报告。"""
    system = (
        f"你是「{lens['name']}」这个视角的主笔。{lens['style']}\n\n"
        "现在有多个不同的 AI 模型，都在用你这个视角分析同一个人的处境（每份意见后面标了模型名）。"
        "请把它们合并成一份高质量的视角报告：\n"
        "## 这个视角的共识\n"
        "多个模型都提到的判断（3~5 条）。\n"
        "## 有价值的补充\n"
        "单独某个模型提出的、值得保留的独特观点。\n"
        "## 这个视角给的建议\n"
        "2~4 条具体建议，注明来自哪个模型的想法。\n"
        "## 这个视角的局限\n"
        "一句话说明这个视角容易忽略什么。\n\n"
        f"全文 450~700 字，用{language}回答。不要罗列所有模型的原话，要有编辑和取舍。"
    )
    body = "\n\n".join(f"### {o['label']}\n{o['text']}" for o in outputs if o.get("text"))
    user = f"原始信息：\n\n{render_user_brief(parts)}\n\n各模型的「{lens['name']}」意见：\n\n{body}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# --------------------------------------------------------------------------
# 任务编排
# --------------------------------------------------------------------------

EVENT_QUEUE_TIMEOUT = 0.35

PRICE_INDEX = {m["id"]: m for m in MODEL_CATALOG}

# 正在跑 / 刚跑完的任务：手机断线后重新连回来，靠它恢复现场
JOBS: dict[str, "Job"] = {}
JOBS_LOCK = threading.Lock()
JOBS_KEEP = 60          # 最多保留多少个任务
JOBS_TTL = 60 * 60 * 6  # 任务最多保留 6 小时


def register_job(job: "Job") -> None:
    with JOBS_LOCK:
        JOBS[job.id] = job
        if len(JOBS) > JOBS_KEEP:
            for old_id in sorted(JOBS, key=lambda k: JOBS[k].created)[: len(JOBS) - JOBS_KEEP]:
                JOBS.pop(old_id, None)
        now = time.time()
        for jid in [k for k, j in JOBS.items() if now - j.created > JOBS_TTL]:
            JOBS.pop(jid, None)


def get_job(job_id: str) -> "Job | None":
    with JOBS_LOCK:
        return JOBS.get(job_id)


def price_of(model_id: str) -> tuple[float, float]:
    item = PRICE_INDEX.get(model_id)
    if item:
        return float(item.get("cin") or 0), float(item.get("cout") or 0)
    return 2.0, 3.0  # 未知模型按中位价估算


def estimate_usage_cost(usage: dict, models: list, aggregator: dict) -> float:
    """按目录价粗算本次实际花费（人民币口径）。"""
    pin, pout = price_of((aggregator or {}).get("id", ""))
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    # 汇总调用通常占总输入的大头，这里用混合价加权：评审模型取平均价
    pairs = [price_of(m.get("id", "")) for m in models] or [(2.0, 3.0)]
    avg_in = sum(p[0] for p in pairs) / len(pairs)
    avg_out = sum(p[1] for p in pairs) / len(pairs)
    mix_in = (avg_in + pin) / 2
    mix_out = (avg_out + pout) / 2
    return prompt / 1e6 * mix_in + completion / 1e6 * mix_out


class Job:
    def __init__(self, payload: dict, cfg: dict):
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.payload = payload
        self.cfg = cfg
        self.parts = payload.get("parts") or {}
        self.language = payload.get("language") or cfg.get("language") or "中文"
        self.temperature = float(payload.get("temperature") or cfg.get("temperature") or 0.85)
        self.max_tokens = int(payload.get("max_tokens") or cfg.get("max_tokens") or 1600)
        self.deep = bool(payload.get("deep"))
        self.models = payload.get("models") or []
        self.aggregator = payload.get("aggregator") or {}
        self.suggestions = int(payload.get("suggestions") or 4)
        self.layered = payload.get("layered")
        if self.layered is None:
            self.layered = bool(cfg.get("layered_summary", True))
        self.max_concurrency = max(1, min(20, int(payload.get("max_concurrency")
                                                  or cfg.get("max_concurrency") or 6)))
        # 单次调用：空闲 N 秒没有新数据就断开；总时长超过 M 秒也强制收尾（保留已生成内容）
        self.idle_timeout = int(payload.get("idle_timeout") or cfg.get("idle_timeout") or 45)
        self.hard_timeout = int(payload.get("hard_timeout") or cfg.get("hard_timeout") or 150)
        self.stop_event = threading.Event()
        self.q: "queue.Queue[dict]" = queue.Queue()
        self.results: dict[str, dict] = {}
        self.final_text = ""
        self.lock = threading.Lock()
        self.created = time.time()
        self.created_at_str = now_str()
        # 访客标识（由 HTTP 层注入）
        self.visitor_id = ""
        self.client_ip = ""
        self.client_ua = ""
        self.client_referer = ""
        # 断线重连用的现场：每张卡片的最新文本、状态，以及总体状态
        self.subscribers: list[queue.Queue] = []
        self.cards: dict[str, dict] = {}
        self.state = {"status": "running", "total": 0, "cost": None, "usage": None}
        self.opener = build_opener(cfg)
        self._reasoning_chars: dict[str, int] = {}
        self._usage_by_key: dict[str, tuple[int, int]] = {}
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        self.cost = 0.0

    def add_usage(self, key: str, usage: dict) -> None:
        # 注意：流式接口会在很多分片里重复带上"累计 usage"，
        # 所以这里取最大值而不是累加，否则 token 数会被放大成百上千倍。
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        with self.lock:
            cur = self._usage_by_key.get(key, (0, 0))
            self._usage_by_key[key] = (max(cur[0], pt), max(cur[1], ct))
            self.usage["prompt_tokens"] = sum(v[0] for v in self._usage_by_key.values())
            self.usage["completion_tokens"] = sum(v[1] for v in self._usage_by_key.values())
            self.usage["calls"] = len(self._usage_by_key)

    def emit(self, event: str, **data) -> None:
        payload = {"event": event, **data}
        with self.lock:
            self._apply(payload)
            subs = list(self.subscribers)
        for q in subs:
            q.put(payload)

    # -- 现场维护：让新连上来的客户端能补齐所有已生成的内容 ---------------
    def _apply(self, ev: dict) -> None:
        e = ev.get("event")
        key = ev.get("key")
        if e == "job_start":
            self.state["total"] = ev.get("total") or 0
        elif e == "model_start":
            self.cards.setdefault(key, {
                "key": key, "label": ev.get("label"), "lens_name": ev.get("lens_name"),
                "scope": ev.get("scope"), "phase": ev.get("phase"), "text": "",
                "status": "running", "error": None,
            })
        elif e == "delta":
            card = self.cards.setdefault(key, {
                "key": key, "label": "", "lens_name": "", "scope": ev.get("scope"),
                "phase": None, "text": "", "status": "running", "error": None,
            })
            card["text"] += ev.get("text") or ""
        elif e in ("model_done", "lens_done"):
            card = self.cards.get(key)
            if card:
                card["status"] = "done"
        elif e in ("model_error", "model_timeout"):
            card = self.cards.get(key)
            if card:
                card["status"] = "error"
                card["error"] = ev.get("message")
        elif e == "done":
            self.state.update({"status": "done", "cost": ev.get("cost"), "usage": ev.get("usage")})
        elif e == "fatal":
            self.state.update({"status": "error", "error": ev.get("message")})
        elif e == "__close__":
            if self.state["status"] == "running":
                self.state["status"] = "done"
            self.state["finished"] = True

    def subscribe(self) -> tuple["queue.Queue", dict]:
        q: "queue.Queue[dict]" = queue.Queue()
        with self.lock:
            self.subscribers.append(q)
            snap = {
                "event": "snapshot", "job_id": self.id, "state": dict(self.state),
                "cards": [dict(c) for c in self.cards.values()],
                "parts": self.parts,
                "aggregator": self.aggregator,
            }
        return q, snap

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    # -- 单模型调用 ------------------------------------------------------
    def _run_one(self, model: dict, lens: dict, messages: list, scope: str, phase: str = "round1",
                 key: str | None = None) -> str:
        key = key or f"{scope}:{model.get('provider')}:{model.get('id')}"
        label = model.get("label") or model.get("id")
        provider = resolve_provider(self.cfg, model.get("provider"))
        self.emit("model_start", key=key, scope=scope, label=label,
                  lens_name=lens["name"], phase=phase)
        buffer = []
        t_start = time.time()
        timed_out = False
        try:
            for kind, piece in stream_chat(provider, model["id"], messages, temperature=self.temperature,
                                           max_tokens=self.max_tokens, stop_event=self.stop_event,
                                           timeout=self.idle_timeout, opener=self.opener):
                if self.stop_event.is_set():
                    break
                if time.time() - t_start > self.hard_timeout:
                    timed_out = True
                    break
                if kind == "usage":
                    self.add_usage(key, piece)
                    continue
                if kind == "reasoning":
                    self._reasoning_chars[key] = self._reasoning_chars.get(key, 0) + len(piece)
                    self.emit("thinking", key=key, chars=self._reasoning_chars[key])
                    continue
                buffer.append(piece)
                self.emit("delta", key=key, scope=scope, text=piece)
        except UpstreamError as exc:
            self.emit("model_error", key=key, scope=scope, message=str(exc)[:600])
            return ""
        except Exception as exc:  # noqa: BLE001
            self.emit("model_error", key=key, scope=scope, message=f"{type(exc).__name__}: {exc}"[:600])
            return ""
        if timed_out:
            self.emit("model_timeout", key=key, scope=scope,
                      message=f"超过 {self.hard_timeout} 秒仍未答完，已截断（保留已生成部分）")
        text = "".join(buffer)
        entry = {"label": label, "lens_name": lens["name"], "lens": model.get("lens"),
                 "lens_key": lens["key"],
                 "model": model.get("id"), "provider": model.get("provider"), "text": text, "phase": phase}
        with self.lock:
            self.results[key] = entry
        self.emit("model_done", key=key, scope=scope, chars=len(text))
        return text

    # -- 主流程 ----------------------------------------------------------
    def run(self) -> None:
        try:
            models = [m for m in self.models if m.get("id")]
            if not models:
                self.emit("fatal", message="请至少选择一个模型。")
                return

            # 展开成「模型 × 视角」矩阵
            tasks = []  # (model, lens, key)
            for m in models:
                lens_keys = m.get("lenses") or ([m["lens"]] if m.get("lens") else DEFAULT_LENSES)
                for lk in lens_keys:
                    lens = next((l for l in LENSES if l["key"] == lk), LENSES[0])
                    key = f"model:{m.get('provider')}:{m.get('id')}#{lens['key']}"
                    tasks.append((m, lens, key))

            self.emit("job_start", job_id=self.id, total=len(tasks), models=[
                {"key": k, "label": m.get("label") or m["id"], "lens_name": l["name"]}
                for m, l, k in tasks
            ])

            # 第一轮：所有「模型 × 视角」并行
            with ThreadPoolExecutor(max_workers=min(self.max_concurrency, len(tasks))) as pool:
                futures = []
                for m, lens, key in tasks:
                    msgs = reviewer_messages(lens, self.parts, self.language)
                    futures.append(pool.submit(self._run_one, m, lens, msgs, "model", "round1", key))
                for f in futures:
                    f.result()

            round1 = list(self.results.values())
            if not any(r.get("text") for r in round1):
                self.emit("fatal", message="所有模型都没有返回内容，请检查 API Key、模型名与网络。")
                return

            # 第二轮（可选圆桌交叉）
            cross_outputs = []
            if self.deep:
                with ThreadPoolExecutor(max_workers=min(self.max_concurrency, len(tasks))) as pool:
                    futures = []
                    for m, lens, own_key in tasks:
                        pool_items = [r for k, r in self.results.items()
                                      if k != own_key and r.get("text") and r.get("phase") == "round1"]
                        # 意见太多时只取样，避免第二轮 prompt 过大（控成本）
                        if len(pool_items) > 6:
                            same_lens = [r for r in pool_items if r.get("lens_key") == lens["key"]]
                            other_lens = [r for r in pool_items if r.get("lens_key") != lens["key"]]
                            pool_items = same_lens[:2] + other_lens[:4]
                        others = "\n\n".join(
                            f"### {r['label']}（{r['lens_name']}）\n{(r.get('text') or '')[:1200]}"
                            for r in pool_items
                        )
                        if not others.strip():
                            continue
                        msgs = cross_exam_messages(lens, self.parts, others, self.language)
                        futures.append(pool.submit(self._run_one, m, lens, msgs, "cross", "round2",
                                                   f"cross:{m.get('provider')}:{m.get('id')}#{lens['key']}"))
                    for f in futures:
                        f.result()
                cross_outputs = [r for r in self.results.values() if r.get("phase") == "round2"]

            # 分层汇总第一层：同一视角、不同模型先合并
            lens_reports = []
            if self.layered and len(round1) > 16:
                agg_cfg = self.aggregator or {}
                groups = {}
                for r in round1:
                    groups.setdefault(r.get("lens_key") or "rational", []).append(r)
                for lk, items in groups.items():
                    if len(items) < 2:
                        continue
                    lens = next((l for l in LENSES if l["key"] == lk), LENSES[0])
                    key = f"lens:{lk}"
                    provider = resolve_provider(self.cfg, agg_cfg.get("provider"))
                    self.emit("model_start", key=key, scope="lens", label=f"{lens['name']} · 视角综述",
                              lens_name=lens["name"], phase="merge")
                    msgs = lens_summary_messages(lens, self.parts, items, self.language)
                    buffer = []
                    t_start = time.time()
                    try:
                        for kind, piece in stream_chat(provider, agg_cfg.get("id", ""), msgs,
                                                       temperature=0.5, max_tokens=1200,
                                                       stop_event=self.stop_event, opener=self.opener,
                                                       timeout=max(self.idle_timeout, 60)):
                            if self.stop_event.is_set():
                                break
                            if time.time() - t_start > self.hard_timeout:
                                break
                            if kind == "usage":
                                self.add_usage(key, piece)
                                continue
                            if kind == "reasoning":
                                self._reasoning_chars[key] = self._reasoning_chars.get(key, 0) + len(piece)
                                self.emit("thinking", key=key, chars=self._reasoning_chars[key])
                                continue
                            buffer.append(piece)
                            self.emit("delta", key=key, scope="lens", text=piece)
                    except Exception as exc:  # noqa: BLE001
                        self.emit("model_error", key=key, scope="lens", message=str(exc)[:400])
                    text = "".join(buffer)
                    if text.strip():
                        lens_reports.append({"label": lens["name"], "lens_name": lens["name"],
                                             "lens_key": lk, "model": agg_cfg.get("id"),
                                             "provider": agg_cfg.get("provider"), "text": text,
                                             "phase": "merge"})
                        self.results[key] = lens_reports[-1]
                    self.emit("model_done", key=key, scope="lens", chars=len(text))

            # 汇总
            agg = self.aggregator or {}
            if not agg.get("id"):
                self.emit("fatal", message="请选择汇总模型。")
                return
            agg_key = "final"
            agg_label = agg.get("label") or agg["id"]
            self.emit("model_start", key=agg_key, scope="final", label=agg_label,
                      lens_name="总分析师", phase="aggregate")
            provider = resolve_provider(self.cfg, agg.get("provider"))
            # 有分层综述时，把综述 + 各方原始意见一起交给总分析师（原始意见做长度截断以控成本）
            trimmed = []
            for r in round1:
                item = dict(r)
                item["text"] = (r.get("text") or "")[:1000]
                trimmed.append(item)
            feed = (lens_reports + trimmed) if lens_reports else round1
            msgs = aggregator_messages(self.parts, feed, cross_outputs, self.language, self.suggestions)
            buffer = []
            t_start = time.time()
            try:
                for kind, piece in stream_chat(provider, agg["id"], msgs, temperature=0.6,
                                               max_tokens=1800, stop_event=self.stop_event,
                                               opener=self.opener, timeout=max(self.idle_timeout, 60)):
                    if self.stop_event.is_set():
                        break
                    if time.time() - t_start > max(self.hard_timeout, 240):
                        break
                    if kind == "usage":
                        self.add_usage(agg_key, piece)
                        continue
                    if kind == "reasoning":
                        self._reasoning_chars[agg_key] = self._reasoning_chars.get(agg_key, 0) + len(piece)
                        self.emit("thinking", key=agg_key, chars=self._reasoning_chars[agg_key])
                        continue
                    buffer.append(piece)
                    self.emit("delta", key=agg_key, scope="final", text=piece)
            except Exception as exc:  # noqa: BLE001
                self.emit("model_error", key=agg_key, scope="final", message=str(exc)[:600])
            self.final_text = "".join(buffer)
            self.emit("model_done", key=agg_key, scope="final", chars=len(self.final_text))
            self.save_history()
            self.cost = estimate_usage_cost(self.usage, self.models, self.aggregator)
            self.emit("done", job_id=self.id, usage=self.usage, cost=round(self.cost, 4))
            try:
                record_run(self, time.time() - self.created, "done")
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] 统计写入失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            self.emit("fatal", message=f"{type(exc).__name__}: {exc}")
            try:
                record_run(self, time.time() - self.created, "error")
            except Exception:
                pass
        finally:
            self.emit("__close__")

    def save_history(self) -> None:
        try:
            ensure_dirs()
            record = {
                "id": self.id,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "parts": self.parts,
                "final": self.final_text,
                "reviews": list(self.results.values()),
                "aggregator": self.aggregator,
                "deep": self.deep,
                "title": (self.parts.get("problem") or self.parts.get("encounter")
                          or self.parts.get("expectation") or "未命名")[:40],
            }
            (HISTORY_DIR / f"{self.id}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 保存历史失败：{exc}")


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "MPA/1.0"
    protocol_version = "HTTP/1.1"
    cfg_cache = None

    def log_message(self, fmt, *args):  # 静音
        pass

    # -- 鉴权 --------------------------------------------------------------
    def _cfg(self) -> dict:
        return load_config()

    def _is_authed(self, cfg: dict) -> bool:
        if not auth_enabled(cfg):
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == COOKIE_NAME:
                    return hmac.compare_digest(v.strip(), auth_token(cfg))
        return False

    def _deny(self) -> None:
        if self.path.startswith("/api/"):
            return self._send_json({"error": "unauthorized", "need_login": True}, 401)
        body = LOGIN_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _guard(self) -> bool:
        """返回 True 表示已放行；返回 False 表示已经回写了拒绝响应。"""
        path = self.path.split("?")[0]
        if (path in ("/api/login", "/manifest.webmanifest", "/sw.js", "/static/sw.js")
                or path.startswith("/static/icons/") or path == "/favicon.ico"):
            # PWA 图标与清单必须能匿名访问，否则「添加到主屏幕」时拿不到图标
            return True
        cfg = self._cfg()
        if self._is_authed(cfg):
            return True
        self._deny()
        return False

    # -- 工具 ------------------------------------------------------------
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cookie_header()
        self.end_headers()
        self.wfile.write(body)

    def _cookie_header(self) -> None:
        """第一次访问时给浏览器发一个匿名访客 ID，用来区分不同的人。"""
        if getattr(self, "_new_vid", None):
            self.send_header(
                "Set-Cookie",
                f"{VISITOR_COOKIE}={self._new_vid}; Path=/; Max-Age={60*60*24*400}; SameSite=Lax")

    def _visitor_id(self) -> str:
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == VISITOR_COOKIE and v.strip():
                    return v.strip()
        self._new_vid = secrets.token_hex(6)
        return self._new_vid

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cookie_header()
        self.end_headers()
        self.wfile.write(body)

    # -- 后台统计 ----------------------------------------------------------
    def _admin(self, path: str) -> None:
        cfg = self._cfg()
        token = stats_config(cfg)["admin_token"]
        parts = [p for p in path.split("/") if p]  # ["admin", token, ...]
        if len(parts) < 2 or not hmac.compare_digest(parts[1], token):
            return self._send_html(ADMIN_404, 404)
        rest = parts[2:]
        if not rest:
            return self._send_html(admin_dashboard())
        if rest[0] == "run" and len(rest) > 1:
            run = get_run(rest[1])
            return self._send_html(admin_run_detail(run) if run else ADMIN_404, 200 if run else 404)
        if rest[0] == "visitor" and len(rest) > 1:
            return self._send_html(admin_visitor(rest[1]))
        if rest[0] == "export.json":
            return self._send_json({"overview": stats_overview(),
                                    "visitors": list_visitors(1000),
                                    "runs": [{**r, "reviews_json": json.loads(r["reviews_json"] or "[]")}
                                             for r in list_runs(1000)]})
        if rest[0] == "export.csv":
            return self._send_csv()
        self._send_html(ADMIN_404, 404)

    def _send_csv(self) -> None:
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["时间", "访客ID", "IP", "设备", "用时(秒)", "花费(元)", "状态",
                    "遭遇", "问题", "期待", "最终报告"])
        for r in list_runs(5000):
            w.writerow([r["created_at"], r["vid"], r["ip"], short_ua(r["ua"] or ""),
                        r["duration"], r["cost"], r["status"],
                        r["encounter"], r["problem"], r["expectation"],
                        (r["final_text"] or "")])
        body = ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM 让 Excel 正确识别中文
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="mpa-runs.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def _serve_static(self, rel: str) -> None:
        target = (STATIC_DIR / rel.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        if target.name == "index.html":
            # 注入版本号：既做缓存击穿，也让页面能判断自己是不是旧版本
            body = target.read_text(encoding="utf-8").replace("__VER__", build_version()).encode("utf-8")
        else:
            body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cookie_header()
        self.end_headers()
        self.wfile.write(body)

    # -- 路由 ------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        if not self._guard():
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            vid = self._visitor_id()
            try:
                touch_visitor(vid, client_ip_of(self), self.headers.get("User-Agent", ""), "/")
            except Exception:
                pass
            return self._serve_static("index.html")
        if path.startswith("/admin"):
            return self._admin(path)
        if path == "/manifest.webmanifest":
            return self._serve_static("manifest.webmanifest")
        if path == "/sw.js":
            # Service Worker 必须从根路径提供，作用域才能覆盖整个站点
            return self._serve_static("sw.js")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path == "/api/meta":
            return self._send_json({
                "lenses": LENSES,
                "catalog": MODEL_CATALOG,
                "pool": [{**m, "price": ("免费" if not m["cin"] else f"￥{m['cin']}/￥{m['cout']}")}
                         for m in MODEL_POOL],
                "providers": {
                    pid: {"label": p["label"], "hint": p["hint"], "default_base_url": p["base_url"],
                          "supports_model_list": p.get("supports_model_list", False)}
                    for pid, p in PROVIDER_PRESETS.items()
                },
            })
        if path == "/api/config":
            return self._send_json(public_config(load_config()))
        if path == "/api/version":
            return self._send_json({"version": build_version()})
        if path.startswith("/api/job/"):
            rest = path[len("/api/job/"):]
            if rest.endswith("/stream"):
                return self._stream_job(rest[: -len("/stream")])
            job = get_job(rest)
            if not job:
                return self._send_json({"error": "任务不存在或已过期"}, 404)
            q, snap = job.subscribe()
            job.unsubscribe(q)
            return self._send_json(snap)
        if path == "/api/draw":
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            count = 12
            for part in query.split("&"):
                if part.startswith("count="):
                    try:
                        count = int(part[6:])
                    except ValueError:
                        pass
            return self._send_json(draw_random_round(count))
        if path == "/api/history":
            ensure_dirs()
            items = []
            for f in sorted(HISTORY_DIR.glob("*.json"), reverse=True)[:60]:
                try:
                    rec = json.loads(f.read_text(encoding="utf-8"))
                    items.append({"id": rec["id"], "title": rec.get("title", ""),
                                  "created_at": rec.get("created_at", ""),
                                  "chars": len(rec.get("final") or "")})
                except Exception:
                    continue
            return self._send_json({"items": items})
        if path.startswith("/api/history/"):
            hid = path.rsplit("/", 1)[-1]
            f = HISTORY_DIR / f"{hid}.json"
            if not f.is_file():
                return self._send_json({"error": "not found"}, 404)
            return self._send_json(json.loads(f.read_text(encoding="utf-8")))
        self.send_error(404)

    def do_DELETE(self):  # noqa: N802
        if not self._guard():
            return
        path = self.path.split("?")[0]
        if path.startswith("/api/history/"):
            hid = path.rsplit("/", 1)[-1]
            f = HISTORY_DIR / f"{hid}.json"
            if f.is_file():
                f.unlink()
            return self._send_json({"ok": True})
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        try:
            if path == "/api/login":
                payload = self._read_json()
                cfg = load_config()
                if not auth_enabled(cfg):
                    return self._send_json({"ok": True, "note": "未设置密码"})
                if hmac.compare_digest(str(payload.get("password") or ""),
                                       (cfg["access"].get("password") or "")):
                    token = auth_token(cfg)
                    body = json.dumps({"ok": True}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Set-Cookie",
                                     f"{COOKIE_NAME}={token}; Path=/; Max-Age={60*60*24*90}; SameSite=Lax; HttpOnly")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                return self._send_json({"ok": False, "error": "密码错误"}, 401)

            if not self._guard():
                return

            if path == "/api/config":
                payload = self._read_json()
                cfg = load_config()
                if payload.get("active_provider"):
                    cfg["active_provider"] = payload["active_provider"]
                if isinstance(payload.get("providers"), dict):
                    for pid, val in payload["providers"].items():
                        slot = cfg["providers"].setdefault(pid, {})
                        if val.get("api_key") is not None:
                            slot["api_key"] = val.get("api_key", "")
                        if val.get("base_url"):
                            slot["base_url"] = val["base_url"]
                for field in ("default_models", "default_aggregator", "temperature", "language",
                              "max_tokens", "max_concurrency", "layered_summary",
                              "idle_timeout", "hard_timeout"):
                    if payload.get(field) is not None:
                        cfg[field] = payload[field]
                if isinstance(payload.get("network"), dict):
                    net = cfg.setdefault("network", {"force_ipv4": True, "proxy": ""})
                    for k in ("force_ipv4", "proxy"):
                        if payload["network"].get(k) is not None:
                            net[k] = payload["network"][k]
                    apply_network_options(cfg)
                if isinstance(payload.get("access"), dict) and payload["access"].get("password") is not None:
                    cfg.setdefault("access", {})["password"] = str(payload["access"]["password"])
                save_config(cfg)
                return self._send_json({"ok": True, "config": public_config(cfg)})

            if path == "/api/models":
                payload = self._read_json()
                cfg = load_config()
                provider = resolve_provider(cfg, payload.get("provider"))
                if payload.get("api_key"):
                    provider["api_key"] = payload["api_key"]
                if payload.get("base_url"):
                    provider["base_url"] = payload["base_url"].rstrip("/")
                return self._send_json({"items": list_upstream_models(provider, build_opener(cfg))})

            if path == "/api/run":
                payload = self._read_json()
                cfg = load_config()
                apply_network_options(cfg)
                st = stats_config(cfg)
                vid = self._visitor_id()
                # 防止被刷：每人每小时次数上限 + 全站每日花费上限
                per_hour = int(st.get("per_visitor_hourly") or 0)
                if per_hour and runs_last_hour(vid) >= per_hour:
                    return self._send_json(
                        {"error": f"每人每小时最多分析 {per_hour} 次，休息一会儿再来吧"}, 429)
                cap = float(st.get("daily_cost_cap") or 0)
                if cap and cost_today() >= cap:
                    return self._send_json(
                        {"error": "今天的额度已经用完了，明天再来吧"}, 429)
                job = Job(payload, cfg)
                job.visitor_id = vid
                job.client_ip = client_ip_of(self)
                job.client_ua = self.headers.get("User-Agent", "")
                job.client_referer = self.headers.get("Referer", "")
                register_job(job)
                threading.Thread(target=job.run, daemon=True).start()
                return self._send_json({"job_id": job.id, "ok": True})

            if path.startswith("/api/job/") and path.endswith("/stop"):
                job = get_job(path[len("/api/job/"): -len("/stop")])
                if not job:
                    return self._send_json({"error": "任务不存在"}, 404)
                job.stop_event.set()
                return self._send_json({"ok": True})
        except UpstreamError as exc:
            return self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        self.send_error(404)

    def _stream_job(self, job_id: str) -> None:
        """SSE：先把当前现场（快照）发给客户端，再持续推增量。
        手机锁屏断线后重新连上同一个任务，就能补齐全部内容。"""
        job = get_job(job_id)
        if not job:
            return self._send_json({"error": "任务不存在或已过期"}, 404)

        q, snap = job.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            self.wfile.write(f"data: {json.dumps(snap, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=EVENT_QUEUE_TIMEOUT)
                except queue.Empty:
                    if job.state.get("finished") and q.empty():
                        break
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if ev.get("event") == "__close__":
                    break
                chunk = f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端断开了（手机锁屏就是这种情况），任务继续在后台跑
        finally:
            job.unsubscribe(q)


def main() -> None:
    ensure_dirs()
    cfg = load_config()
    if not CONFIG_PATH.exists():
        save_config(cfg)
    apply_network_options(cfg)
    init_stats_db()
    st = stats_config(cfg)
    port = int(os.environ.get("MPA_PORT", "8765"))
    host = os.environ.get("MPA_HOST", "127.0.0.1")
    url = f"http://{host}:{port}/"
    server = ThreadingHTTPServer((host, port), Handler)

    active = resolve_provider(cfg, cfg.get("active_provider"))
    print("=" * 62)
    print(f"  {DISPLAY_NAME} 已启动")
    print(f"  打开： {url}")
    print(f"  供应商： {active['label']}")
    print(f"  API Key： {'已配置 ✓' if active['api_key'] else '未配置（打开页面后点右上角「设置」填写）'}")
    net = cfg.get("network") or {}
    print(f"  网络：   {'强制 IPv4' if net.get('force_ipv4', True) else '自动'} · "
          f"代理 {net.get('proxy') or '未设置'}")
    print(f"  密码：   {'已开启' if auth_enabled(cfg) else '未设置（任何人拿到网址都能用）'}")
    print(f"  统计后台： {url}admin/{st['admin_token']}")
    print("  停止： 按 Control + C")
    print("=" * 62)
    if os.environ.get("MPA_NO_BROWSER") != "1":
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
