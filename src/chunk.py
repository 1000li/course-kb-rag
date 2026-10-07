"""M3a 切块层：干净课文本 → chunks 表（数据集的一行）。

- 输入：data/interim/cleaned/{doc_id}/L{nn}.txt（含 ⟦Pn⟧ 页标记）
- 结构：节标题（一、二、…）切大块；栏目（知识点一览/课堂活动/思考题/代码段/课后任务）
  由关键词启发式判定，规则与已知局限见 docs/annotation-spec.md（annotate 阶段生成）
- 大小：正文按段贪心装包目标 ≤500 字，硬上限 800（超限按段/按行递归切分）；
  相邻 <50 字且同 content_type 同节的块合并
- 页标记不进入 text；块的 page_start/page_end 取块内首尾页标记（物理页码）
- 幂等：同 doc 同 pipeline_version 重跑先删后插；每 doc 一条 pipeline_runs

用法：py -3 -m src.chunk
"""

import json
import re
import sys
import uuid
from datetime import datetime, timezone

from .db import ROOT, init_db
from .parse import KP_HEADER_RE, SECTION_RE

sys.stdout.reconfigure(encoding="utf-8")

CLEANED_DIR = ROOT / "data" / "interim" / "cleaned"
PIPELINE_VERSION = "0.1"

TARGET_MAX = 500   # 正文装包目标上限
HARD_MAX = 800     # 全类型硬上限
MERGE_MIN = 50     # 短块合并阈值

MARKER_RE = re.compile(r"⟦P(\d+)⟧")
# 课标题行（每课首块判定用）
LESSON_TITLE_RE = re.compile(r"^第\s*[0-9一二三四五六七八九十]+\s*课\s*[：:]")
# 栏目头：短前缀（≤12 字）+ 冒号
COLON_HEAD_RE = re.compile(r"^(.{2,12}?)[：:]")
POST_CLASS_RE = re.compile(r"^(课后任务|课后探索地图)")   # 课后任务（到课尾）
SECTION_POST_RE = re.compile(r"课后任务")                 # 节标题含「课后任务」
SUBSECTION_RE = re.compile(r"^\d{1,2}\s*[.、．]\s*\S")    # 数字小节（1. xxx）
THINK_KW = ("思考",)                                      # → 思考题
ACTIVITY_KW = ("实验", "练习", "练一练", "活动", "游戏", "显微镜", "任务", "挑战",
               "小组", "动手", "观察", "讨论", "扮演", "制作", "设计", "实践",
               "测试", "调试", "探索", "卡片")            # → 课堂活动
CJK_RE = re.compile(r"[一-鿿]")

CODE_START_RE = re.compile(
    r"^(import\s|from\s+\w+\s+import|def\s|class\s|while\s|if\s|elif\s"
    r"|for\s+.*\bin\s|print\(|input\(|#)")
CODE_KW_RE = re.compile(
    r"^(if\s|elif\s|else\s*:|for\s|while\s|try\s*:|except|finally\s*:|return\b"
    r"|break\b|continue\b|pass\b|raise\s|with\s|def\s|class\s|import\s|from\s"
    r"|print\(|input\(|#)")
CODE_CHARS = set("=(){}[],:.")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_codeish(line: str) -> bool:
    """代码区续行判定：去注释后无 CJK 且含代码字符，或是极短换行残片。"""
    s = line if line.startswith("#") else line.split("#")[0].strip()
    if not s or CODE_KW_RE.match(s):
        return True
    if len(s) <= 2:
        return True  # PDF 换行残片（如「值」）
    if CJK_RE.search(s):
        return False
    return any(c in CODE_CHARS for c in s)


def classify_column(prefix: str) -> str:
    if any(k in prefix for k in THINK_KW):
        return "思考题"
    if any(k in prefix for k in ACTIVITY_KW):
        return "课堂活动"
    return "正文"


def column_has_content(u) -> bool:
    """栏目块是否已收到头部之外的内容（头行冒号后带文也算）。"""
    n_lines = sum(len(p.lines) for p in u.paras)
    if n_lines >= 2:
        return True
    first = u.paras[0].lines[0] if u.paras and u.paras[0].lines else ""
    m = COLON_HEAD_RE.match(first)
    return bool(m and first[m.end():].strip())


class Para:
    __slots__ = ("lines", "pages")

    def __init__(self):
        self.lines: list[str] = []
        self.pages: list[int] = []

    def chars(self) -> int:
        return sum(len(l) for l in self.lines)


class Unit:
    """同 content_type 同节的段落流，切块的最小候选。"""

    __slots__ = ("ctype", "section", "paras")

    def __init__(self, ctype: str, section: str | None):
        self.ctype = ctype
        self.section = section
        self.paras: list[Para] = []

    def add_line(self, line: str, page: int):
        if not self.paras or not self.paras[-1].lines:
            self.paras.append(Para())
        self.paras[-1].lines.append(line)
        self.paras[-1].pages.append(page)

    def break_para(self):
        if self.paras and self.paras[-1].lines:
            self.paras.append(Para())

    def text(self) -> str:
        return "\n".join("\n".join(p.lines) for p in self.paras if p.lines).strip()

    def page_range(self) -> tuple[int | None, int | None]:
        ps = [pg for p in self.paras for pg in p.pages]
        return (min(ps), max(ps)) if ps else (None, None)


