# -*- coding: utf-8 -*-
"""别名生成 + 目标路径 + 工具函数测试。"""
import server
import classify


class TestAlias:
    def test_clean_horse(self):
        assert server.make_alias("13cm horsy.3mf", "可活动的马 快速打印 无需支撑") == "可活动的马"

    def test_fallback_filename(self):
        # 无标题时用清理后的文件名
        a = server.make_alias("B-Rail_Large_Cargo_1.0.3mf", "")
        assert a  # 非空
        assert "B-Rail" in a or "B" in a

    def test_no_trailing_space(self):
        a = server.make_alias("1tpu联动件柱体_117.3mf", "1/100元祖RX78 无需支撑打印 小腿带联动关节")
        assert a == a.rstrip()  # 无尾部空白
        assert len(a) <= 26


class TestHash:
    def test_sha256_deterministic(self, tmp_path):
        p = tmp_path / "f.bin"
        p.write_bytes(b"hello 3mf" * 100)
        h1 = server.sha256_file(str(p))
        h2 = server.sha256_file(str(p))
        assert h1 == h2
        assert len(h1) == 64


class TestCleanDesc:
    def test_remove_noise_words(self):
        # 直接测 classify._clean_desc（内部函数，随逻辑迁至 classify 模块）
        assert "分色" not in classify._clean_desc("分色 打印 元祖高达")
        assert "元祖" in classify._clean_desc("分色 打印 元祖高达")
