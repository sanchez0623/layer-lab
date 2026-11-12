"""OSS 上传助手（V1 签名）：建 bucket → 传对象 → 生成限时下载 URL。

用法：
  python scripts/oss_upload.py <本地文件> [对象名]
输出最后一行是预签名 URL（30 分钟有效），供服务器 curl 下载。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import sys
import time
import urllib.parse

import httpx

REGION = "cn-shenzhen"
BUCKET = "limit-lens-deploy-1622731870935409"
ENDPOINT = f"https://oss-{REGION}.aliyuncs.com"


def sign(secret: str, sts: str) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), sts.encode(), hashlib.sha1).digest()).decode()


def main() -> int:
    ak, sk = load_ak_wrap()
    local = sys.argv[1]
    key = sys.argv[2] if len(sys.argv) > 2 else local.replace("\\", "/").split("/")[-1]
    data = open(local, "rb").read()

    # 1) PutBucket（已存在则忽略冲突）
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    sts = f"PUT\n\n\n{date}\n/{BUCKET}/"
    auth = f"OSS {ak}:{sign(sk, sts)}"
    r = httpx.put(f"{ENDPOINT}/{BUCKET}/", headers={"Date": date, "Authorization": auth},
                  timeout=30)
    if r.status_code in (200, 409):
        print(f"bucket {BUCKET}:", "created" if r.status_code == 200 else "already exists")
    else:
        print("PutBucket 失败:", r.status_code, r.text[:200])
        return 1

    # 2) PutObject
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    ctype = "application/gzip"
    sts = f"PUT\n\n{ctype}\n{date}\n/{BUCKET}/{key}"
    auth = f"OSS {ak}:{sign(sk, sts)}"
    r = httpx.put(f"{ENDPOINT}/{BUCKET}/{urllib.parse.quote(key)}",
                  headers={"Date": date, "Authorization": auth, "Content-Type": ctype},
                  content=data, timeout=300)
    if r.status_code != 200:
        print("PutObject 失败:", r.status_code, r.text[:200])
        return 1
    print(f"已上传 {key}: {len(data)} bytes")

    # 3) 预签名 GET URL（30 分钟）
    expires = int(time.time()) + 1800
    sts = f"GET\n\n\n{expires}\n/{BUCKET}/{key}"
    sig = urllib.parse.quote(sign(sk, sts), safe="-_.~")
    url = (f"https://{BUCKET}.oss-{REGION}.aliyuncs.com/"
           f"{urllib.parse.quote(key)}?OSSAccessKeyId={ak}&Expires={expires}&Signature={sig}")
    print("PRESIGNED_URL")
    print(url)
    return 0


def load_ak_wrap():
    sys.path.insert(0, __file__.rsplit("\\", 1)[0])
    from ecs_run import load_ak
    return load_ak()


if __name__ == "__main__":
    raise SystemExit(main())
