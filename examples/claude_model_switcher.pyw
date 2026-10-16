#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude 模型切换器 —— 点哪个模型，就把 ~/.claude/settings.json 切到那个 profile，
并自动拉起对应的本地代理。双击桌面「Claude模型切换器.cmd」启动。

链路说明（两种）：
  普通 profile : Claude Code -> ccproxy:<port> -> 上游 API
  Zen 免费模型 : Claude Code -> ccproxy:3462 -> zen_bridge:8787 -> opencode serve:41234 -> Zen

带 x_bridge 的 profile 会在切换时先确保 zen_bridge 已运行；因为 zen_bridge 要
顺带拉起 opencode serve，冷启动需要约 25-40 秒，所以切换动作放在后台线程执行，
界面用轮询显示进度，避免 GUI 假死。

删除模型：免费模型经常过期/失效。**免费模型（profile 带 x_free）是真删**——
os.remove 直接干掉，不留 backup（免费模型要么不好用、要么已失效，没有保留价值）；
只有付费模型才归档到 ~/.claude/backups/deleted/（可恢复）。删完点「刷新」或重启
本工具即可，不用改代码、不用重新部署。
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

HOME = os.path.expanduser("~")
PY = r"C:\Users\DELL\.workbuddy\binaries\python\versions\3.13.12\python.exe"
CC = r"C:\Users\DELL\.workbuddy\skills\claude-code-third-party-model\scripts\ccproxy.py"
ZB = r"C:\Users\DELL\.workbuddy\skills\claude-code-third-party-model\scripts\zen_bridge.py"
ZB_PORT = 8787
ZB_SDK_PORT = 41234

# name=显示名, profile=profiles 下文件名(无后缀), port=本地 ccproxy 端口(None=直连),
# args=ccproxy 启动参数, bridge=是否需要先拉起 zen_bridge
MODELS = [
    ("DeepSeek V4.1 Flash（免费·Zen）", "zen-ds-flash", 3462,
     "--upstream http://127.0.0.1:8787/v1/chat/completions "
     "--model deepseek-v4.1-flash-free --key anonymous", True),
    ("Kilo 文本 + 读图桥接（免费）", "kilo-bridge", 3457,
     "--upstream https://api.kilo.ai/api/gateway/v1/chat/completions --model kilo-auto/free --key anonymous", False),
    ("读图 Vision（免费·可看图）", "vision", 3458,
     "--upstream https://api.kilo.ai/api/gateway/v1/chat/completions --model nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free --key anonymous", False),
    ("Kilo 快稳 LFM2.5（免费·agent首选）", "kilo-fast", 3461,
     "--upstream https://api.kilo.ai/api/gateway/v1/chat/completions --model liquid/lfm-2.5-2.6b:free --key anonymous", False),
    ("DeepSeek-V4 (付费)", "deepseek", None, "", False),
    ("GLM-4-Flash（免费·稳定）", "glm4", None, "", False),
    ("GLM-4-Flash + 读图桥接（免费）", "glm4-bridge", None, "", False),
]

DETACHED = 0x00000008  # DETACHED_PROCESS

# 删除下拉框用的短标签（按钮显示名太长，Combobox 里放不下会被截断）：
# 精简到「模型名 + 免费/付费」，一眼可辨又短。没列到的用 fallback 逻辑补。
DEL_LABELS = {
    "zen-ds-flash": "DS V4.1 Flash 免费·Zen",
    "deepseek":     "DeepSeek-V4 付费",
    "glm4":         "GLM-4-Flash 免费",
    "glm4-bridge":  "GLM-4-Flash 免费+读图",
    "glm41v":       "GLM-4.1V 思考 免费",
    "glm46v":       "GLM-4.6V 免费",
    "kilo-bridge":  "Kilo 文本 免费+读图",
    "kilo-fast":    "Kilo LFM2.5 免费",
    "vision":       "读图 Vision 免费",
}


def port_open(port, timeout=0.4):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def http_ok(port, path, timeout=2.0):
    """比 TCP 探测更可靠：确认端口上确实是我们要的服务。"""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", path)
        r = c.getresponse()
        ok = r.status == 200
        r.read()
        c.close()
        return ok
    except Exception:
        return False


