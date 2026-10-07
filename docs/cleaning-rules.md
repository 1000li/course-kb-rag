# 清洗规则清单

<!-- 本文件由 src/clean.py 自动生成，请勿手改；重跑 clean 即更新命中统计 -->

生成时间：2026-10-07T06:40:09+00:00（三版本合计命中数来自 cleaning_log 全表聚合）

| rule_id | 说明 | 命中数 |
|---|---|---|
| `page_number_line` | 行首孤立页码行：整行仅 1-3 位数字且课内页码序列递增，判定为页脚页码，整行删除 | 210 |
| `image_caption_dup` | 连续重复的图片占位/图注行：相邻两行strip后相同且长度 ≤40，保留首行删除后续重复行 | 0 |
| `whitespace` | 空白规整：行首尾空白剔除；CJK（含CJK标点）之间的半角空格删除；连续空行压缩为一行 | 1534 |
| `pua_bullet` | PUA 私用区字符（Wingdings/Symbol 字体项目符号，如 U+F06C/U+F0B7）替换为标准项目符号「•」 | 39 |

聚合查询示例：`SELECT rule_id, action, COUNT(*) FROM cleaning_log GROUP BY rule_id, action`
