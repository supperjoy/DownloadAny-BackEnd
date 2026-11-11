# 注意:这里刻意不写 `# syntax=docker/dockerfile:1`。
# 那行会让 BuildKit 去 docker.io 拉外部 frontend 镜像,而国内服务器普遍连不上 Docker Hub
# (DNS 常被污染到假 IP,表现为 dial tcp ...:443 i/o timeout),构建直接失败。
# Docker 29 内嵌的 dockerfile frontend 已支持下面用到的 RUN --mount=type=cache,无需外部镜像。
# 若将来需要更新的 BuildKit 语法再恢复这行,并给服务器配好 registry mirror。

# 项目用 uv 管理依赖(requires-python >= 3.13),直接用 uv 官方镜像,
# 构建 + 运行单阶段即可,简单可靠。
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# UV_LINK_MODE=copy:跨文件系统的缓存层用复制而不是硬链接,避免报错
# UV_PYTHON_DOWNLOADS=never:镜像里已带 Python,不让 uv 再下一份
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

# 1) 先只拷贝依赖清单并安装第三方依赖 —— 只要 pyproject/uv.lock 不变,
#    这一层会命中缓存,改代码不会触发重新装依赖
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 2) 再拷贝源码并安装项目本身(editable,site-packages 里指回 /app/src)
COPY main.py ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# 非 root 用户运行
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

# 必须监听 0.0.0.0,否则宿主机端口映射访问不到。
# 生产可按 CPU 加 --workers N(注意多 worker 会放大启动时 create_all 的并发)。
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
