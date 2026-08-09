# -*- coding: utf-8 -*-
# MIT License
#
# Copyright (c) 2026 3MF Manager Contributors
# SPDX-License-Identifier: MIT
# See LICENSE file for full license text.
"""
功能类的进一步子分类 + Dummy13 / Minecraft 子分类（仅在对应父类 >30 时使用）。
所有分类相关的子类逻辑集中在此，供 build_analysis / generate_html / plan_structure / alias 复用，
保证两个分类器与规划/别名脚本完全一致。
"""
import mc_subcat  # Minecraft 子分类（已拆分为 动物被动/怪物敌对/角色Boss）

SEP = " · "

# ---------- 其他/未分类 重分类规则（仅作用于原兜底项） ----------
# 顺序即优先级：更具体的语义（动物/积木/文具/钥匙扣/武器）排在靠前，
# 避免"载具"等大类用宽泛子串(car/ev/tank/bike/raft/6x6)误吞无关文件。
UNCAT_RULES = [
    # 动物/生物（先于载具等，防止 car→Scarlet、自行车→LegoValves 误判）
    (["鹦鹉","parrot","macaw","海龟","turtle","卡皮巴拉","capybara","熊猫","panda","鳄鱼","crocodile","章鱼","octopus","河豚","海豚","dolphin","鲸","whale","兔","rabbit","柴犬","狗","dog","龙","dragon","鹤","crane","蝴蝶","butterfly","狼","wolf","狐狸","fox","羊","sheep","绵羊","蝾螈","axolotl","蛙","frog","蛇","snake","鹿","deer","松鼠","squirrel","鸟","bird","鹰","eagle","蜂","bee","蚂蚁","ant","昆虫","insect","宠物","pet","动物","生物"], "动物/生物模型"),
    # 积木/人偶
    (["lego","积木","minifigure","人偶","娃娃","拼图","brick man","brick minifigure","霸王龙","block26","人仔","小人","玩偶","布偶"], "积木/人偶"),
    # 钥匙扣/挂件
    (["钥匙扣","keychain","钥匙链","keyring","挂件","挂饰","挂绳","胸针","徽章","badge","冰箱贴","贴纸","标牌","姓名牌","狗牌","名牌","卡套","证件卡","工作牌","校卡"], "钥匙扣/挂件"),
    # 文具/工具配件（先于载具，避免 car→card 卡套、bike→水杯架）
    (["螺丝","screw","螺母","螺帽","紧固","轴承","bearing","卡尺","尺","量角器","三角板","游标","工具","tool","wrench","绘图","drawing","线材","线缆","cable","理线","绕线","卷线","充电器","charger","数据线","夹子","clip","防尘","dust","z柱","热床","底板","baseplate","焊接","riser","增高架","工具架","水杯架","杯架","支架","底座","口哨","whistle","哨子","印章","stamp","浮漂","float","日历","翻页","色卡","钟表","钟","clock","指力","水泵","pump","卷线器","集线器","笔架","笔筒","文具","课程表","算盘","小棒"], "文具/工具配件"),
    # 武器/刀剑
    (["刀","knife","blade","剑","sword","gun","枪","火箭发射器","加农炮","炮","武器","weapons","odm","火箭炮","伸缩剑","假剑","sliced_gun","gatling","匕首","战斧","大剑","军刀","迫击炮","炮弹","弹匣"], "武器/刀剑模型"),
    # 解压/指尖玩具
    (["fidget","减压","减压球","陀螺","不倒翁","捏捏","按压","helixcore","转运葫芦","竹蜻蜓","指尖","clicker","解压","点击器","指环","盗梦","旋转玩具","滚轮","spinner"], "解压/指尖玩具"),
    # 展示架/收纳墙
    (["shelf","货架","展示架","hsw","置物架","置物墙","connectors","模块化","skadis","multiboard","boards","收纳架","置物","层架","书架","展架","收纳墙","蜂窝"], "展示架/收纳墙"),
    # 载具/车船（用较具体的词，避免 car/ev/tank/bike/raft/6x6 误吞）
    (["坦克","truck","卡车","货车","厢式车","面包车","bus","巴士","赛车","ae86","cybertruck","赛博卡车","挖掘机","excavator","垃圾车","投石机","皮划艇","kayak","皮艇","船","boat","摩托","机车","越野车","吉普","jeep","五菱","小货车","小汽车","小轿车","跑车","山地车","mtb","单车","挖掘","推土机","压路机","装甲车","步战车","战车","火箭车","轮式","载具","车模","模型车","汽车","赛车模型","卡车模型"], "载具/车船模型"),
    # 机器人/机甲（先于摆件，避免 d13/头盔/头部 被摆件或载具吞）
    (["robot","机器人","mecha","机甲","unitree","宇树","凯普","单元g","blokees","bota","little g","robot es","robot c","leaper","lethal","扎古","defender","机械臂","机械手","无人机","droid","android","外骨骼","仿生","机器狗","机械"], "机器人/机甲模型"),
    # 摆件/装饰雕像（最后兜底，用较具体的词）
    (["雕塑","sculpture","浮雕","relief","骷髅","skull","moai","面具","雕像","墙面","wall art","星空","太空装饰","logo","曼陀罗","art","书签","bookmark","月亮","太空","venus","玻璃","glass","恶作剧","恶搞","整蛊","锤","hammer","雷神","thor","漫威","marvel","影视","周边","cosplay","吉祥物","mascot","三星堆","satan","撒旦","排球","volleyball","篮球","basketball","夸夸卡","标牌","billboard","signage","背包","backpack","行李箱","suitcase","摆件","装饰","手办摆件","花瓶","烛台","香薰","吊坠","项链","手链","耳环","戒指","饰品","挂饰","diy","iphone","手机壳","手机模型"], "摆件/装饰雕像"),
]