def find_kp_end(lines: list[str], start: int) -> int:
    """kp 列表区：引导语行起，编号条目及其续行止。返回结束行号（不含）。"""
    items, gap, i = 0, False, start + 1
    item_re = re.compile(r"^\s*(\d+)\s*[、.．]")
    while i < len(lines):
        s = lines[i].strip()
        if MARKER_RE.fullmatch(s):
            i += 1
            continue
        if not s:
            gap = True
            i += 1
            continue
        m = item_re.match(s)
        if m and int(m.group(1)) == items + 1:
            items += 1
            gap = False
            i += 1
            continue
        if items == 0 or gap or SECTION_RE.match(s):
            break
        i += 1  # 条目续行
    return i


def split_lesson(text: str) -> list[Unit]:
    """课文本 → 有序 Unit 流。页标记消费为页码，不进文本。"""
    lines = text.splitlines()
    units: list[Unit] = []
    cur: Unit | None = None
    cur_section: str | None = None
    page = 0
    in_code = False
    i = 0

    def flush():
        nonlocal cur
        if cur is not None and cur.text():
            units.append(cur)
        cur = None

    while i < len(lines):
        s = lines[i].strip()
        m = MARKER_RE.fullmatch(s)
        if m:
            page = int(m.group(1))
            i += 1
            continue
        if not s:
            if not in_code and cur is not None:
                # 栏目块（思考题/课堂活动）已收到内容后遇空行即收束，
                # 防止栏目块吞掉后续正文（栏目内容多为单段）
                if cur.ctype in ("思考题", "课堂活动") and column_has_content(cur):
                    flush()
                else:
                    cur.break_para()
            i += 1
            continue

        # 代码区续行 / 收尾
        if in_code:
            if SECTION_RE.match(s) or POST_CLASS_RE.match(s) or not is_codeish(s):
                flush()
                in_code = False
            else:
                cur.add_line(s, page)
                i += 1
                continue
        # 代码区开始（去注释后无 CJK，防误判正文）
        if CODE_START_RE.match(s) and not CJK_RE.search(s.split("#")[0]):
            flush()
            cur = Unit("代码段", cur_section)
            in_code = True
            cur.add_line(s, page)
            i += 1
            continue

        # 知识点一览（课首）
        if KP_HEADER_RE.search(s):
            flush()
            kp = Unit("知识点一览", cur_section)
            kp.add_line(s, page)
            j = find_kp_end(lines, i)
            for k in range(i + 1, j):
                t = lines[k].strip()
                mm = MARKER_RE.fullmatch(t)
                if mm:
                    page = int(mm.group(1))
                    continue
                if t:
                    kp.add_line(t, page)
            units.append(kp)
            i = j
            continue

        # 课后任务（到课尾；含节标题形式的「六、课后任务与思考」）
        sec_post = bool(SECTION_RE.match(s) and SECTION_POST_RE.search(s))
        if POST_CLASS_RE.match(s) or sec_post:
            flush()
            if sec_post:
                cur_section = s
            cur = Unit("课后任务", cur_section)
            cur.add_line(s, page)
            i += 1
            continue

        # 节标题
        if SECTION_RE.match(s):
            flush()
            cur_section = s
            cur = Unit("正文", cur_section)
            cur.add_line(s, page)
            i += 1
            continue

        # 数字小节（1. xxx）：收束当前块，归入正文（chapter_path 仍用节标题）；
        # 课后任务内的编号列表项（1. 任务一…）不是小节，不收束
        if (SUBSECTION_RE.match(s) and len(s) <= 30
                and (cur is None or cur.ctype == "正文")):
            flush()
            cur = Unit("正文", cur_section)
            cur.add_line(s, page)
            i += 1
            continue

        # 栏目头（思考题 / 课堂活动）
        cm = COLON_HEAD_RE.match(s)
        if cm:
            ctype = classify_column(cm.group(1))
            if ctype != "正文":
                flush()
                cur = Unit(ctype, cur_section)
                cur.add_line(s, page)
                i += 1
                continue

        # 普通正文行
        if cur is None:
            cur = Unit("正文", cur_section)
        cur.add_line(s, page)
        i += 1

    flush()
    return units


def split_para(p: Para) -> list[Para]:
    """单段超硬上限 → 按行递归切分。"""
    if p.chars() <= HARD_MAX:
        return [p]
    out: list[Para] = []
    buf = Para()
    for line, pg in zip(p.lines, p.pages):
        if buf.lines and buf.chars() + len(line) > HARD_MAX:
            out.append(buf)
            buf = Para()
        buf.lines.append(line)
        buf.pages.append(pg)
    if buf.lines:
        out.append(buf)
    return out


