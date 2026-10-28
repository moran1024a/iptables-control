#!/usr/bin/env python3
"""检查独立组件清单与源码一致，不要求不同组件使用同一版本。"""
import json
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parent.parent
manifest = json.loads((root / "server/components.json").read_text())
checks = [
    ("server/ictl-server.py", r'^VERSION = "([^"]+)"', manifest["server"]),
    ("deploy/install-server.sh", r'^VERSION="([^"]+)"', manifest["server"]),
    ("agent/ictl.sh", r'^VERSION="([^"]+)"', manifest["forward_agent"]),
    ("tunnel-agent/main.go", r'^const Version = "([^"]+)"', manifest["tunnel_agent"]),
    ("tunnel-agent/install-tunnel.sh", r'^VERSION="([^"]+)"', manifest["tunnel_agent"]),
]
for path, pattern, expected in checks:
    match = re.search(pattern, (root / path).read_text(), re.M)
    if not match or match[1] != expected:
        sys.exit(f"{path}: 版本与组件清单不一致（应为 {expected}）")
if len(sys.argv) > 1 and sys.argv[1].removeprefix("v") != manifest["server"]:
    sys.exit("发布标签与服务端版本不一致")
print("独立组件版本校验通过")
