"""M7 一键管线（架构文档 §11）：ingest → parse → clean → chunk → annotate → export → qc → index → eval。

- 全链路幂等：ingest 按 sha256 跳过，其余阶段同版本重跑覆盖；全链路重跑结果一致
- eval 阶段只做检索评估，不调用 LLM，all 模式无需 API key
- 各阶段可单独跑：py -3 -m src.pipeline ingest parse ...

用法：
  py -3 -m src.pipeline all
  py -3 -m src.pipeline chunk annotate
"""

import importlib
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

STAGES = [
    ("ingest", "src.ingest"),
    ("parse", "src.parse"),
    ("clean", "src.clean"),
    ("chunk", "src.chunk"),
    ("annotate", "src.annotate"),
    ("export", "src.export"),
    ("qc", "src.qc"),
    ("index", "src.index"),
    ("eval", "src.eval_retrieval"),
]


def main() -> int:
    args = sys.argv[1:] or ["all"]
    selected = [s for s, _ in STAGES] if args == ["all"] else args
    unknown = [s for s in selected if s not in dict(STAGES)]
    if unknown:
        print(f"未知阶段：{unknown}；可选：{[s for s, _ in STAGES]} 或 all")
        return 2

    for name in selected:
        module = dict(STAGES)[name]
        print(f"\n===== 阶段 {name} =====")
        t0 = time.monotonic()
        rc = importlib.import_module(module).main()
        if rc:
            print(f"阶段 {name} 失败（rc={rc}），管线中止")
            return rc
        print(f"----- {name} 完成（{time.monotonic() - t0:.1f}s）-----")
    print("\n管线全部完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
