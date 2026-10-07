# -*- coding: utf-8 -*-
"""fix_v12.py — 从 v1.1 手术生成 v1.2（可重跑）。

用法: py src/fix_v12.py
步骤: 1) 对 v1.1 前 71 页做 redact+insert 手术
      2) 重编译 tmp/lesson15.typ (typst) 并替换最后 2 页
      3) 保存 v1.2 并做文本级验证 + 渲染代表性区域 PNG
"""
import os, sys, subprocess, json
import pymupdf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "raw", "人工智能主题课程讲义v1.1.pdf")
OUT = os.path.join(ROOT, "data", "raw", "人工智能主题课程讲义v1.2.pdf")
TMP = os.path.join(ROOT, "tmp")
V12 = os.path.join(TMP, "v12")
TYPST = r"C:\Users\YUQIL\AppData\Local\Microsoft\WinGet\Links\typst.exe"
LESSON15_TYP = os.path.join(TMP, "lesson15.typ")
LESSON15_PDF = os.path.join(TMP, "lesson15.pdf")

MSYH = r"C:\Windows\Fonts\msyh.ttc"      # 微软雅黑 (正文中文)
MSYHB = r"C:\Windows\Fonts\msyhbd.ttc"   # 微软雅黑 Bold (标题/加粗)
CONSOLA = r"C:\Windows\Fonts\consola.ttf"
CONSOLAB = r"C:\Windows\Fonts\consolab.ttf"

_font_cache = {}
def F(path):
    if path not in _font_cache:
        _font_cache[path] = pymupdf.Font(fontfile=path)
    return _font_cache[path]

def rgb(color_int):
    return ((color_int >> 16 & 255) / 255, (color_int >> 8 & 255) / 255, (color_int & 255) / 255)

STATUS = {}   # item_no -> (status, note)
def report(item, ok, note=""):
    STATUS[item] = ("完成" if ok else "失败", note)
    if not ok:
        print(f"  [ITEM {item}] FAILED: {note}")

# ---------------- 页面结构辅助 ----------------

def get_lines(page):
    """返回 [{rect,text,origin,size,color}] 按 y,x 排序。"""
    lines = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            spans = [s for s in l["spans"] if s["text"]]
            txt = "".join(s["text"] for s in l["spans"])
            if not txt.strip():
                continue
            sp0 = min(spans, key=lambda s: s["bbox"][0])
            lines.append({
                "rect": pymupdf.Rect(l["bbox"]), "text": txt,
                "origin": sp0["origin"], "size": sp0["size"], "color": sp0["color"],
            })
    lines.sort(key=lambda L: (round(L["rect"].y0, 1), L["rect"].x0))
    return lines

def find_line(page, needle):
    """search_for(needle) 的第一个命中及其所在行。"""
    rects = page.search_for(needle)
    if not rects:
        raise RuntimeError(f"search_for 未命中: {needle!r} (page {page.number+1})")
    r = rects[0]
    for L in get_lines(page):
        if L["rect"].intersects(r):
            return L, r
    raise RuntimeError(f"找不到行: {needle!r}")

def fill_under(page, rect):
    """检测 redact 区域的底色：包含区域中心的填充矩形中面积最小（最内层）者。"""
    c = rect + (-2, -2, 2, 2)
    center = pymupdf.Point((c.x0 + c.x1) / 2, (c.y0 + c.y1) / 2)
    best, best_area = (1, 1, 1), None
    for dr in page.get_drawings():
        if dr["type"] in ("f", "fs") and dr.get("fill") is not None:
            if dr["rect"].contains(center):
                area = dr["rect"].width * dr["rect"].height
                if best_area is None or area < best_area:
                    best, best_area = dr["fill"], area
    return best

# ---------------- 换行重排 ----------------

NO_START = "，。、；：？！）】》”’％—…·"   # 不允许出现在行首（挤压到上一行尾）

def tokenize(text):
    toks, cur = [], ""
    for ch in text:
        is_cjk = ('一' <= ch <= '鿿') or ch in NO_START or ch in "（【《“‘、。，；：？！）：；，。"
        if ch == " ":
            if cur: toks.append(cur); cur = ""
            toks.append(" ")
        elif is_cjk:
            if cur: toks.append(cur); cur = ""
            toks.append(ch)
        else:
            cur += ch
    if cur: toks.append(cur)
    return toks

