#!/usr/bin/env bash
# Datasette 呈现（M7）：只读打开 db/course_kb.db，端口 8001，预置查询见 datasette-metadata.yaml
# 用法：bash scripts/serve_datasette.sh
set -e
cd "$(dirname "$0")/.."
# Windows 两处坑：
# 1) datasette 用系统 locale（GBK）读 metadata 文件 → PYTHONUTF8=1 强制 UTF-8
# 2) --load-extension 按 "路径:入口名" 解析，C:\... 的盘符冒号会误切 →
#    把 sqlite-vec 的 vec0.dll 复制到仓库相对路径再加载
export PYTHONUTF8=1
VEC_SRC=$(py -3 -c "import sqlite_vec; print(sqlite_vec.loadable_path())")
cp -f "$VEC_SRC.dll" scripts/vec0.dll
exec py -3 -m datasette serve -i db/course_kb.db \
  --metadata scripts/datasette-metadata.yaml \
  --load-extension scripts/vec0.dll \
  -p 8001
