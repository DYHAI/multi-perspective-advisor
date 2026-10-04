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
import mimetypes
import os
import queue
import socket
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
        "layered_summary": cfg.get("layered_summary", True),
        "default_lenses": DEFAULT_LENSES,
        "network": cfg.get("network", {"force_ipv4": True, "proxy": ""}),
    }


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
        self.q.put({"event": event, **data})

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
        except Exception as exc:  # noqa: BLE001
            self.emit("fatal", message=f"{type(exc).__name__}: {exc}")
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

    def log_message(self, fmt, *args):  # 静音
        pass

    # -- 工具 ------------------------------------------------------------
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        self.end_headers()
        self.wfile.write(body)

    # -- 路由 ------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
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
                return self._run_job(payload, cfg)
        except UpstreamError as exc:
            return self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        self.send_error(404)

    def _run_job(self, payload: dict, cfg: dict) -> None:
        job = Job(payload, cfg)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        worker = threading.Thread(target=job.run, daemon=True)
        worker.start()
        try:
            while True:
                try:
                    ev = job.q.get(timeout=EVENT_QUEUE_TIMEOUT)
                except queue.Empty:
                    if not worker.is_alive() and job.q.empty():
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
            job.stop_event.set()
        finally:
            worker.join(timeout=2)


def main() -> None:
    ensure_dirs()
    cfg = load_config()
    if not CONFIG_PATH.exists():
        save_config(cfg)
    apply_network_options(cfg)
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