def wrap(text, font, size, widths):
    """按每行可用宽度贪心断行，返回行列表。"""
    lines = [""]
    for t in tokenize(text):
        w_avail = widths[min(len(lines) - 1, len(widths) - 1)]
        if t == " " and not lines[-1]:
            continue
        if not lines[-1] or font.text_length(lines[-1] + t, size) <= w_avail:
            lines[-1] += t
        elif t in NO_START:          # 标点悬挂：允许轻微超出
            lines[-1] += t
        else:
            lines.append("" if t == " " else t)
    return lines

def dyn_old(text, a, b):
    i = text.index(a)
    j = text.index(b, i) + len(b)
    return text[i:j]

# ---------------- 手术操作 ----------------
# 每个 op 是 prepare(page) -> (redact_rects, insert_fn)

def op_simple(page, needle, new, fontfile, item, occurrence=0):
    """原处替换（新文本应不比旧文本宽出所在行剩余空间）。"""
    rects = page.search_for(needle)
    if not rects or len(rects) <= occurrence:
        raise RuntimeError(f"item{item}: 未命中 {needle!r}")
    r = rects[occurrence]
    L, _ = find_line(page, needle)
    sp_size, sp_color, org_y = L["size"], L["color"], L["origin"][1]
    def ins(pg):
        pg.insert_text((r.x0, org_y), new, fontname="f" + str(item),
                       fontfile=fontfile, fontsize=sp_size, color=rgb(sp_color))
    return [r], ins

def op_tail(page, anchor, new_tail, fontfile, item):
    """从 anchor 起到行尾整体替换。"""
    L, r = find_line(page, anchor)
    rect = pymupdf.Rect(r.x0, L["rect"].y0, L["rect"].x1 + 0.5, L["rect"].y1)
    def ins(pg):
        pg.insert_text((r.x0, L["origin"][1]), new_tail, fontname="f" + str(item),
                       fontfile=fontfile, fontsize=L["size"], color=rgb(L["color"]))
    return [rect], ins

def op_delete_chars(page, anchor, n_tail_chars, item):
    """删除 anchor 命中串的最后 n_tail_chars 个字符（每字符宽 = 行字号）。"""
    L, r = find_line(page, anchor)
    x0 = r.x1 - n_tail_chars * L["size"]
    return [pymupdf.Rect(x0, L["rect"].y0, r.x1 + 0.3, L["rect"].y1)], lambda pg: None

def op_first_chars(page, anchor, n_chars, new, fontfile, item):
    """替换 anchor 命中串的前 n_chars 个字符（等宽假设）。"""
    L, r = find_line(page, anchor)
    rect = pymupdf.Rect(r.x0 - 0.3, L["rect"].y0, r.x0 + n_chars * L["size"] + 0.3, L["rect"].y1)
    def ins(pg):
        pg.insert_text((r.x0, L["origin"][1]), new, fontname="f" + str(item),
                       fontfile=fontfile, fontsize=L["size"], color=rgb(L["color"]))
    return [rect], ins

def op_segments(page, anchor, segments, item):
    """从 anchor 到行尾，按 [(text, fontfile, color_int_or_None), ...] 分段重写。"""
    L, r = find_line(page, anchor)
    rect = pymupdf.Rect(r.x0, L["rect"].y0, L["rect"].x1 + 0.5, L["rect"].y1)
    size = L["size"]
    def ins(pg):
        x = r.x0
        for text, ff, col in segments:
            c = rgb(L["color"] if col is None else col)
            pg.insert_text((x, L["origin"][1]), text, fontname=f"f{item}_{id(text)%997}",
                           fontfile=ff, fontsize=size, color=c)
            x += F(ff).text_length(text, size)
    return [rect], ins

def op_number_swap(page, line_anchor, new_num, item):
    """替换行首独立数字编号 span（如 '2.' -> '1.'）。"""
    L, _ = find_line(page, line_anchor)
    spans = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            if abs(l["bbox"][1] - L["rect"].y0) < 1 and l["spans"]:
                spans = l["spans"]
                break
    if not spans:
        raise RuntimeError(f"item{item}: 行无 spans")
    s0 = spans[0]
    rect = pymupdf.Rect(s0["bbox"])
    org = s0["origin"]
    def ins(pg):
        pg.insert_text((org[0], org[1]), new_num, fontname="f" + str(item),
                       fontfile=MSYHB, fontsize=s0["size"], color=rgb(s0["color"]))
    return [rect], ins

