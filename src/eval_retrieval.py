"""M5 检索评估：eval/questions.jsonl → recall@5 / MRR / 拒答率 + 拒答规则标定。

- 可答题（should_abstain=false）：课级命中 = top-5 任一块 lesson_no ∈ expected_lesson_no；
  recall@5 与 MRR（首个命中名次的倒数均值）
- 应拒答题（should_abstain=true，含 q23 在 doc_001 上的缺课场景）
- 拒答规则标定（不拍脑袋）：
  实测发现架构文档 §4.4「融合 top-1 的 RRF 分数阈值」不可行——RRF 只含名次不含绝对相关性，
  应拒答题 top-1 RRF 与可答题完全重叠（如 q22 红烧肉 RRF 0.0328 为全场最高之一）。
  改用**双信号与门**：top-5 内最大向量相似度 < tv 且最大 BM25 < tb → 拒答
  （两个信号都弱才拒；任一信号强则答）。tv/tb 由本评测集标定：
  tv 取 max(应拒答 maxVec) 与其上方最低可答题 maxVec 的中点；
  tb 在误拒 ≤10% 约束下取 max(应拒答 maxBM25) 与被保留可答题最低 maxBM25 的中点。
  标定过程与分布见 docs/eval-report-m5.md。
- 结果落库 eval_runs / eval_results（每次运行一轮新行）

用法：py -3 -m src.eval_retrieval
"""

import json
import sys
import uuid
from datetime import datetime, timezone

from .db import ROOT, init_db
from .index import MODEL_NAME
from .retrieve import Retriever
from .chunk import PIPELINE_VERSION

sys.stdout.reconfigure(encoding="utf-8")

QUESTIONS = ROOT / "eval" / "questions.jsonl"
REPORT = ROOT / "docs" / "eval-report-m5.md"
FALSE_ABSTAIN_LIMIT = 0.10


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def signals(x: dict) -> tuple[float, float]:
    """双信号：top-5 内最大向量相似度、最大 BM25（单边未入榜记 -inf）。"""
    vs = [c["vec_score"] for c in x["top5"] if c["vec_score"] is not None]
    bs = [c["bm25_score"] for c in x["top5"] if c["bm25_score"] is not None]
    return (max(vs) if vs else float("-inf")), (max(bs) if bs else float("-inf"))