UNCAT_BY_LABEL = {label: kws for kws, label in UNCAT_RULES}  # 分类名 -> 关键词（供 categorize 按名取用，避免索引错位）

# 兜底文件名片段 -> 分类（针对无标题、无明确关键词的残留文件）
FILENAME_OVERRIDE = [
    ("cube.3mf", "机器人/机甲模型"),        # CUBE 小立方体人
    ("小立方体", "机器人/机甲模型"),
    ("k9 mini", "机器人/机甲模型"),         # Pax K9 机器人
    ("链条包", "摆件/装饰雕像"), ("小包包", "摆件/装饰雕像"), ("包.3mf", "摆件/装饰雕像"),
    ("female-scale100", "IP·Dummy13"),     # Dummy13 女体框架
    ("6dframe", "机器人/机甲模型"),          # 6DFrame 框架
]

# ---------- Dummy13 子分类 ----------
DUMMY_HELMET = ["helmet","头盔","面罩","hat","帽子","mask","面具","c3po","face","脸部","眼罩","goggle"]  # 头面部覆盖件
DUMMY_HEAD   = ["头","head","skull","骨","facial","hair","头发","发","miku","发型","头雕","脸","人头"]     # 头雕（不含头盔）
DUMMY_BONE   = ["骨架","骨骼","外骨骼","exo","exo-","bone","frame","tpu骨架","强化","关节加强"]           # 骨骼/结构
DUMMY_ARMOR  = ["armour","装甲","weapon","武器","sword","剑","火箭","rocket","parts","部件","配件","hand","手","gun","枪","blade","刀","wing","翼","尾","tail","adaptor","adapter","底座","stand","支架","back","背包"]
DUMMY_SET    = ["set","套装","all","skulls","全套","kit","军备","包","pack","装备","variants","变化"]