def op_line_rewrite(page, anchor, transform, fontfile, item):
    """整行重写（transform: 原文 -> 新文）。"""
    L, _ = find_line(page, anchor)
    rect = pymupdf.Rect(L["rect"].x0, L["rect"].y0 - 0.6, L["rect"].x1 + 0.6, L["rect"].y1 + 0.6)
    newt = transform(L["text"])
    def ins(pg):
        pg.insert_text((L["origin"][0], L["origin"][1]), newt, fontname="f" + str(item),
                       fontfile=fontfile, fontsize=L["size"], color=rgb(L["color"]))
    return [rect], ins

def op_reflow(page, start_anchor, end_anchor, old_spec, new_seg, item,
              fontfile=MSYH, sizes=None):
    """段落级重排：从 start_anchor 所在行到 end_anchor 所在行（同列），
    拼接行文本 -> 替换 -> 按原行宽/原基线重新断行写入。"""
    L0, _ = find_line(page, start_anchor)
    L1, _ = find_line(page, end_anchor)
    colx0 = min(L0["rect"].x0, L1["rect"].x0)
    sel = [L for L in get_lines(page)
           if L["rect"].y0 >= L0["rect"].y0 - 1 and L["rect"].y0 <= L1["rect"].y0 + 1
           and abs(L["rect"].x0 - colx0) <= 30]
    text = "".join(L["text"] for L in sel)
    if callable(old_spec):
        old = old_spec(text)
    elif isinstance(old_spec, tuple):
        old = dyn_old(text, old_spec[0], old_spec[1])
    else:
        old = old_spec
    if old not in text:
        raise RuntimeError(f"item{item}: 段落内未找到旧文本 {old[:30]!r}")
    newtext = text.replace(old, new_seg(old) if callable(new_seg) else new_seg, 1)
    x1 = max(L["rect"].x1 for L in sel)
    widths = [x1 - L["rect"].x0 for L in sel]
    size0 = sel[0]["size"]
    color = sel[0]["color"]
    cand = sizes or [size0, round(size0 - 0.6, 2), round(size0 - 1.05, 2)]
    chosen, wrapped = None, None
    for s in cand:
        w = wrap(newtext, F(fontfile), s, widths)
        if len(w) <= len(sel):
            chosen, wrapped = s, w
            break
    if chosen is None:
        raise RuntimeError(f"item{item}: 最小字号仍放不下 ({len(wrap(newtext, F(fontfile), cand[-1], widths))} > {len(sel)} 行)")
    if chosen != size0:
        report(item, True, f"字号回退 {size0} -> {chosen}")
    redacts = [pymupdf.Rect(L["rect"].x0, L["rect"].y0 - 0.6, L["rect"].x1 + 0.6, L["rect"].y1 + 0.6)
               for L in sel]
    def ins(pg):
        for i, linetext in enumerate(wrapped):
            if not linetext:
                continue
            pg.insert_text((sel[i]["origin"][0], sel[i]["origin"][1]), linetext,
                           fontname="f" + str(item), fontfile=fontfile,
                           fontsize=chosen, color=rgb(color))
    return redacts, ins

# ---------------- 手术清单 ----------------

