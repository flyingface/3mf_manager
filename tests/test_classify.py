# -*- coding: utf-8 -*-
"""分类器测试：IP 优先、功能分类、子分类、新增分类兜底。"""
import server


def c(folder, filename, title=""):
    return server.categorize(folder, filename, title)


class TestIP:
    def test_gundam_filename(self):
        assert c("", "RX78_gundam.3mf", "高达") == "IP·高达"

    def test_dummy13_title_priority_over_folder(self):
        # 物理在 Minecraft 文件夹，但标题含 dummy13 -> 归 Dummy13（含子类）
        assert c("Minecraft", "whatever.3mf", "DUMMY13武器包").startswith("IP·Dummy13")

    def test_pokemon(self):
        assert c("", "皮卡丘.3mf", "") == "IP·宝可梦"

    def test_dragonball_arale(self):
        assert c("", "阿拉蕾.3mf", "阿拉蕾 ARALE 鸟山明") == "IP·七龙珠"

    def test_delta_force(self):
        assert c("", "步战车.3mf", "三角洲行动DeltaForce") == "IP·三角洲"

    def test_failed_reclass_guard(self):
        # 完美沙鲁 -> 七龙珠
        assert c("", "Perfect_Cell.3mf", "完美沙鲁") == "IP·七龙珠"


class TestFunctional:
    def test_mayan_horse(self):
        assert c("", "天选黑马.3mf", "马年") == "手办/马年 · 挂件摆件"

    def test_dino(self):
        assert c("", "DinoEgg01.3mf", "恐龙 霸王龙") == "手办/恐龙"

    def test_storage_box(self):
        assert c("", "收纳盒.3mf", "Gridfinity 盒子") == "收纳/盒体/容器 · 通用储物盒"

    def test_vehicle(self):
        assert c("", "cybertruck.3mf", "赛博卡车") == "载具/车船模型"

    def test_scarlet_macaw_not_vehicle(self):
        # 回归：载具关键词 car 不应误吞 Scarlet（动物）
        assert c("", "Scarlet+Macaw1.3.3mf", "绯红金刚鹦鹉") == "动物/生物模型"


class TestSubcategory:
    def test_minecraft_subcat(self):
        assert server.target_of("IP·Minecraft", "minecraft-cow.3mf", "", "")[2] == "动物被动"
        assert server.target_of("IP·Minecraft", "creeper.3mf", "", "")[2] == "Creeper苦力怕"
        assert server.target_of("IP·Minecraft", "minecraft-zombie.3mf", "", "")[2] == "怪物敌对"

    def test_dummy13_subcat(self):
        assert server.target_of("IP·Dummy13", "Polygonal Head.3mf", "头", "")[2] == "头雕"


class TestTargetPath:
    def test_ip_target(self):
        assert server.target_relpath("IP·高达", "a.3mf", "高达") == "01_IP授权/高达Gundam"

    def test_func_target(self):
        assert server.target_relpath("手办/马年 · 挂件摆件", "a.3mf", "马") == "02_功能实用/马年/挂件摆件"

    def test_uncategorized(self):
        assert server.target_relpath("其他/未分类", "a.3mf", "") == "04_其他未分类"