def dummy_subcat(filename, title="", folder=""):
    s = (filename + " " + (title or "") + " " + (folder or "")).lower()
    if any(k in s for k in DUMMY_BONE):
        return "骨骼结构"
    if any(k in s for k in DUMMY_HELMET):
        return "头盔面罩"
    if any(k in s for k in DUMMY_HEAD):
        return "头雕"
    if any(k in s for k in DUMMY_ARMOR):
        return "配件装甲"
    if any(k in s for k in DUMMY_SET):
        return "套装"
    return "躯干身体"

# ---------- 功能父类子分类 ----------
FUNC_SUB_PARENTS = {"手办/角色/玩具", "手办/马年", "收纳/盒体/容器", "收纳/网格系统",
                     "文具/工具配件", "摆件/装饰雕像"}
FUNC_SUB = {
    "手办/角色/玩具": [
        (["解压","fidget","clicker","点击","按压","捏","keycap","钥匙帽"], "解压点击"),
        (["战车","坦克","车","tank","car","摩托","船","龙舟","boat","飞机","直升机"], "载具战车"),
        (["章鱼","octopus","旋转","陀螺","spinner"], "章鱼旋转"),
        (None, "摆件装饰"),
    ],
    "手办/马年": [
        (["酷酷马","哭哭马","苦苦马","哭哭","酷酷"], "哭哭酷酷马"),
        (["小白马","新年","摇","喜庆","开心","马上"], "小白马新年"),
        (["八骏","名画","画","图"], "八骏图名画"),
        (None, "挂件摆件"),
    ],
    "收纳/盒体/容器": [
        (["弹匣","子弹","mag","magazine","ammo","弹仓"], "弹匣子弹盒"),
        (["药","干燥","pill","capsule","vitamin","密封","防潮","干燥剂"], "药盒干燥剂"),
        (None, "通用储物盒"),
    ],
    "收纳/网格系统": [
        (["drawer","抽屉"], "抽屉Drawer"),
        (["bin","料盒","料仓"], "料盒Bin"),
        (["tray","托盘","base","底座","plate","托"], "托盘底座Tray"),
        (["stacker","叠堆","stack","high","doubleheight"], "叠堆Stacker"),
        (None, "通用模块"),
    ],
    "文具/工具配件": [
        (["螺丝","screw","螺母","螺帽","紧固"], "螺丝紧固件"),
        (["防尘","dust","轴承","bearing","底板","焊接","baseplate","riser","导轨","rail","工具架","ams","增"], "打印机工作台配件"),
        (["线材","cable","线缆","绕线","整理器","charger","充电器","线夹","clip"], "线材理线"),
        (None, "文具小工具"),
    ],
    "摆件/装饰雕像": [
        (["deadpool","venom","vader","wukong","zelda","忍者神龟","tmnt","邓紫棋","三星堆","格里扎","satan","撒旦","雷神","thor","hammer","漫威","美少女"], "影视游戏周边"),
        (["雕塑","sculpture","浮雕","relief","骷髅","skull","moai","面具","雕像","墙面","wall art","星空","太空","logo","曼陀罗","art"], "雕塑装饰"),
        (None, "摆件小物"),
    ],
}

def func_subcat(cat, filename="", title="", folder=""):
    rules = FUNC_SUB.get(cat)
    if not rules:
        return None
    s = (filename + " " + (title or "") + " " + (folder or "")).lower()
    for kws, sub in rules:
        if kws is None or any(k in s for k in kws):
            return sub
    return None

# ---------- 统一 refine：对超 30 的父类补子分类 ----------
def refine(cat, folder, filename, title):
    if cat.startswith("IP·"):
        name = cat[3:].split(SEP, 1)[0].strip()
        if name == "Minecraft":
            sub = mc_subcat.minecraft_subcat(filename, title, folder)
            return "IP·Minecraft · " + (sub or "其他")
        if name == "Dummy13":
            sub = dummy_subcat(filename, title, folder)
            return "IP·Dummy13 · " + (sub or "其他")
        return cat
    if cat in FUNC_SUB_PARENTS:
        sub = func_subcat(cat, filename, title, folder)
        return cat + " · " + (sub or "其他")
    return cat