def pack_units(units: list[Unit]) -> list[Unit]:
    """大小控制：硬上限 800（按段/按行递归切）；正文贪心装包 ≤500；
    <50 字且同 content_type 同节的相邻块合并。"""
    packed: list[Unit] = []
    for u in units:
        paras = [sp for p in u.paras if p.lines for sp in split_para(p)]
        buf: list[Para] = []
        blen = 0
        for p in paras:
            limit = TARGET_MAX if u.ctype == "正文" else HARD_MAX
            if buf and blen + p.chars() > limit:
                packed.append(make_unit(u, buf))
                buf, blen = [], 0
            buf.append(p)
            blen += p.chars()
        if buf:
            packed.append(make_unit(u, buf))

    merged: list[Unit] = []
    for u in packed:
        if (merged and len(u.text()) < MERGE_MIN
                and merged[-1].ctype == u.ctype and merged[-1].section == u.section):
            merged[-1].paras.extend(u.paras)
        else:
            merged.append(u)

    # 残余短块兜底合并（课标题块除外）：标题/标签样的短块前挪并入后块，
    # 其余并入前块——避免「示例代码片段」「二、知识衔接」这类孤儿标签块
    out: list[Unit] = []
    pending: Unit | None = None
    for u in merged:
        if pending is not None:
            u.paras = pending.paras + u.paras
            pending = None
        t = u.text()
        if len(t) < MERGE_MIN and not LESSON_TITLE_RE.match(t):
            headerish = len(t) <= 30 and not t.endswith(("。", "！", "？", ".", "!", "?"))
            if headerish or not out:
                pending = u                  # 并入后块
            else:
                out[-1].paras.extend(u.paras)  # 并入前块
            continue
        out.append(u)
    if pending is not None:
        if out:
            out[-1].paras.extend(pending.paras)
        else:
            out.append(pending)
    return out


def make_unit(template: Unit, paras: list[Para]) -> Unit:
    u = Unit(template.ctype, template.section)
    u.paras = list(paras)
    return u


def main() -> int:
    conn = init_db()
    docs = conn.execute(
        "SELECT doc_id, version FROM documents WHERE format = 'pdf' ORDER BY doc_id"
    ).fetchall()
    for d in docs:
        doc_id, version = d["doc_id"], d["version"]
        started = utcnow()
        run_id = f"chunk_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        try:
            # 幂等：同 doc 同 pipeline_version 重跑先删后插
            conn.execute(
                "DELETE FROM chunks WHERE doc_id = ? AND pipeline_version = ?",
                (doc_id, PIPELINE_VERSION))
            lessons = conn.execute(
                "SELECT lesson_no FROM lessons WHERE doc_id = ? ORDER BY lesson_no",
                (doc_id,)).fetchall()
            n_chunks, total_chars, over = 0, 0, 0
            now = utcnow()
            for les in lessons:
                no = les["lesson_no"]
                src = CLEANED_DIR / doc_id / f"L{no:02d}.txt"
                units = pack_units(split_lesson(src.read_text(encoding="utf-8")))
                lesson_id = f"{doc_id}_L{no:02d}"
                seq = 0
                for u in units:
                    text = u.text()
                    if not text:
                        continue
                    seq += 1
                    ctype = u.ctype
                    # 每课首块若仅为课标题 → content_type 记为「课标题」（index 阶段排除）
                    if (seq == 1 and len(text) <= 60
                            and LESSON_TITLE_RE.match(text)):
                        ctype = "课标题"
                    page_start, page_end = u.page_range()
                    chapter = f"第{no}课" + (f" > {u.section}" if u.section else "")
                    conn.execute(
                        """INSERT INTO chunks
                           (chunk_id, lesson_id, doc_id, lesson_no, section_title,
                            content_type, chapter_path, text, char_count,
                            page_start, page_end, knowledge_tags,
                            pipeline_version, superseded_by, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?)""",
                        (f"{doc_id}_L{no:02d}_{seq:03d}", lesson_id, doc_id, no,
                         u.section, ctype, chapter, text, len(text),
                         page_start, page_end, PIPELINE_VERSION, now))
                    n_chunks += 1
                    total_chars += len(text)
                    if len(text) > HARD_MAX:
                        over += 1
            stats = {"chunks": n_chunks,
                     "avg_chars": round(total_chars / n_chunks, 1) if n_chunks else 0,
                     "over_hard_max": over, "pipeline_version": PIPELINE_VERSION}
            conn.execute(
                """INSERT INTO pipeline_runs
                   (run_id, stage, doc_version, started_at, finished_at, stats, status)
                   VALUES (?, 'chunk', ?, ?, ?, ?, 'success')""",
                (run_id, version, started, utcnow(),
                 json.dumps(stats, ensure_ascii=False)))
            conn.commit()
            print(f"[切块] {doc_id} ({version}): {n_chunks} 块，"
                  f"均长 {stats['avg_chars']}，超上限 {over}")
        except Exception as e:
            conn.rollback()
            conn.execute(
                """INSERT INTO pipeline_runs
                   (run_id, stage, doc_version, started_at, finished_at, stats, status)
                   VALUES (?, 'chunk', ?, ?, ?, ?, 'failed')""",
                (run_id, version, started, utcnow(),
                 json.dumps({"error": str(e)}, ensure_ascii=False)))
            conn.commit()
            raise
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
