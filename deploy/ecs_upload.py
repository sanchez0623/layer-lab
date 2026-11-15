"""ECS 可靠文件上传：分块写入独立 part 文件 + 逐块 sha256 校验 + 失败重试。

用法: python deploy/ecs_upload.py <本地文件> <远端路径>

背景：ecs_run.py upload 的「printf 追加」式上传在云助手偶发静默丢块时
无法察觉（曾把 691KB 部署包传成 419KB 且 sha256 不一致）。本脚本：
  1. 每块写入独立文件 /root/.ll_parts/<index>，立即远端 sha256sum 校验；
  2. 校验不过自动重传该块（最多 5 次）；
  3. 全部完成后 cat 合并并校验整体 sha256。
"""

from __future__ import annotations

import base64
import hashlib
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0].rsplit("\\", 1)[0])
from ecs_run import load_ak, run_and_wait  # noqa: E402

CHUNK = 11000          # base64 字符数/块（11000 → 命令约 15KB；实测 22000 会超云助手上限）
MAX_RETRY = 5


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    local, remote = sys.argv[1], sys.argv[2]
    ak_id, secret = load_ak()
    data = open(local, "rb").read()
    sha = hashlib.sha256(data).hexdigest()
    chunks = base64.b64encode(data)
    n = (len(chunks) + CHUNK - 1) // CHUNK
    print(f"上传 {local} → {remote}: {len(data)} bytes, {n} 块, sha256={sha[:16]}...")

    run_and_wait("rm -rf /root/.ll_parts && mkdir -p /root/.ll_parts",
                 ak_id, secret, name="llp-init")

    failed: list[int] = []
    for i in range(n):
        part = chunks[i * CHUNK:(i + 1) * CHUNK].decode()
        part_sha = hashlib.sha256(part.encode()).hexdigest()
        ok = False
        for attempt in range(1, MAX_RETRY + 1):
            r = run_and_wait(
                f"printf '%s' '{part}' > /root/.ll_parts/{i:04d} && "
                f"echo S=$(sha256sum /root/.ll_parts/{i:04d} | cut -d' ' -f1)",
                ak_id, secret, name=f"llp-{i+1}/{n}")
            if r["exit_code"] == 0 and part_sha in r["output"]:
                ok = True
                break
            print(f"  块 {i+1}/{n} 第{attempt}次校验不符（exit={r['exit_code']}），重试")
        if not ok:
            failed.append(i + 1)
    if failed:
        print(f"上传失败：{len(failed)} 块重试后仍不符: {failed}")
        return 1

    r = run_and_wait(
        f"cat /root/.ll_parts/* | base64 -d > '{remote}' && "
        f"echo ACTUAL=$(sha256sum '{remote}' | cut -d' ' -f1) EXPECT={sha} "
        f"SIZE=$(stat -c %s '{remote}') && sha256sum /root/.ll_parts/*",
        ak_id, secret, name="llp-done")
    print(r["output"].strip())
    # 本地比对每个 part 的 sha，定位错块（保留远端 parts 便于取证）
    import re
    remote_shas = {}
    for line in r["output"].splitlines():
        m = re.match(r"([0-9a-f]{64})\s+/root/\.ll_parts/(\d+)", line)
        if m:
            remote_shas[int(m.group(2))] = m.group(1)
    bad = []
    for i in range(n):
        part = chunks[i * CHUNK:(i + 1) * CHUNK].decode()
        if remote_shas.get(i) != hashlib.sha256(part.encode()).hexdigest():
            bad.append(i)
    print(f"比对结果: {len(remote_shas)}/{n} 个 part 有哈希; 不符块: {bad[:20]}")
    run_and_wait("rm -rf /root/.ll_parts", ak_id, secret, name="llp-clean")
    return 0 if sha in r["output"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
