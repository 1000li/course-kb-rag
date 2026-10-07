"""M5 混合检索：向量召回 k=20 + BM25 召回 k=20 → RRF 融合取 top-5。

- 默认口径（架构文档 §4.1）：仅 active 文档且 superseded_by IS NULL 的块；
  scope_doc_id 显式指定其他版本时放开到该版本（版本对比场景，§4.2）
- 查询理解（§4 第 8 条，2026-10-08 第二次真实提问驱动）：课号识别（"第N课"scope 到
  lesson_no=N，召回阶段过滤）+ 保守的同义词扩展（作业→思考题/课后任务/练习 等）
- 课标题块不入索引（index.py 已排除），BM25 语料与索引口径一致
- 页码口径：物理页码（§4.7）
- 返回每块的融合分数及向量/BM25 各自分数与名次，供阈值标定分析

用法：py -3 -m src.retrieve "问题" [--doc doc_001]
"""

import json
import re
import sys
from collections import defaultdict

import jieba
from rank_bm25 import BM25Okapi

from .db import init_db, load_vec
from .index import MODEL_NAME, check_index, get_embedder, embed_texts

sys.stdout.reconfigure(encoding="utf-8")
jieba.setLogLevel(20)

K_VEC = 20
K_BM25 = 20
TOP_N = 5
RRF_K = 60

# 查询理解：课号识别（"第1课"/"第一课"/"第十二节课"）
LESSON_PAT = re.compile(r"第\s*([0-9]+|[零一二两三四五六七八九十]+)\s*节?课")
CN_DIGIT = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 查询理解：同义词扩展（保守，只加这几组；讲义实际用词 ≠ 用户口语用词）
SYNONYM_EXPANSIONS = {
    "作业": "思考题 课后任务 练习",
    "讨论": "思考题 讨论",
    "练习题": "练习 实践",
}


def _cn_to_int(s: str) -> int | None:
    """中文/阿拉伯数字 → int；无法解析返回 None。支持 1-99（十/十二/二十）。"""
    if s.isdigit():
        return int(s)
    if "十" in s:
        left, _, right = s.partition("十")
        if left and left not in CN_DIGIT or right and right not in CN_DIGIT:
            return None
        tens = CN_DIGIT[left] if left else 1
        ones = CN_DIGIT[right] if right else 0
        return tens * 10 + ones
    if len(s) == 1 and s in CN_DIGIT:
        return CN_DIGIT[s]
    return None


def parse_lesson_no(question: str) -> int | None:
    """识别 query 中的"第N课/第N节课"；识别不到返回 None（走全库检索）。
    多处提及时取第一个（保守：课号对比类提问不在此层处理）。"""
    m = LESSON_PAT.search(question)
    return _cn_to_int(m.group(1)) if m else None


def expand_query(question: str) -> str:
    """同义词扩展：命中映射表的关键词时把扩展词追加到 query 尾部
    （嵌入与 BM25 共用扩展后文本）。"""
    extra = [exp for key, exp in SYNONYM_EXPANSIONS.items() if key in question]
    return question + (" " + " ".join(extra) if extra else "")


def tokenize(text: str) -> list[str]:
    # 拉丁字母小写归一化（与 index.normalize_text 对齐）：保证 BM25 语料与查询
    # 两侧词元大小写一致，避免 "AI" 与 "ai" 被视为不同词。
    return [t for t in jieba.lcut(text.lower()) if t.strip() and not t.isspace()]


