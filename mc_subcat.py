#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MIT License
#
# Copyright (c) 2026 3MF Manager Contributors
# SPDX-License-Identifier: MIT
# See LICENSE file for full license text.
"""
Minecraft 子分类：把 IP·Minecraft 下的文件进一步划分。
返回 None 表示不属于任何子类，留在 Minecraft/其他。
优先级：Creeper苦力怕 > 角色Boss > 怪物敌对 > 动物被动 > Cube盒子。
"""
CREEPER = ["creeper", "苦力怕", "爬行者"]

CHARACTER = [  # 角色 / Boss / 村民 / 可动主角
    "steve", "史蒂夫", "alex", "末影龙", "凋灵", "wither", "村民", "铁傀儡",
    "雪傀儡", "猪灵", "pillager", "掠夺者", "dragon", "龙", "minifigure", "人物",
    "deadpool",
]

MONSTER = [  # 敌对生物
    "ender", "末影", "ghast", "恶魂", "skeleton", "骷髅", "spider", "蜘蛛",
    "zombie", "僵尸", "slime", "史莱姆", "witch", "女巫", "guardian", "守卫",
    "ravager", "劫掠", "phantom", "幻翼", "hoglin", "疣猪", "strider", "炽足",
    "shulker", "潜影", "creeper",  # 已在 CREEPER 先命中
]

ANIMAL = [  # 被动 / 中性生物
    "chicken", "鸡", "axolotl", "蝾螈", "bear", "熊", "fox", "狐狸", "frog", "青蛙",
    "goat", "山羊", "horse", "马", "cow", "奶牛", "牛", "llama", "羊驼", "panda",
    "熊猫", "pig", "猪", "rabbit", "兔", "sheep", "绵羊", "羊", "squid", "鱿鱼",
    "bee", "蜜蜂", "ocelot", "豹猫", "dog", "狗", "cat", "猫", "fish", "鱼",
    "海豚", "dolphin", "蝙蝠", "bat", "axolotl",
]

CUBE = [
    "cube", "方块", "block", "盒", "box", "tnt", "ore", "矿石",
    "crafting", "工作台", "furnace", "熔炉", "nether", "portal",
    "附魔台", "walking", "行走",
]


def minecraft_subcat(filename, title="", folder=""):
    s = (filename + " " + (title or "") + " " + (folder or "")).lower()
    if any(k in s for k in CREEPER):
        return "Creeper苦力怕"
    if any(k in s for k in CHARACTER):
        return "角色Boss"
    if any(k in s for k in MONSTER):
        return "怪物敌对"
    if any(k in s for k in ANIMAL):
        return "动物被动"
    if any(k in s for k in CUBE):
        return "Cube盒子"
    return None