def build_ops(doc):
    """返回 {page_index0: [op_prepare_fn,...]}"""
    P = lambda n: doc[n - 1]          # 1-based -> page object
    ops = {}
    def add(pno, fn):
        ops.setdefault(pno - 1, []).append(fn)

    # 1. p5 ChatGPT 的 1750 亿参数 -> 其前身 GPT-3 的 1750 亿参数
    add(5, lambda pg: op_simple(pg, "ChatGPT", "其前身", MSYH, 1))
    add(5, lambda pg: op_reflow(pg, "的1750", "的副作用。", ("的1750", "的副作用。"),
                                lambda old: "GPT-3 " + old, 1))
    # 2. p9 乌龟案例（侧栏 4 行重排；新文本较长，字号可回退）
    turtle_new = ("你知道吗？2017年有研究者把3D打印乌龟的表面纹理刻意改动，AI就把它认成了步枪"
                  "——说明AI的识别会被精心设计的干扰骗过（这叫“对抗样本”）哦！")
    add(9, lambda pg: op_reflow(pg, "你知道吗", "哦！", ("你知道吗", "哦！"), turtle_new, 2))
    # 3. p14 计算2000万种可能走法 -> 评估上千种可能走法
    def item3(pg):
        L, rz = find_line(pg, "万种可能走法")
        sp_calc = None
        for b in pg.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                for s in l["spans"]:
                    if abs(l["bbox"][1] - L["rect"].y0) < 1 and "计算" in s["text"]:
                        sp_calc = s
        if sp_calc is None:
            raise RuntimeError("item3: 找不到'计算' span")
        x0 = sp_calc["bbox"][2] - 2 * sp_calc["size"]
        rect = pymupdf.Rect(x0 - 0.3, L["rect"].y0, rz.x1 + 0.5, L["rect"].y1)
        def ins(p):
            p.insert_text((x0, L["origin"][1]), "评估上千种可能走法",
                          fontname="f3", fontfile=MSYH, fontsize=L["size"], color=rgb(L["color"]))
        return [rect], ins
    add(14, item3)
    # 4. p41 每秒155帧 -> 每秒约 65 帧
    add(41, lambda pg: op_tail(pg, "每秒155", "每秒约 65 帧的实时检测", MSYH, 4))
    # 5. p35 GPT 记忆表述
    mem_new = ("GPT 不会保存跨对话的长期记忆——新开一段对话它就不记得你；"
               "但在同一段对话里，它可以参考前面说过的内容。")
    add(35, lambda pg: op_reflow(pg, "GPT 没有", "练知识。", ("GPT 没有", "练知识。"), mem_new, 5))
    # 6. p36 奥运会举办地 -> 奥运会的比赛结果
    add(36, lambda pg: op_tail(pg, "奥运会举办地", "奥运会的比赛结果”）。", MSYH, 6))
    # 7. p29 2023年的ChatGPT拥有1700亿参数 -> 2022年发布的ChatGPT基于GPT-3.5（前身GPT-3拥有1750亿参数）
    add(29, lambda pg: op_reflow(pg, "2017 年发明的", "发现程序中的bug",
                                 ("2023 年的ChatGPT", "拥有1700 亿参数"),
                                 "2022 年发布的 ChatGPT 基于 GPT-3.5（前身 GPT-3 拥有 1750 亿参数）", 7))
    # 8. p62 肢体错位表述
    add(62, lambda pg: op_reflow(pg, "又能保证", "明显错误。", ("目前这项技术", "明显错误。"),
                                 "目前这项技术已显著减少早期AI 绘画常见的肢体错位问题，但仍可能出错，需要人工检查。", 8))
    # 9. p65 2019 -> 2018
    add(65, lambda pg: op_simple(pg, "2019", "2018", MSYH, 9))
    # 10. p68 1.2亿 -> 净增约1200万
    add(68, lambda pg: op_reflow(pg, "位，相当于", "机器做不到", ("相当于", "亿个就业机会"),
                                 "相当于净增了约 1200 万个就业机会", 10))
    # 11. p71 （2003）->（2004）
    add(71, lambda pg: op_simple(pg, "2003", "2004", MSYH, 11))
    # 12. p65 80%偏见表述
    add(65, lambda pg: op_reflow(pg, "就像人类需要道德约束", "的人类选择。",
                                 ("80%的AI", "算法本身。"),
                                 "大量AI 偏见问题与训练数据的质量密切相关，而非只由算法决定。", 12))
    # 13. p65 标题 当科技见人性 -> 当科技遇见人性
    add(65, lambda pg: op_tail(pg, "见人性", "遇见人性——AI 与伦理", MSYHB, 13))
    # 14. p7 标题 硬件 -> 算力
    add(7, lambda pg: op_first_chars(pg, "硬件的创世法则", 2, "算力", MSYHB, 14))
    # 15. p53/p54 HSV 刻度
    add(53, lambda pg: op_simple(pg, "170-180", "170-179", MSYH, 15))
    add(54, lambda pg: op_simple(pg, "H≈60°", "H≈30°", MSYH, 15))
    add(54, lambda pg: op_simple(pg, "200°-240°", "100°-120°", MSYH, 15))
    # 16. p62 色彩抖动值 -> 风格强度等参数
    add(62, lambda pg: op_reflow(pg, "一些参数", "而构图立意始终源于人类。", ("色彩抖动值", "85%"),
                                 "风格强度、结构强度等参数", 16))
    # 17. p63 比例参数 -> Midjourney 示例
    add(63, lambda pg: op_reflow(pg, "在提示词后追加参数", "为泼墨效果。", ("比例参数", "750"),
                                 "以 Midjourney 为例：--ar 16:9 设比例，--s 750 设风格强度", 17))
    # 18. p63 里昂美术馆 -> 假设表述
    add(63, lambda pg: op_reflow(pg, "AI 系统？", "专题展。", "法国里昂美术馆曾拒绝展出",
                                 "试想：如果一家美术馆拒绝展出", 18))
    # 19. p30 代码 "?" -> "？"; p31 正文 “?” -> “？”
    add(30, lambda pg: op_segments(pg, '"?"', [
        ('"', CONSOLA, 0x4070a0), ("？", MSYH, 0x4070a0), ('"', CONSOLA, 0x4070a0),
        (" ", CONSOLA, 0x007020), ("in", CONSOLAB, 0x007020),
        (" ", CONSOLA, 0x000000), ("sentence", CONSOLA, 0x000000)], 19))
    add(31, lambda pg: op_reflow(pg, "上面的示例代码", "输出结果是什么。", "“?”", "“？”", 19))
    # 20. get_distance 调用与定义匹配
    add(46, lambda pg: op_segments(pg, "get_distance(thumb_tip,", [
        ("get_distance(hand_landmarks.landmark, 4, 8)", CONSOLA, 0x000000),
        (" ", CONSOLA, 0x666666), ("<", CONSOLA, 0x666666),
        (" ", CONSOLA, 0x40a070), ("0.05", CONSOLA, 0x40a070),
        (":", CONSOLA, 0x000000)], 20))
    add(48, lambda pg: op_segments(pg, "get_distance(palm_center,", [
        ("get_distance(landmarks.landmark, 0, i)", CONSOLA, 0x000000)], 20))
    # 21. p44 OpenCV -> MediaPipe（标手指关节）
    add(44, lambda pg: op_reflow(pg, "关键点检测", "确定", "OpenCV", "MediaPipe", 21))
    # 22. p47 注释 手掌中心点 -> 手腕关键点
    add(47, lambda pg: op_tail(pg, "取手掌中心点", "取手腕关键点（0 号点）代表手部位置", MSYH, 22))
    # 23. p24 概率分布 -> 各类别的得分
    add(24, lambda pg: op_reflow(pg, "这段代码构建", "个关键点。", ("最终输出", "概率分布"),
                                 "最终输出 0-9 各类别的得分", 23))
    # 24. p23 上节课展示的MNIST... -> 在MNIST手写数字识别任务中
    add(23, lambda pg: op_reflow(pg, "要大量标注姓名", "的手写体。", ("上节课展示的", "demo 中"),
                                 "在MNIST 手写数字识别任务中", 24))
    # 25. p25 过拟合例子
    add(25, lambda pg: op_reflow(pg, "好的数据输入计算机", "学习参数。", ("如果发现", "（过拟合）"),
                                 "如果发现AI 对练过的照片很准、换新照片就出错（过拟合）", 25))
    # 26. p23-25 小节编号 2./3./4./5. -> 1./2./3./4.
    add(23, lambda pg: op_number_swap(pg, "机器学习类型探秘", "1.", 26))
    add(24, lambda pg: op_number_swap(pg, "深度学习揭秘", "2.", 26))
    add(25, lambda pg: op_number_swap(pg, "项目流程认知", "3.", 26))
    add(25, lambda pg: op_number_swap(pg, "轻量级代码实践", "4.", 26))
    # 27. p42-43 代码讲解编号 4.-8. -> 1.-5.
    def item27(pg, pairs):
        plans = []
        for b in pg.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                txt = "".join(s["text"] for s in l["spans"]).strip()
                if txt in pairs and 100 <= l["bbox"][0] <= 120:
                    s0 = l["spans"][0]
                    plans.append((pymupdf.Rect(s0["bbox"]), s0["origin"], s0["size"], pairs[txt]))
        if not plans:
            raise RuntimeError("item27: 未找到编号 span")
        redacts = [p[0] for p in plans]
        def ins(p):
            for _, org, size, newt in plans:
                p.insert_text((org[0], org[1]), newt, fontname="f27", fontfile=MSYH,
                              fontsize=size, color=(0, 0, 0))
        return redacts, ins
    add(42, lambda pg: item27(pg, {"4.": "1.", "5.": "2.", "6.": "3.", "7.": "4."}))
    add(43, lambda pg: item27(pg, {"8.": "5."}))
    # 28. p49 课后任务
    add(49, lambda pg: op_tail(pg, "尝试为游戏添加子弹发射功能",
                               "尝试增加第二种发射方式：当食指与中指间距小于阈值时也发射子弹", MSYH, 28))
    # 29. p51 机械臂夹伤
    add(51, lambda pg: op_reflow(pg, "安全提示", "操作时保持距离。",
                                 "机械臂的力度可能夹伤手指", "机械臂力度较大，可能夹伤手指", 29))
    # 30. p58 TA -> 学生
    add(58, lambda pg: op_tail(pg, "TA", "学生自己模仿练习。", MSYH, 30))
    # 31. p67 增加 -> 采取（整行重写：斜体行内混排会有风格/空隙问题）
    add(67, lambda pg: op_line_rewrite(pg, "增加哪些改进",
                                       lambda t: t.replace("增加", "采取"), MSYHB, 31))
    # 32. p3 人工智则 -> 人工智能则
    add(3, lambda pg: op_tail(pg, "人工智则源自", "人工智能则源自于超量级计算。", MSYH, 32))
    # 33. p30 示例代码课的 -> 示例代码的（删“课”）
    add(30, lambda pg: op_delete_chars(pg, "这段示例代码课", 1, 33))
    # 34. 全局 涉及到 -> 涉及（整行重写：部分斜体行字符 bbox 重叠，局部 redact 会误删邻字）
    for pno in [2, 7, 12, 16, 22, 27, 32, 40, 44, 50, 53, 57, 60, 65]:
        def make34(pno_):
            def f(pg):
                rects = pg.search_for("涉及到")
                if not rects:
                    raise RuntimeError(f"item34: p{pno_} 未找到'涉及到'")
                plans = []
                for r in rects:
                    for L in get_lines(pg):
                        if L["rect"].intersects(r):
                            plans.append(L)
                            break
                seen = set()
                uniq = []
                for L in plans:      # 同一行多处只重写一次
                    key = round(L["rect"].y0, 1)
                    if key not in seen:
                        seen.add(key)
                        uniq.append(L)
                def ins(p):
                    for L_ in uniq:
                        newt = L_["text"].replace("涉及到", "涉及")
                        p.insert_text((L_["origin"][0], L_["origin"][1]), newt,
                                      fontname="f34", fontfile=MSYH,
                                      fontsize=L_["size"], color=rgb(L_["color"]))
                return [pymupdf.Rect(L["rect"].x0, L["rect"].y0 - 0.6,
                                     L["rect"].x1 + 0.6, L["rect"].y1 + 0.6) for L in uniq], ins
            return f
        add(pno, make34(pno))
    return ops

