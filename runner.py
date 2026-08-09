#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MIT License
#
# Copyright (c) 2026 3MF Manager Contributors
# SPDX-License-Identifier: MIT
# See LICENSE file for full license text.
"""3MF Manager 命令行入口：uv run mfmanager 或 python runner.py [端口]"""
import os, sys

# 确保本文件所在目录在 sys.path（flat 布局）
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from server import main

if __name__ == "__main__":
    main()