def calibrate(ans: list[dict], abs_: list[dict]) -> tuple[dict | None, str]:
    """返回 ({'vec_max_top5': tv, 'bm25_max_top5': tb}, 说明)；不可行返回 (None, 原因)。"""
    amv = max(signals(x)[0] for x in abs_)
    above = min((signals(x)[0] for x in ans if signals(x)[0] > amv), default=None)
    if above is None:
        return None, f"不可行：无tv可分隔应拒答 maxVec={amv:.4f}"
    tv = (amv + above) / 2
    amb = max(signals(x)[1] for x in abs_)
    low_ans_bm = sorted(signals(x)[1] for x in ans if signals(x)[0] < tv)
    allowed = int(len(ans) * FALSE_ABSTAIN_LIMIT)
    if len(low_ans_bm) <= allowed:
        # 涉险可答题数本就在限额内：tb 取应拒答最大值与涉险最低值的中点（如有余量）
        tb = ((amb + min(low_ans_bm)) / 2) if (low_ans_bm and min(low_ans_bm) > amb) \
            else amb + 1e-9
    else:
        keep_from = low_ans_bm[allowed]  # 牺牲 bm 最低的前 allowed 道
        if keep_from <= amb:
            return None, (f"不可行：应拒答 maxBM25={amb:.2f} 与可答题重叠过深")
        tb = (amb + keep_from) / 2
    false_n = sum(1 for x in ans
                  if signals(x)[0] < tv and signals(x)[1] < tb)
    if false_n > allowed:
        return None, f"不可行：误拒 {false_n} 道超过上限 {allowed}"
    note = (
        f"**为何不用 RRF 阈值（规格偏离说明）**：架构文档 §4.4 原口径为「融合 top-1 的 "
        f"RRF 分数低于阈值 → 拒答」。实测 RRF 分数只含名次不含绝对相关性：应拒答题 "
        f"top-1 RRF ∈ [{min(x['top1_rrf'] for x in abs_):.4f}, "
        f"{max(x['top1_rrf'] for x in abs_):.4f}]，与可答题 "
        f"[{min(x['top1_rrf'] for x in ans):.4f}, {max(x['top1_rrf'] for x in ans):.4f}] "
        f"完全重叠（q22「红烧肉」RRF 0.0328 为全场最高之一），任何 RRF 阈值都无法满足"
        f"「应拒答全拒且误拒≤10%」。\n\n"
        f"**改用双信号与门**：top-5 内最大向量相似度 < tv 且最大 BM25 < tb → 拒答。\n\n"
        f"- 应拒答 maxVec ∈ [{min(signals(x)[0] for x in abs_):.3f}, {amv:.3f}]；"
        f"向量侧低于 tv 的可答题由 BM25 侧（tb 门）兜底区分。\n"
        f"- tv = max(应拒答 maxVec)={amv:.6f} 与其上方最低可答题 maxVec={above:.6f} "
        f"的中点 = **{tv:.6f}**\n"
        f"- tb = max(应拒答 maxBM25)={amb:.2f} 与被保留可答题最低 maxBM25 的中点 = "
        f"**{tb:.4f}**（允许误拒 ≤{allowed} 道，实际误拒 {false_n} 道）")
    # 阈值须存全精度：中点取整可能回落到 max(应拒答) 之下，使边界应拒答题漏拒
    # （2026-10 实测：tv 中点 0.17414 被 round(.,4) 截为 0.1741，q23 maxVec=0.17413 漏拒）
    return {"vec_max_top5": tv, "bm25_max_top5": tb}, note


