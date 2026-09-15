# -*- coding: utf-8 -*-
"""tieniuled 安装 / 重启后验证

用法:
    python verify.py http://NAS_IP:8977

检查项:
    1. 面板是否在线（GET /api/status）
    2. 灯控引擎是否在跑、状态文件是否新鲜
    3. 灯控硬件是否可用（hw_ok：电源灯走 Super I/O、硬盘灯走 SATA 控制器）
    4. 电源灯 / 硬盘灯当前模式与开关
    5. 夜间计划：时间段、当前是否在夜间、下一次切换时间
    6. 在位硬盘与各槽位当前灯态
"""
import json
import sys
import time
import urllib.request

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)

HOST = sys.argv[1].rstrip("/")
# 绕过系统代理：局域网地址走代理常常直接失败
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path):
    req = urllib.request.Request(HOST + path)
    return json.loads(opener.open(req, timeout=15).read().decode("utf-8"))


def hhmm(minutes):
    return "%02d:%02d" % (minutes // 60 % 24, minutes % 60)


def parse_windows(wins):
    """与 led_engine.parse_windows 同款：返回 [(起, 止)] 分钟区间，非法项丢弃"""
    out = []
    for w in wins or []:
        if "-" not in w:
            continue
        a, b = w.split("-", 1)
        try:
            h1, m1 = [int(x) for x in a.strip().split(":")]
            h2, m2 = [int(x) for x in b.strip().split(":")]
        except Exception:
            continue
        s, e = h1 * 60 + m1, h2 * 60 + m2
        if s == e or not (0 <= s < 1440 and 0 <= e < 1440):
            continue
        out.append((s, e))
    return out


def in_any(ranges, now):
    for s, e in ranges:
        if s < e:
            if s <= now < e:
                return True
        elif now >= s or now < e:          # 跨零点（如 22:00-07:00）
            return True
    return False


def next_boundary(ranges, now):
    """下一次状态翻转的时刻（分钟数），没有则返回 None"""
    best = None
    for s, e in ranges:
        for t in (s, e):
            delta = (t - now) % 1440
            if delta == 0:
                delta = 1440
            if best is None or delta < best[0]:
                best = (delta, t)
    return best


def main():
    print("目标: %s" % HOST)

    # ---- 1) 在线 ----
    try:
        s = api("/api/status")
    except Exception as e:
        print("面板不可达 ❌  %s" % e)
        print("  · 确认容器在跑：docker ps | grep tieniuled")
        print("  · 确认端口是否正确（默认 8977）")
        return 1
    if not s.get("ok"):
        print("面板返回异常 ❌  %s" % s.get("error"))
        return 1
    d = s.get("data") or {}

    fresh = time.time() - float(d.get("ts") or 0)
    print("面板在线 ✅   状态文件 %.1f 秒前更新" % fresh)

    # ---- 2) 引擎 ----
    # 引擎健康度只看「状态文件是否在持续刷新」——引擎每秒重写一次；
    # status 里的 service/enabled 是宿主机版遗留字段（容器内无 systemd，恒为
    # active/enabled/unknown），不能当判据，面板 UI 也是靠新鲜度判断的。
    print("\n=== 引擎 ===")
    if fresh < 6:
        print("  引擎运行中 ✅   pid = %s    uptime = %ss"
              % (d.get("pid"), d.get("uptime")))
    elif fresh < 30:
        print("  引擎可能卡顿 ⚠️   状态已 %.0f 秒未刷新（正常应 1 秒一次）" % fresh)
    else:
        print("  引擎已停止 ❌   状态已 %.0f 秒未刷新" % fresh)
        print("     · 容器内看日志：docker logs tieniuled")
        print("     · 引擎由 app/run.sh 在容器启动时拉起")

    # ---- 3) 硬件 ----
    hw = d.get("hw_ok")
    print("\n=== 灯控硬件 ===")
    print("  hw_ok = %s   %s" % (hw, "✅ 寄存器可写" if hw else "❌ 未接上 Super I/O / SATA 控制器"))
    pw = d.get("power") or {}
    print("  电源灯端口 port_ok = %s" % pw.get("port_ok"))

    # ---- 4) 两盏灯 ----
    print("\n=== 电源灯 ===")
    print("  模式 = %-6s  当前状态 = %s   呼吸周期 = %sms (最低 %s%%)"
          % (pw.get("mode"), pw.get("state"), pw.get("breath_ms"), pw.get("breath_min")))

    dk = d.get("disk") or {}
    print("\n=== 硬盘灯 ===")
    print("  启用 = %-5s  总开关 = %-4s(配置值 %-4s)  空闲灯语 = %s"
          % (dk.get("enabled"), dk.get("switch"), dk.get("switch_cfg"), dk.get("idle_mode")))
    disks = d.get("disks") or []
    if disks:
        for x in disks:
            print("  槽位 %s  %-12s 在线=%-5s 灯态=%-4s 亮度=%.2f  %sGB"
                  % (x.get("slot"), x.get("dev") or "-", x.get("online"),
                     x.get("state"), x.get("level") or 0, x.get("gb") or 0))
    else:
        print("  （未识别到硬盘槽位）")

    # ---- 5) 夜间计划 ----
    sch = d.get("schedule") or {}
    now = time.localtime()
    now_min = now.tm_hour * 60 + now.tm_min
    print("\n=== 夜间计划（本机当前 %s）===" % hhmm(now_min))
    for key, label in (("power", "电源灯"), ("disk", "硬盘灯")):
        g = sch.get(key) or {}
        wins = g.get("windows") or []
        rng = parse_windows(wins)
        on = bool(g.get("enabled"))
        inside = on and in_any(rng, now_min)
        print("  %s: %s  时间段 %s  当前 %s"
              % (label, "已启用" if on else "未启用",
                 " ".join(wins) if wins else "(空)",
                 ("在夜间时段内" if inside else "不在夜间时段") if on else "-"))
        if on and rng:
            nb = next_boundary(rng, now_min)
            if nb:
                print("        下一次切换: %s（%d 分钟后）" % (hhmm(nb[1]), nb[0]))
        elif on and not rng:
            print("        ⚠️ 已启用但时间段为空——不会有任何效果")

    night = d.get("night") or {}
    if night.get("mute"):
        print("  临时熄灯中，剩余 %s 分钟" % night.get("mute_left"))
    if dk.get("switch_cfg") == "off":
        print("  ⚠️ 硬盘灯总开关是「熄灭」——定时改的也是熄灭状态，看不出效果，"
              "请把总开关切到「跟随系统」")

    print("\n验证完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