def wait_for(fn, timeout, interval=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


def _spawn(args, logfile):
    """后台拉起进程。清掉代理环境变量——本机回环请求必须直连，
    否则会落到 http_proxy 上被拒（本机那个 127.0.0.1:14247 代理常年失效）。"""
    log = os.path.join(HOME, ".claude", logfile)
    env = os.environ.copy()
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    with open(log, "ab") as lf:
        subprocess.Popen(args, stdout=lf, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, env=env,
                         creationflags=DETACHED, close_fds=True)


def ensure_bridge(timeout=90):
    """确保 zen_bridge 在 8787 可用；它会自己拉起 opencode serve(41234)。"""
    if http_ok(ZB_PORT, "/healthz", 1.5):
        return "桥接已在运行", True
    _spawn([PY, ZB, "--port", str(ZB_PORT), "--sdk-port", str(ZB_SDK_PORT),
            "--auto-start"], "zen-bridge.log")
    if not wait_for(lambda: port_open(ZB_PORT), timeout):
        return "桥接启动超时，见 ~/.claude/zen-bridge.log", False
    if wait_for(lambda: http_ok(ZB_PORT, "/healthz", 3), 30):
        return "桥接已启动", True
    return "桥接端口已开但健康检查未通过，见 ~/.claude/zen-bridge.log", False


def ensure_proxy(port, args, timeout=30):
    if port is None:
        return "（直连，无需代理）", True
    if port_open(port):
        return "代理已在运行", True
    _spawn([PY, CC, "--port", str(port)] + args.split(), "ccproxy-%d.log" % port)
    if wait_for(lambda: port_open(port), timeout):
        return "已启动代理", True
    return "代理启动超时，见 ~/.claude/ccproxy-%d.log" % port, False


def current_model():
    try:
        s = json.load(open(os.path.join(HOME, ".claude", "settings.json"), encoding="utf-8"))
        env = s.get("env", {})
        return env.get("ANTHROPIC_BASE_URL", ""), (s.get("model") or env.get("ANTHROPIC_MODEL", ""))
    except Exception:
        return "", ""


PROFILES_DIR = os.path.join(HOME, ".claude", "profiles")
DELETE_DIR = os.path.join(HOME, ".claude", "backups", "deleted")


def list_profile_names():
    """profiles 目录下所有 profile 名（不含 .json）。"""
    try:
        return sorted(f[:-5] for f in os.listdir(PROFILES_DIR)
                      if f.endswith(".json") and os.path.isfile(os.path.join(PROFILES_DIR, f)))
    except OSError:
        return []


def delete_profile(name, permanent=False):
    """删除 profile。

    permanent=True（免费模型，profile 带 x_free）：os.remove 真删，不留 backup。
    permanent=False（付费模型）：移入 ~/.claude/backups/deleted/，可恢复。
    返回 (归档路径 or None, 是否真删)。
    """
    src = os.path.join(PROFILES_DIR, name + ".json")
    if not os.path.isfile(src):
        raise FileNotFoundError("profile 不存在: " + src)
    if permanent:
        os.remove(src)
        return None, True
    os.makedirs(DELETE_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(DELETE_DIR, "%s.json.deleted-%s" % (name, ts))
    shutil.move(src, dest)
    return dest, False
def switch(profile, port, args, bridge):
    pf = os.path.join(HOME, ".claude", "profiles", profile + ".json")
    prof = json.load(open(pf, encoding="utf-8"))
    sp = os.path.join(HOME, ".claude", "settings.json")
    settings = json.load(open(sp, encoding="utf-8"))
    settings["env"] = prof.get("env", {})
    settings["model"] = prof.get("model")
    if "modelSettings" in prof:
        settings["modelSettings"] = prof["modelSettings"]
    # 读图桥接：profile 带 mcpServers 就注入，否则清掉（避免非桥接 profile 残留 MCP）
    if "mcpServers" in prof:
        settings["mcpServers"] = prof["mcpServers"]
    else:
        settings.pop("mcpServers", None)
    tmp = sp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp, sp)

    notes = []
    ok = True
    if bridge:
        msg, ok = ensure_bridge()
        notes.append(msg)
        if ok:
            # 桥接起来了，ccproxy 才可能连得上上游
            pmsg, pok = ensure_proxy(port, args)
            notes.append(pmsg)
            ok = ok and pok
    else:
        pmsg, pok = ensure_proxy(port, args)
        notes.append(pmsg)
        ok = pok
    return "; ".join(notes), ok


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Claude 模型切换器")
        self.geometry("430x915")
        self.minsize(400, 500)      # 允许拖动改大小，但别拖到看不清
        self.resizable(True, True)
        tk.Label(self, text="点哪个模型，就切到哪个\n切完重启 Claude Code 即可",
                 font=("Microsoft YaHei", 11), pady=6).pack()

        self.status = tk.Label(self, text="", fg="green", font=("Microsoft YaHei", 9),
                               wraplength=390, justify="left", height=3)
        self.status.pack(pady=4)

        self.buttons = []
        for disp, prof, port, args, bridge in MODELS:
            b = tk.Button(self, text=disp, height=2, font=("Microsoft YaHei", 11),
                          command=lambda d=disp, p=prof, pt=port, a=args, br=bridge:
                          self.on_click(d, p, pt, a, br))
            b.pack(fill="x", padx=26, pady=5)
            self.buttons.append(b)

        # ---- 删除模型（免费模型失效时一键移除入口）----
        sep = tk.Frame(self, height=2, bd=1, relief="sunken")
        sep.pack(fill="x", padx=26, pady=8)
        tk.Label(self, text="删除模型（免费模型真删·付费归档可恢复）",
                 font=("Microsoft YaHei", 9), fg="gray").pack()
        # Combobox 独占一行全宽（放按钮旁边会被挤到 200px，文字全截断）
        self.del_combo = ttk.Combobox(self, state="readonly", height=12,
                                      font=("Microsoft YaHei", 10))
        self.del_combo.pack(fill="x", padx=26, pady=4)
        btns = tk.Frame(self)
        btns.pack(fill="x", padx=26, pady=2)
        tk.Button(btns, text="刷新列表", width=10,
                  command=self.refresh_del_list).pack(side="left")
        tk.Button(btns, text="删除所选模型", width=14, fg="darkred",
                  command=self.on_delete).pack(side="right")
        # 吸收多余高度：窗口拉高时不留死白
        tk.Frame(self).pack(fill="both", expand=True)

        self._task = None
        self._del_map = {}  # 下拉显示文本 -> profile 名
        self.refresh()

    def refresh_del_list(self):
        """下拉列表显示「短标签 [profile名]」，分得清免费/付费，不会误删。"""
        names = list_profile_names()
        self._del_map = {}
        labels = []
        for n in names:
            label = DEL_LABELS.get(n)
            if not label:
                # DEL_LABELS 没列到的（新加的 profile）：用模型 ID 短名补充
                try:
                    p = json.load(open(os.path.join(PROFILES_DIR, n + ".json"),
                                       encoding="utf-8"))
                    mid = p.get("model") or (p.get("env") or {}).get("ANTHROPIC_MODEL", "")
                    mid = mid.split("/")[-1].split("[")[0]
                    label = ("%s（%s）" % (n, mid[:28])) if mid else n
                except Exception:
                    label = n
            text = "%s  [%s]" % (label, n)
            labels.append(text)
            self._del_map[text] = n
        self.del_combo["values"] = labels
        if labels and not self.del_combo.get():
            self.del_combo.current(0)
        elif self.del_combo.get() not in self._del_map:
            self.del_combo.set("")

    def on_delete(self):
        text = self.del_combo.get().strip()
        name = getattr(self, "_del_map", {}).get(text, text)
        if not name:
            messagebox.showwarning("没有选中", "先在下拉框里选一个要删除的模型")
            return
        try:
            prof = json.load(open(os.path.join(PROFILES_DIR, name + ".json"),
                                  encoding="utf-8"))
        except Exception as e:
            messagebox.showerror("读不了这个 profile", str(e))
            return
        model = prof.get("model") or (prof.get("env") or {}).get("ANTHROPIC_MODEL", "?")
        is_free = bool(prof.get("x_free"))
        if is_free:
            body = ("将【永久删除】免费模型「%s」\nprofile 文件: %s\n上游模型 ID: %s\n\n"
                    "免费模型直接真删，不留 backup。\n确定删除？" % (text, name + ".json", model))
        else:
            body = ("将删除模型「%s」\nprofile 文件: %s\n上游模型 ID: %s\n\n"
                    "操作 = 把该 profile 移入\n~/.claude/backups/deleted/\n"
                    "（不是硬删，需要时可捡回）\n\n确定删除？"
                    % (text, name + ".json", model))
        if not messagebox.askyesno("确认删除", body):
            return
        try:
            dest, real_delete = delete_profile(name, permanent=is_free)
        except Exception as e:
            messagebox.showerror("删除失败", str(e))
            return
        # 如果删的是当前生效的 profile，提醒一句（settings.json 本身不动，
        # 只是从此没有 profile 与它对应，显示会变成"未知"）。
        cur_base, cur_model = current_model()
        pbase = (prof.get("env") or {}).get("ANTHROPIC_BASE_URL", "")
        pmodel = prof.get("model") or (prof.get("env") or {}).get("ANTHROPIC_MODEL", "")
        extra = ""
        if pbase == cur_base and pmodel == cur_model:
            extra = ("\n\n注意：这是当前正在用的模型。settings.json 未改动，"
                     "但已无 profile 与它对应，建议再点一个别的模型。")
        self.refresh_del_list()
        self.refresh()
        if real_delete:
            done_msg = "「%s」已永久删除（免费模型，无 backup）。" % text
        else:
            done_msg = "「%s」已移除。\n归档到：%s（可恢复）" % (text, dest)
        messagebox.showinfo(
            "已删除",
            "%s%s\n\n重启 Claude Code 后，切换器列表不再显示它。"
            % (done_msg, extra))

    def refresh(self):
        cur_base, cur_model = current_model()
        try:
            s = json.load(open(os.path.join(HOME, ".claude", "settings.json"), encoding="utf-8"))
            has_bridge = "vision-bridge" in s.get("mcpServers", {})
        except Exception:
            has_bridge = False
        cur_disp = "未知"
        for disp, prof, port, args, bridge in MODELS:
            try:
                p = json.load(open(os.path.join(HOME, ".claude", "profiles", prof + ".json"), encoding="utf-8"))
            except Exception:
                continue
            pbase = p["env"]["ANTHROPIC_BASE_URL"]
            pmodel = p.get("model") or p["env"].get("ANTHROPIC_MODEL", "")
            pbridge = "vision-bridge" in p.get("mcpServers", {})
            if pbase == cur_base and pmodel == cur_model and pbridge == has_bridge:
                cur_disp = disp
                break
        self.status.config(
            text="当前：%s\n切换后重启 Claude Code；想保留上下文重启时加 --continue" % cur_disp,
            fg="blue")
        self.refresh_del_list()

    def _set_busy(self, busy, text=None, color=None):
        state = tk.DISABLED if busy else tk.NORMAL
        for b in self.buttons:
            b.config(state=state)
        if text is not None:
            self.status.config(text=text, fg=color or "blue")
        self.update_idletasks()

    def on_click(self, disp, prof, port, args, bridge):
        if not os.path.isfile(os.path.join(PROFILES_DIR, prof + ".json")):
            messagebox.showwarning(
                "该模型已删除",
                "「%s」的 profile 已被删除。\n点下方「刷新列表」或重启本工具。"
                % disp)
            self.refresh_del_list()
            return
        self._task = {"disp": disp, "prof": prof, "port": port, "args": args,
                      "bridge": bridge, "done": False, "result": None}
        tip = ("正在拉起 Zen 桥接（约 30 秒，含 opencode serve 冷启动）..."
               if bridge else "正在切换...")
        self._set_busy(True, tip, "#b8860b")
        threading.Thread(target=self._worker, daemon=True).start()
        self.after(300, self._poll)

    def _worker(self):
        t = self._task
        try:
            t["result"] = switch(t["prof"], t["port"], t["args"], t["bridge"])
        except Exception as e:
            t["result"] = ("失败：%s" % e, False)
        finally:
            t["done"] = True

    def _poll(self):
        t = self._task
        if t is None:
            return
        if not t["done"]:
            self.after(300, self._poll)
            return
        msg, ok = t["result"]
        self._set_busy(False)
        extra = ""
        if t["bridge"] and ok:
            extra = ("\n\n桥接已就绪，WorkBuddy 侧也能用同一个模型：\n"
                     "模型列表选 custom-local:deepseek-v4.1-flash-free")
        messagebox.showinfo(
            "已切换" if ok else "部分失败",
            "已切换到：%s\n%s%s\n\n重启 Claude Code 后生效。\n"
            "想接着之前的对话继续：重启时用 claude --continue\n"
            "（换模型不会丢会话，历史都在 ~/.claude/projects/）"
            % (t["disp"], msg, extra))
        self._task = None
        self.refresh()


if __name__ == "__main__":
    try:
        App().mainloop()
    except Exception:
        import traceback
        log = os.path.join(HOME, ".claude", "switcher-error.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        try:
            messagebox.showerror("切换器出错", "详情见 " + log)
        except Exception:
            pass
