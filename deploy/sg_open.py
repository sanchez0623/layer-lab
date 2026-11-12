"""检查/放行安全组入方向端口（layer-lab 云助手部署用）。

用法：
  python deploy/sg_open.py check              # 列出当前入方向规则
  python deploy/sg_open.py open <端口> [协议]  # 放行 0.0.0.0/0（幂等）
  python deploy/sg_open.py open-all           # 一次放行 3011(应用) + 3444(HTTPS)
"""
from __future__ import annotations

import json
import sys

from ecs_run import ECS_ENDPOINT, INSTANCE, REGION, call, load_ak


def get_sg(ak: str, sk: str) -> str:
    d = call(ECS_ENDPOINT, "2014-05-26", "DescribeInstances",
             {"RegionId": REGION, "InstanceIds": json.dumps([INSTANCE])}, ak, sk)
    return d["Instances"]["Instance"][0]["SecurityGroupIds"]["SecurityGroupId"][0]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ak, sk = load_ak()
    sg = get_sg(ak, sk)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"

    if cmd == "check":
        d = call(ECS_ENDPOINT, "2014-05-26", "DescribeSecurityGroups",
                 {"RegionId": REGION, "SecurityGroupIds": json.dumps([sg])},
                 ak, sk)
        print("安全组:", sg, d["SecurityGroups"]["SecurityGroup"][0]["SecurityGroupName"])
        rules = call(ECS_ENDPOINT, "2014-05-26", "DescribeSecurityGroupAttribute",
                     {"RegionId": REGION, "SecurityGroupId": sg,
                      "Direction": "ingress"}, ak, sk)
        for p in rules["Permissions"]["Permission"]:
            print(f"  {p.get('IpProtocol')}/{p.get('PortRange')}  from "
                  f"{p.get('SourceCidrIp')}  ({p.get('Description', '')})")
        return 0

    if cmd == "open-all":
        for p in ("3011", "3444"):
            import subprocess
            subprocess.run([sys.executable, __file__, "open", p], check=False)
        return 0

    if cmd == "open":
        port = sys.argv[2]
        proto = sys.argv[3] if len(sys.argv) > 3 else "tcp"
        rng = f"{port}/{port}"
        # 幂等：先查是否已放行
        rules = call(ECS_ENDPOINT, "2014-05-26", "DescribeSecurityGroupAttribute",
                     {"RegionId": REGION, "SecurityGroupId": sg,
                      "Direction": "ingress"}, ak, sk)
        for p in rules["Permissions"]["Permission"]:
            if (p.get("PortRange") == rng and p.get("IpProtocol") == proto
                    and p.get("SourceCidrIp") == "0.0.0.0/0"):
                print(f"{proto}/{port} 已放行，跳过")
                return 0
        call(ECS_ENDPOINT, "2014-05-26", "AuthorizeSecurityGroup",
             {"RegionId": REGION, "SecurityGroupId": sg, "IpProtocol": proto,
              "PortRange": rng, "SourceCidrIp": "0.0.0.0/0",
              "Description": "layer-lab"}, ak, sk)
        print(f"已放行 {proto}/{port} (0.0.0.0/0)")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
