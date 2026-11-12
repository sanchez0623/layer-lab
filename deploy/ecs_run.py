"""阿里云 ECS 云助手 OpenAPI 客户端（RPC 风格，HMAC-SHA1 签名）。

复刻 dividend-alpha-v2 已验证的链路：本地脚本 → POST ecs.<region>.aliyuncs.com
Action=RunCommand → 轮询 DescribeInvocationResults（base64 回传输出）。
AK 从 workbench 配置读取，不在日志中回显 secret。

用法：
  python scripts/ecs_run.py check                 # DescribeInstances 实例信息
  python scripts/ecs_run.py run "uname -a"        # 执行命令并等待输出
  python scripts/ecs_run.py upload <本地> <远端>   # 分块 base64 上传文件
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
import urllib.parse
import uuid

import httpx

REGION = "cn-shenzhen"
INSTANCE = "i-wz95p1n73tl4oraq1xmi"
ECS_ENDPOINT = f"https://ecs.{REGION}.aliyuncs.com"
AK_CONFIG = r"C:\Users\zsq23\.workbench\config.json"


def load_ak() -> tuple[str, str]:
    with open(AK_CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    p = cfg["profiles"][cfg.get("current", "default")]
    return p["access_key_id"], p["access_key_secret"]


def pct(s: str) -> str:
    """RFC3986 百分号编码（阿里云签名规范）。"""
    return urllib.parse.quote(s, safe="-_.~")


def sign_params(params: dict, secret: str) -> dict:
    """为 RPC 请求追加签名参数（HMAC-SHA1 & '/'）。"""
    items = sorted((k, v) for k, v in params.items())
    query = "&".join(f"{pct(k)}={pct(v)}" for k, v in items)
    sts = "POST&" + pct("/") + "&" + pct(query)
    sig = base64.b64encode(
        hmac.new((secret + "&").encode(), sts.encode(), hashlib.sha1).digest()
    ).decode()
    return {**params, "Signature": sig}


def call(endpoint: str, version: str, action: str, params: dict,
         ak_id: str, secret: str, timeout: float = 30.0) -> dict:
    base = {
        "Action": action, "Version": version, "Format": "JSON",
        "AccessKeyId": ak_id, "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0", "SignatureNonce": str(uuid.uuid4()),
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    signed = sign_params({**base, **params}, secret)
    r = httpx.post(endpoint, data=signed, timeout=timeout,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    d = r.json()
    if r.status_code != 200 or "Error" in d:
        err = d.get("Error", d)
        raise RuntimeError(f"{action} 失败 [{r.status_code}] {err.get('Code')}: "
                           f"{err.get('Message')}")
    return d


def run_command(cmd: str, ak_id: str, secret: str, name: str = "ll-cmd",
                timeout_cmd: int = 600) -> str:
    """下发 shell 命令，返回 InvokeId。内容 base64（云助手限 64KB 内）。"""
    b64 = base64.b64encode(cmd.encode()).decode()
    if len(b64) > 60000:
        raise ValueError(f"命令内容过大: {len(b64)} bytes（需分块）")
    d = call(ECS_ENDPOINT, "2014-05-26", "RunCommand",
             {"RegionId": REGION, "Type": "RunShellScript",
              "CommandContent": b64, "ContentEncoding": "Base64",
              "InstanceId.1": INSTANCE, "Name": name,
              "Timeout": str(timeout_cmd)},
             ak_id, secret)
    return d["InvokeId"]


def wait_result(invoke_id: str, ak_id: str, secret: str,
                max_wait: int = 900) -> dict:
    """轮询 DescribeInvocationResults 直到完成，返回 {status, exit_code, output}。"""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        d = call(ECS_ENDPOINT, "2014-05-26", "DescribeInvocationResults",
                 {"RegionId": REGION, "InvokeId": invoke_id}, ak_id, secret)
        res = (d.get("Invocation", {}).get("InvocationResults")
               or {}).get("InvocationResult", [])
        if res:
            r = res[0]
            terminal = (r.get("ExitCode") is not None
                        or r.get("FinishedTime")
                        or r.get("InvocationStatus") in ("Finished", "Success", "Failed"))
            if terminal:
                out = base64.b64decode(r.get("Output", "") or "").decode(
                    "utf-8", errors="replace")
                return {"status": r.get("InvocationStatus", "Finished"),
                        "exit_code": r.get("ExitCode"),
                        "output": out}
        time.sleep(2)
    return {"status": "Timeout", "exit_code": -1, "output": ""}


def run_and_wait(cmd: str, ak_id: str, secret: str, name: str = "ll-cmd") -> dict:
    iid = run_command(cmd, ak_id, secret, name=name)
    print(f"[ecs-run] InvokeId: {iid}", file=sys.stderr)
    return wait_result(iid, ak_id, secret)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ak_id, secret = load_ak()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"

    if cmd == "check":
        d = call(ECS_ENDPOINT, "2014-05-26", "DescribeInstances",
                 {"RegionId": REGION,
                  "InstanceIds": json.dumps([INSTANCE])}, ak_id, secret)
        inst = d["Instances"]["Instance"][0]
        print("实例:", inst["InstanceId"], inst["InstanceName"])
        print("状态:", inst["Status"], "| OS:", inst.get("OSName"),
              "| IP:", inst.get("PublicIpAddress"))
        return 0

    if cmd == "run":
        r = run_and_wait(sys.argv[2], ak_id, secret)
        print("status:", r["status"], "exit:", r["exit_code"])
        print(r["output"])
        return 0 if r["exit_code"] == 0 else 1

    if cmd == "upload":
        import os
        local, remote = sys.argv[2], sys.argv[3]
        data = open(local, "rb").read()
        sha = hashlib.sha256(data).hexdigest()
        chunks = base64.b64encode(data)
        print(f"上传 {local} → {remote}: {len(data)} bytes, "
              f"sha256={sha[:16]}...")
        r = run_and_wait(
            "rm -f /root/.ll_upload.b64", ak_id, secret, name="ll-up-init")
        assert r["exit_code"] == 0, r
        CH = 22000
        n = (len(chunks) + CH - 1) // CH
        print(f"  分块: {n} 块 × {CH} chars")
        i = 0
        while i < n:
            part = chunks[i * CH:(i + 1) * CH].decode()
            try:
                r = run_and_wait(
                    f"printf '%s' '{part}' >> /root/.ll_upload.b64",
                    ak_id, secret, name=f"ll-up-{i+1}/{n}")
            except RuntimeError as e:
                if "ExceedLimit" in str(e) and CH > 5000:
                    CH //= 2
                    n = (len(chunks) + CH - 1) // CH
                    print(f"  超限，降为 {CH} 重来")
                    run_and_wait("rm -f /root/.ll_upload.b64",
                                 ak_id, secret, name="ll-up-reinit")
                    i = 0
                    continue
                raise
            if r["exit_code"] != 0:
                print(f"块 {i+1}/{n} 失败:", r["output"][:200])
                return 1
            i += 1
            if i % 10 == 0 or i == n:
                print(f"  {i}/{n} 块")
        r = run_and_wait(
            f"base64 -d /root/.ll_upload.b64 > '{remote}' && "
            f"echo ACTUAL=$(sha256sum '{remote}' | cut -d' ' -f1) "
            f"EXPECT={sha}",
            ak_id, secret, name="ll-up-done")
        print(r["output"].strip())
        return 0 if sha in r["output"] else 1

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
