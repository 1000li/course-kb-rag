# course-kb-rag 公网部署镜像（Render 免费档）
FROM python:3.13-slim

ENV PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖层（pyproject.toml 固定全部版本）
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# 构建期预热 embedding 模型（~100MB）：触发 fastembed 下载并缓存进镜像层，
# 避免容器冷启动时下载超时（Render 健康检查有时间窗）
RUN python -c "from fastembed import TextEmbedding; \
    TextEmbedding(model_name='BAAI/bge-small-zh-v1.5'); \
    print('embedding model cached')"

# 应用代码 + 事实库 + 前端
COPY web ./web
COPY eval ./eval
COPY db/course_kb.db ./db/course_kb.db
RUN mkdir -p data/logs

EXPOSE 8000
# Render 注入 $PORT；本地 docker run 默认 8000
CMD python -m src.serve