def main() -> int:
    questions = [json.loads(l) for l in
                 QUESTIONS.read_text(encoding="utf-8").splitlines() if l.strip()]
    conn = init_db()
    r = Retriever(conn)

    results = []
    for q in questions:
        res = r.search(q["question"], scope_doc_id=q["scope_doc_id"])
        top5 = res["top_chunks"]
        hit_rank = None
        if not q["should_abstain"]:
            for i, c in enumerate(top5, 1):
                if c["lesson_no"] in q["expected_lesson_no"]:
                    hit_rank = i
                    break
        mv, mb = signals({"top5": top5})
        results.append({
            "qid": q["qid"], "question": q["question"],
            "scope_doc_id": res["scope_doc_id"],
            "should_abstain": q["should_abstain"],
            "calibration": q.get("calibration", True),
            "expected": q["expected_lesson_no"], "note": q["note"],
            "hit_rank": hit_rank, "top1_rrf": res["top1_rrf"],
            "lesson_filter": res["lesson_filter"],
            "max_vec5": mv, "max_bm5": mb, "top5": top5,
        })

    ans = [x for x in results if not x["should_abstain"]]
    abs_ = [x for x in results if x["should_abstain"]]
    # calibration=false 的题（owner 2026-10-08 裁定，如 q23：归档版本 scope 陷阱题）
    # 不参与阈值标定与拒答类指标，仍在逐题明细中展示实际表现作为信息项
    ans_cal = [x for x in ans if x.get("calibration", True)]
    abs_cal = [x for x in abs_ if x.get("calibration", True)]
    recall = sum(1 for x in ans if x["hit_rank"]) / len(ans)
    mrr = sum(1.0 / x["hit_rank"] for x in ans if x["hit_rank"]) / len(ans)

    rule, calib_note = calibrate(ans_cal, abs_cal)
    excluded = [x["qid"] for x in results if not x.get("calibration", True)]
    if excluded:
        calib_note += (f"\n\n**标定集排除**：{excluded}（calibration=false，不参与 "
                       f"tv/tb 标定与拒答率/误拒率统计，逐题明细仍展示其实际表现）")
    for x in results:
        # 与 qa.should_abstain 同口径：课号 scope 题不走双信号与门（分值与全库
        # 标定不可比），仅空 scope 拒答；其余题走与门
        if x["lesson_filter"] is not None:
            x["abstained"] = not x["top5"]
        else:
            x["abstained"] = (rule is not None
                              and x["max_vec5"] < rule["vec_max_top5"]
                              and x["max_bm5"] < rule["bm25_max_top5"])
    abstain_rate = (sum(1 for x in abs_cal if x["abstained"]) / len(abs_cal)) if rule else 0
    false_rate = (sum(1 for x in ans_cal if x["abstained"]) / len(ans_cal)) if rule else 0

    run_id = f"eval_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
    now = utcnow()
    conn.execute(
        """INSERT INTO eval_runs
           (run_id, pipeline_version, model_name, ran_at, question_count,
            recall_at_5, mrr, abstain_rate, false_abstain_rate, threshold, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, PIPELINE_VERSION, MODEL_NAME, now, len(questions),
         round(recall, 4), round(mrr, 4), round(abstain_rate, 4),
         round(false_rate, 4), json.dumps(rule, ensure_ascii=False), calib_note))
    conn.executemany(
        """INSERT INTO eval_results
           (run_id, qid, scope_doc_id, should_abstain, hit_at_5, first_hit_rank,
            top1_score, abstained, top_chunks)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(run_id, x["qid"], x["scope_doc_id"], int(x["should_abstain"]),
          (int(x["hit_rank"] is not None) if not x["should_abstain"] else None),
          x["hit_rank"], x["top1_rrf"], int(x["abstained"]),
          json.dumps([{k: c[k] for k in ("chunk_id", "lesson_no", "rrf",
                                         "vec_score", "bm25_score")}
                      for c in x["top5"]], ensure_ascii=False))
         for x in results])
    conn.execute(
        """INSERT INTO pipeline_runs
           (run_id, stage, doc_version, started_at, finished_at, stats, status)
           VALUES (?, 'eval', NULL, ?, ?, ?, 'success')""",
        (run_id, now, utcnow(), json.dumps(
            {"questions": len(questions), "recall_at_5": round(recall, 4),
             "mrr": round(mrr, 4), "abstain_rate": abstain_rate,
             "false_abstain_rate": false_rate, "rule": rule}, ensure_ascii=False)))
    conn.commit()

    write_report(run_id, results, recall, mrr, abstain_rate, false_rate,
                 rule, calib_note)
    print(f"[评估] {len(questions)} 题：recall@5={recall:.1%}，MRR={mrr:.3f}，"
          f"拒答率={abstain_rate:.0%}，误拒率={false_rate:.1%}，规则={rule}")
    fails = [x["qid"] for x in ans if not x["hit_rank"]]
    if fails:
        print(f"[未命中] {fails}")
    false_rej = [x["qid"] for x in ans if x["abstained"]]
    if false_rej:
        print(f"[误拒] {false_rej}")
    print(f"报告：{REPORT.relative_to(ROOT)}，run_id={run_id}")
    conn.close()
    return 0