# ---------------- 主流程 ----------------

def main():
    os.makedirs(V12, exist_ok=True)
    doc = pymupdf.open(SRC)
    assert doc.page_count == 73, doc.page_count
    ops = build_ops(doc)
    for pno0 in sorted(ops):
        page = doc[pno0]
        prepared = []
        for fn in ops[pno0]:
            try:
                prepared.append(fn(page))
            except Exception as e:
                print(f"  [p{pno0+1}] op 定位失败: {e}")
                raise
        for redacts, _ in prepared:
            for r in redacts:
                page.add_redact_annot(r, fill=fill_under(page, r))
        page.apply_redactions(
            images=getattr(pymupdf, "PDF_REDACT_IMAGE_NONE", 0),
            graphics=getattr(pymupdf, "PDF_REDACT_LINE_ART_NONE", 0))
        for _, ins in prepared:
            ins(page)
        print(f"p{pno0+1}: {len(prepared)} 处手术完成")

    # 35. 第15课: 重编译 lesson15.typ 并替换最后 2 页
    subprocess.run([TYPST, "compile", "lesson15.typ", "lesson15.pdf"], cwd=TMP, check=True)
    l15 = pymupdf.open(LESSON15_PDF)
    doc.delete_pages([71, 72])
    doc.insert_pdf(l15)
    assert doc.page_count == 73, doc.page_count

    doc.subset_fonts()   # 压缩嵌入字体（msyh.ttc 全量嵌入会膨胀到 ~29MB）
    doc.save(OUT, garbage=4, deflate=True)
    print("saved:", OUT)
    verify()

