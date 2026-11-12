"""最小部署包（服务器 docker build 所需源码），保留目录结构。
用法: python deploy/pack_deploy.py   → 产出 layer-lab-deploy.tar.gz
剔除 node_modules / dist / .git / .env（密钥永不进包）/ 运行数据与缓存。
服务器上用 docker build 现场构建镜像，因此要带全源码（含 frontend/ 与 backend/）。
"""
from __future__ import annotations

import os
import tarfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "layer-lab-deploy.tar.gz")

EXCLUDE_DIRS = {".git", "node_modules", "dist", ".dsh", "__pycache__",
                ".pytest_cache", ".pnpm-store", ".workbuddy", ".sites-runtime"}
# 运行期数据目录（相对项目根）：容器在命名卷里从零开始，不入包
EXCLUDE_REL_DIRS = {os.path.join("backend", "data").replace(os.sep, "/")}
EXCLUDE_FILES = {".env", ".env.local"}
# 只保留运行/构建所需顶层目录，避免把缓存/数据/历史打进包
KEEP_TOP = {"backend", "frontend", "deploy"}
KEEP_FILES = {"Dockerfile", ".dockerignore", "README.md", "requirements.txt"}


def main() -> int:
    files: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT).replace("\\", "/")
        dirnames[:] = [
            d for d in dirnames
            if d not in EXCLUDE_DIRS
            and os.path.join(rel_dir, d).replace("\\", "/") not in EXCLUDE_REL_DIRS
        ]
        for fn in filenames:
            if fn in EXCLUDE_FILES:
                continue
            # 运行期数据库/缓存/上传一律不入包（容器在命名卷里从零开始）
            if fn.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite-journal",
                            ".log", ".csv")):
                continue
            abs_p = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_p, ROOT).replace("\\", "/")
            parts = rel.split("/")
            if parts[0] in KEEP_TOP or (len(parts) == 1 and parts[0] in KEEP_FILES):
                files.append((abs_p, rel))

    with tarfile.open(OUT, "w:gz") as z:
        for abs_p, rel in files:
            z.add(abs_p, arcname=rel)
    print(f"已打包: {OUT}  ({os.path.getsize(OUT) // 1024} KB, {len(files)} 文件)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())