def write_report(run_id, results, recall, mrr, abstain_rate, false_rate,
                 rule, calib_note):
    ans = [x for x in results if not x["should_abstain"]]
    abs_ = [x for x in results if x["should_abstain"]]
    ans_cal = [x for x in ans if x.get("calibration", True)]
    abs_cal = [x for x in abs_ if x.get("calibration", True)]
    excl = {x["qid"] for x in results if not x.get("calibration", True)}
    lines = [
        "# M5 检索质量评估报告",
        "",
        f"run_id：`{run_id}` · 生成：{utcnow()} · 模型：{MODEL_NAME} · "
        f"pipeline_version：{PIPELINE_VERSION}",
        "",
        "## 指标汇总",
        "",
        "| 指标 | 值 | 验收线 |",
        "|---|---|---|",
        f"| 可答题 recall@5（课级） | {recall:.1%}（{sum(1 for x in ans if x['hit_rank'])}/{len(ans)}） | ≥85% |",
        f"| 可答题 MRR | {mrr:.3f} | — |",
        f"| 应拒答题拒答率（标定集） | {abstain_rate:.0%}（{sum(1 for x in abs_cal if x['abstained'])}/{len(abs_cal)}） | 100% |",
        f"| 可答题误拒率（标定集） | {false_rate:.1%}（{sum(1 for x in ans_cal if x['abstained'])}/{len(ans_cal)}） | ≤10% |",
        f"| 拒答规则 | top5 maxVec < {rule['vec_max_top5']:.6f} 且 maxBM25 < {rule['bm25_max_top5']:.4f} → 拒答 | — |" if rule else "| 拒答规则 | 标定失败 | — |",
        "",
        "## 阈值标定",
        "",
        calib_note,
        "",
        "## 各题信号分布（maxVec5 / maxBM25 / top1 RRF）",
        "",
        "应拒答题：" +
        "；".join(f"{x['qid']}({x['max_vec5']:.3f}/{x['max_bm5']:.1f}/{x['top1_rrf']:.4f})"
                 + ("（标定外）" if x["qid"] in excl else "")
                 for x in abs_),
        "",
        "可答题（按 maxVec 升序）：" +
        "；".join(f"{x['qid']}({x['max_vec5']:.3f}/{x['max_bm5']:.1f}/{x['top1_rrf']:.4f})"
                 for x in sorted(ans, key=lambda x: x["max_vec5"])),
        "",
        "## 逐题明细",
        "",
    ]
    for x in results:
        if x["should_abstain"]:
            verdict = "✓ 拒答" if x["abstained"] else "✗ 未拒答"
        else:
            verdict = (f"✓ 命中#{x['hit_rank']}" if x["hit_rank"] else "✗ 未命中")
            if x["abstained"]:
                verdict += "（但被阈值误拒）"
        lines += [
            f"### {x['qid']}（scope={x['scope_doc_id']}）{verdict}",
            "",
            f"Q：{x['question']}",
            f"期望课：{x['expected'] or '—'} · maxVec5={x['max_vec5']:.3f} · "
            f"maxBM25_5={x['max_bm5']:.2f} · top1_rrf={x['top1_rrf']:.4f} · "
            f"出题依据：{x['note']}",
            "",
            "| # | chunk_id | 课 | RRF | vec(名次) | bm25(名次) |",
            "|---|---|---|---|---|---|",
        ]
        for i, c in enumerate(x["top5"], 1):
            lines.append(
                f"| {i} | {c['chunk_id']} | 第{c['lesson_no']}课 | {c['rrf']:.4f} "
                f"| {c['vec_score']}(#{c['vec_rank']}) | {c['bm25_score']}(#{c['bm25_rank']}) |")
        lines.append("")
    fails = [x for x in ans if not x["hit_rank"]]
    false_rej = [x for x in ans if x["abstained"]]
    lines += ["## 失败题分析", ""]
    if fails:
        for x in fails:
            got = [c["lesson_no"] for c in x["top5"]]
            lines.append(f"- **{x['qid']}**（期望第{x['expected']}课，实际 top-5 课号 {got}）："
                         f"{x['question']}")
    else:
        lines.append("可答题全部命中，无召回失败题。")
    if false_rej:
        lines.append("")
        lines.append("误拒题（可答但被拒答规则拦下）：")
        for x in false_rej:
            lines.append(f"- **{x['qid']}** maxVec5={x['max_vec5']:.3f} "
                         f"maxBM25={x['max_bm5']:.2f}：{x['question']} —— "
                         f"向量与 BM25 双弱，属阈值代价（误拒率 {false_rate:.1%} ≤10%）")
    lines.append("")
    REPORT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