class Retriever:
    def __init__(self, conn):
        self.conn = conn
        check_index(conn)  # 模型配置与索引一致性校验（§6）
        load_vec(conn)
        self.chunks = {r["chunk_id"]: dict(r) for r in conn.execute(
            """SELECT chunk_id, doc_id, lesson_no, section_title, content_type,
                      chapter_path, text, page_start, page_end
               FROM chunks
               WHERE content_type != '课标题' AND superseded_by IS NULL""")}
        self.by_doc: dict[str, list[str]] = defaultdict(list)
        for cid, c in self.chunks.items():
            self.by_doc[c["doc_id"]].append(cid)
        self.active_doc = conn.execute(
            "SELECT doc_id FROM documents WHERE status = 'active'").fetchone()[0]
        # BM25 语料与检索 scope 一致（idf 依赖语料，scope 过滤必须在打分前生效）；
        # 课号识别命中时语料进一步收缩到该课
        self._bm25: dict[tuple, tuple] = {}  # (doc_id, lesson_no|None) -> (BM25Okapi, [chunk_id])
        self.model = get_embedder()

    def _bm25_for(self, doc_id: str, lesson_no: int | None = None):
        key = (doc_id, lesson_no)
        if key not in self._bm25:
            ids = [cid for cid in self.by_doc[doc_id]
                   if lesson_no is None or self.chunks[cid]["lesson_no"] == lesson_no]
            corpus = [tokenize(self.chunks[cid]["text"]) for cid in ids]
            self._bm25[key] = (BM25Okapi(corpus), ids)
        return self._bm25[key]

    def _vec_rank(self, question: str, scope_ids: set[str]) -> list[tuple[str, float]]:
        """向量召回：全库 KNN 后按 scope 过滤取前 K_VEC（库小，过滤代价可忽略）。"""
        qv = embed_texts(self.model, [question])[0]
        rows = self.conn.execute(
            """SELECT chunk_id, distance FROM chunk_vectors
               WHERE embedding MATCH ? ORDER BY distance LIMIT ?""",
            (json.dumps(qv), len(self.chunks))).fetchall()
        out = []
        for cid, dist in rows:
            if cid in scope_ids:
                out.append((cid, 1.0 - dist))  # 归一化向量：相似度 = 1 - L2 距离（近似余弦）
                if len(out) == K_VEC:
                    break
        return out

    def search(self, question: str, scope_doc_id: str | None = None,
               top: int = TOP_N) -> dict:
        doc = scope_doc_id or self.active_doc
        lesson = parse_lesson_no(question)  # 查询理解：课号识别
        query_text = expand_query(question)  # 查询理解：同义词扩展
        scope_ids = set(self.by_doc[doc])
        if lesson is not None:
            # 召回阶段即按课号过滤（非事后过滤），BM25 语料同步收缩
            scope_ids = {cid for cid in scope_ids
                         if self.chunks[cid]["lesson_no"] == lesson}
        if not scope_ids:
            # 课号不存在等空 scope：无召回，交由拒答门处理
            return {"question": question, "scope_doc_id": doc,
                    "lesson_filter": lesson, "top_chunks": [], "top1_rrf": 0.0}

        vec_rank = self._vec_rank(query_text, scope_ids)
        bm25, ids = self._bm25_for(doc, lesson)
        scores = bm25.get_scores(tokenize(query_text))
        bm25_rank = sorted(zip(ids, scores), key=lambda x: -x[1])[:K_BM25]

        # RRF 融合
        rrf: dict[str, float] = defaultdict(float)
        detail: dict[str, dict] = {}
        for rank, (cid, s) in enumerate(vec_rank, start=1):
            rrf[cid] += 1.0 / (RRF_K + rank)
            detail.setdefault(cid, {})["vec_score"] = round(s, 4)
            detail[cid]["vec_rank"] = rank
        for rank, (cid, s) in enumerate(bm25_rank, start=1):
            rrf[cid] += 1.0 / (RRF_K + rank)
            detail.setdefault(cid, {})["bm25_score"] = round(float(s), 4)
            detail[cid]["bm25_rank"] = rank

        fused = sorted(rrf.items(), key=lambda x: -x[1])[:top]
        top_chunks = []
        for cid, score in fused:
            c = self.chunks[cid]
            top_chunks.append({
                "chunk_id": cid, "lesson_no": c["lesson_no"],
                "chapter_path": c["chapter_path"], "content_type": c["content_type"],
                "page_start": c["page_start"], "page_end": c["page_end"],
                "rrf": round(score, 6),
                "vec_score": detail[cid].get("vec_score"),
                "vec_rank": detail[cid].get("vec_rank"),
                "bm25_score": detail[cid].get("bm25_score"),
                "bm25_rank": detail[cid].get("bm25_rank"),
                "text": c["text"],
            })
        return {"question": question, "scope_doc_id": doc, "lesson_filter": lesson,
                "top_chunks": top_chunks,
                "top1_rrf": top_chunks[0]["rrf"] if top_chunks else 0.0}


def cite(c: dict) -> str:
    """出处拼装（§4.5）：课号 + 节标题 + 物理页码区间；节标题为空时省略该段。"""
    parts = [f"第{c['lesson_no']}课"]
    if " > " in c["chapter_path"]:
        sec = c["chapter_path"].split(" > ", 1)[1].strip()
        if sec:
            parts.append(sec)
    parts.append(f"P{c['page_start']}-{c['page_end']}")
    return " · ".join(parts)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--doc", default=None, help="scope_doc_id，默认 active 版本")
    args = ap.parse_args()
    conn = init_db()
    r = Retriever(conn)
    res = r.search(args.question, scope_doc_id=args.doc)
    print(f"Q: {args.question}  (scope={res['scope_doc_id']}, "
          f"lesson_filter={res['lesson_filter']}, top1_rrf={res['top1_rrf']})")
    for i, c in enumerate(res["top_chunks"], 1):
        print(f"  #{i} {c['chunk_id']} rrf={c['rrf']:.4f} "
              f"vec={c['vec_score']}(#{c['vec_rank']}) bm25={c['bm25_score']}(#{c['bm25_rank']})")
        print(f"     {cite(c)} | {c['content_type']} | {c['text'][:60]}…")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
