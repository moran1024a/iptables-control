import hashlib
import os
import re
import sys
import settings


def ver(s):
    """版本号转为可比较的数字元组，如 "0.1.10" → (0, 1, 10)。"""
    return tuple((int(x) for x in re.findall("\\d+", str(s or ""))))


AGENT = None


def load_agent():
    global AGENT
    for path in settings.AGENT_FILES:
        if os.path.isfile(path):
            with open(path, "rb") as f:
                data = f.read()
            m = re.search(b'^VERSION="([^"]+)"', data, re.M)
            if m and m[1].decode() == settings.FORWARD_VERSION:
                AGENT = (settings.FORWARD_VERSION, hashlib.sha256(data).hexdigest(), data)
                return
            print(f"节点端脚本 {path} 与组件清单不一致，节点不会自动更新", file=sys.stderr)
            return
    print("未找到节点端脚本，节点不会自动更新", file=sys.stderr)


def agent_update(agent, auto_update):
    """节点端的更新情况：same / newer / pending / disabled / legacy / unavailable。"""
    if not agent or ver(agent) == ver(settings.FORWARD_VERSION):
        return "same"
    if ver(agent) > ver(settings.FORWARD_VERSION):
        return "newer"
    if ver(agent) < settings.AGENT_AUTO_UPDATE:
        return "legacy"
    if not AGENT:
        return "unavailable"
    return "pending" if auto_update else "disabled"