# ---------------- 验证 ----------------

def norm(s):
    return "".join(s.split())

def page_text_visual(page):
    """按视觉位置重排页面文本：词按行聚类(y 中心 ±3pt)，行按 y、词按 x 排序后无空格拼接。"""
    words = page.get_text("words")
    words = [w for w in words if w[4].strip()]
    words.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines = []
    for w in words:
        yc = (w[1] + w[3]) / 2
        if lines and abs(lines[-1][0] - yc) <= 3:
            lines[-1][1].append(w)
        else:
            lines.append([yc, [w]])
    out = []
    for yc, ws in lines:
        ws.sort(key=lambda w: w[0])
        out.append("".join(w[4] for w in ws))
    return "\n".join(out)

def verify():
    doc = pymupdf.open(OUT)
    os.makedirs(os.path.join(V12, "text"), exist_ok=True)
    texts = []
    for i, pg in enumerate(doc):
        t = page_text_visual(pg)
        texts.append(t)
        with open(os.path.join(V12, "text", f"p{i+1:03d}.txt"), "w", encoding="utf-8") as f:
            f.write(t)
    N = [norm(t) for t in texts]
    alltext = norm("".join(texts))
    fails = []
    def chk(item, pno, present=(), absent=()):
        scope = N[pno - 1] if pno else alltext
        for s in present:
            if norm(s) not in scope:
                fails.append(f"item{item} p{pno}: 缺少 {s!r}")
        for s in absent:
            if norm(s) in scope:
                fails.append(f"item{item} p{pno}: 仍存在 {s!r}")
    chk(1, 5, ["其前身GPT-3的1750亿参数"], ["ChatGPT的1750亿参数"])
    chk(2, 9, ["对抗样本", "3D打印乌龟"], ["只因没见过龟壳纹理", "算法步骤再完美"])
    chk(3, 14, ["评估上千种可能走法"], ["2000万种可能走法"])
    chk(4, 41, ["每秒约65帧"], ["每秒155帧"])
    chk(5, 35, ["不会保存跨对话的长期记忆", "参考前面说过的内容"], ["没有“记忆”功能"])
    chk(6, 36, ["奥运会的比赛结果"], ["奥运会举办地"])
    chk(7, 29, ["2022年发布的ChatGPT基于GPT-3.5", "1750亿参数"], ["1700亿参数", "2023年的ChatGPT拥有"])
    chk(8, 62, ["已显著减少早期AI绘画常见的肢体错位问题", "需要人工检查"], ["三只眼睛", "已经能有效避免"])
    chk(9, 65, ["2018年有个"], ["2019年有个"])
    chk(10, 68, ["净增了约1200万个就业机会"], ["1.2亿个就业机会"])
    chk(11, 71, ["（2004）"], ["（2003）"])
    chk(12, 65, ["与训练数据的质量密切相关"], ["80%的AI偏见"])
    chk(13, 65, ["当科技遇见人性"], ["科技见人性"])
    chk(14, 7, ["数据×算力的创世法则"], ["硬件的创世法则"])
    chk(15, 53, ["170-179"], ["170-180"])
    chk(15, 54, ["H≈30°", "100°-120°"], ["H≈60°", "200°-240°"])
    chk(16, 62, ["风格强度、结构强度等参数"], ["色彩抖动值"])
    chk(17, 63, ["以Midjourney为例：--ar16:9设比例，--s750设风格强度"], ["比例参数–ar"])
    chk(18, 63, ["试想：如果一家美术馆拒绝展出"], ["里昂"])
    chk(19, 30, ['"？"insentence'], ['"?"insentence'])
    chk(19, 31, ["或者“？”"], ["或者“?”"])
    chk(20, 46, ["get_distance(hand_landmarks.landmark,4,8)"], ["get_distance(thumb_tip"])
    chk(20, 48, ["get_distance(landmarks.landmark,0,i)"], ["get_distance(palm_center"])
    chk(21, 44, ["MediaPipe标出手指关节"], ["OpenCV标出手指关节"])
    chk(22, 47, ["取手腕关键点（0号点）代表手部位置"], ["取手掌中心点"])
    chk(23, 24, ["各类别的得分"], ["0-9的概率分布"])
    chk(24, 23, ["在MNIST手写数字识别任务中"], ["上节课展示的MNIST"])
    chk(25, 25, ["对练过的照片很准、换新照片就出错"], ["把狐狸错认成猫"])
    # 26/27 用 dict span 校验（编号是独立 span，词序重排可能不可靠）
    def span_num(pno, anchor, expect):
        pg = doc[pno - 1]
        rs = pg.search_for(anchor)
        if not rs:
            fails.append(f"item26 p{pno}: 找不到 {anchor!r}")
            return
        y = rs[0].y0
        nums = []
        for b in pg.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                for s in l["spans"]:
                    if abs(s["bbox"][1] - y) < 6 and s["bbox"][0] < rs[0].x0:
                        nums.append((s["bbox"][0], s["text"].strip()))
        nums.sort()
        if not nums or nums[-1][1] != expect:
            fails.append(f"item26/27 p{pno} {anchor!r}: 行首编号={nums[-1][1] if nums else None} 期望 {expect}")
    span_num(23, "机器学习类型探秘", "1.")
    span_num(24, "深度学习揭秘", "2.")
    span_num(25, "项目流程认知", "3.")
    span_num(25, "轻量级代码实践", "4.")
    def code_nums(pno, y_min, y_max, expect):
        pg = doc[pno - 1]
        got = []
        for b in pg.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                t = "".join(s["text"] for s in l["spans"]).strip()
                if y_min < l["bbox"][1] < y_max and 100 <= l["bbox"][0] <= 120 and t.endswith("."):
                    got.append(t)
        if got != expect:
            fails.append(f"item27 p{pno}: 编号={got} 期望 {expect}")
    code_nums(42, 555, 760, ["1.", "2.", "3.", "4."])
    code_nums(43, 100, 200, ["5."])
    chk(28, 49, ["尝试增加第二种发射方式"], ["尝试为游戏添加子弹发射功能"])
    chk(29, 51, ["机械臂力度较大，可能夹伤手指"], ["机械臂的力度可能夹伤手指"])
    chk(30, 58, ["让学生自己模仿练习"], ["让TA自己模仿练习"])
    chk(31, 67, ["采取哪些改进来避免"], ["增加哪些改进来避免"])
    chk(32, 3, ["人工智能则源自"], ["人工智则源自"])
    chk(33, 30, ["代码的功能是统计"], ["代码课的功能"])
    chk(34, None, [], ["涉及到"])
    # 35. 第15课（按原始行判断，不做跨行拼接）
    p72 = texts[71]
    if p72.count("人类学习与机器学习对比图") != 1:
        fails.append("item35: p72 对比图出现次数 != 1")
    if any("失控风险案例" in ln for ln in p72.splitlines()):
        fails.append("item35: 存在同行'失控风险案例'")
    if doc.page_count != 73:
        fails.append(f"页数异常: {doc.page_count}")
    # 渲染代表性区域 PNG
    shots = [
        (1, 5, (80, 150, 515, 240)), (2, 9, (310, 110, 515, 185)),
        (7, 29, (80, 240, 515, 325)), (13, 65, (80, 65, 515, 105)),
        (12, 65, (80, 378, 515, 435)), (20, 46, (80, 315, 515, 375)),
        (19, 30, (80, 675, 515, 715)), (27, 42, (80, 555, 515, 760)),
        (15, 54, (80, 170, 550, 235)), (10, 68, (80, 535, 515, 590)),
        (5, 35, (80, 498, 515, 540)), (34, 2, (80, 120, 515, 145)),
    ]
    for item, pno, clip in shots:
        pix = doc[pno - 1].get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=pymupdf.Rect(*clip))
        pix.save(os.path.join(V12, f"render_item{item:02d}_p{pno}.png"))
    # 锚点定位的补充渲染（覆盖其余手术点）
    extra = [
        (3, 14, "评估上千种"), (4, 41, "每秒约"), (6, 36, "比赛结果"),
        (9, 65, "2018"), (11, 71, "2004"), (14, 7, "算力的创世法则"),
        (8, 62, "已显著减少"), (16, 62, "风格强度、结构强度"),
        (17, 63, "Midjourney"), (18, 63, "试想"),
        (21, 44, "MediaPipe"), (22, 47, "手腕关键点"), (20, 48, "landmarks.landmark, 0, i"),
        (23, 24, "各类别的得分"), (24, 23, "识别任务中"), (25, 25, "练过的照片"),
        (26, 23, "机器学习类型探秘"), (26, 25, "轻量级代码实践"),
        (28, 49, "第二种发射方式"), (29, 51, "力度较大"), (30, 58, "学生自己模仿"),
        (31, 67, "采取哪些改进"), (32, 3, "人工智能则源自"), (33, 30, "代码的功能"),
        (19, 31, "或者"), (15, 53, "170-179"), (34, 7, "知识点"), (34, 44, "知识点"),
    ]
    for item, pno, anchor in extra:
        rs = doc[pno - 1].search_for(anchor)
        if not rs:
            continue
        r = rs[0]
        clip = pymupdf.Rect(60, r.y0 - 18, 540, r.y1 + 18) & doc[pno - 1].rect
        pix = doc[pno - 1].get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip)
        pix.save(os.path.join(V12, f"x_item{item:02d}_p{pno}_{anchor[:6]}.png"))
    # 第15课整页
    for pno in (72, 73):
        pix = doc[pno - 1].get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4))
        pix.save(os.path.join(V12, f"lesson15_p{pno}.png"))
    print("PNG 渲染完成 ->", V12)
    if fails:
        print("验证失败项:")
        for f_ in fails:
            print("  -", f_)
    else:
        print("全部文本验证通过 ✔  页数 =", doc.page_count)
    with open(os.path.join(V12, "verify_fails.json"), "w", encoding="utf-8") as f:
        json.dump(fails, f, ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main()
