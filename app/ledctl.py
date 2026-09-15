#!/usr/device/face_venv/bin/python3.11
# -*- coding: utf-8 -*-
"""
ledctl - 铁牛NAS Zero1 Pro 灯控命令行（v2，配套 led_engine 引擎）

用法:
  ledctl status                    查看灯控状态（含每块盘当前速率）
  ledctl status --json             输出原始状态 JSON

  ledctl power on|off              电源灯 常亮 / 熄灭
  ledctl disk on|off               自动灯效（引擎）开关
  ledctl disk switch on|off        硬盘灯总开关：跟随系统 / 熄灭（掉盘告警仍闪）

  ledctl engine restart            重启引擎
  ledctl engine reload             重新加载配置

  ledctl night power-mode off|heartbeat
                                   夜间电源灯行为：熄灭 / 极简心跳
  ledctl night exception on|off    夜间掉盘告警是否破例闪

  ledctl schedule preset all-off|power-heartbeat|power-only|disk-only|none
                                   一键套用夜间方案
  ledctl schedule set power on|off "22:00-07:00,13:00-14:00"
  ledctl schedule set disk  on|off "22:00-07:00"

  ledctl mute 30 | mute 60 | mute 0    临时熄灯 N 分钟（0=取消）

  ledctl param <键> <值>           改灯效参数（见下）
  ledctl map <槽位> <设备|空>      改硬盘->槽位映射，如 ledctl map 3 sdd

  ledctl demo <槽位> blink|solid|alarm|off   单槽演示
  ledctl sweep                    四槽依次自检（每槽 0.8 秒）
  ledctl probe levels             0 号盘轮流试 0x00/0x40/0x80/0xc0/0xff，
                                  用肉眼判断这组寄存器有没有"亮度分级"
  ledctl lights-off               熄灭全部灯（关机前用）

参数键:
  heartbeat-ms        空闲心跳周期(默认 3000)
  heartbeat-pulse-ms  心跳脉冲长度(默认 60)
  blink-ms            活动快闪半周期(默认 80)
  hold-ms             活动结束后继续快闪的时长(默认 900)
  alarm-ms            掉盘告警慢闪半周期(默认 500)
  threshold-kb        判定"有活动"的最小增量(KB/采样窗口，0=任意)
  disk-breath-ms      硬盘灯呼吸周期(默认 4000)
  disk-breath-min     硬盘灯呼吸最暗亮度%(默认 30，0=全黑)
  disk-breath-step-ms 波浪模式下相邻盘的相位差(默认 220)
  night-hb-ms         夜间电源灯心跳周期(默认 10000)
  night-hb-pulse-ms   夜间电源灯 pulse 长度(默认 80)
  breath-ms           电源灯呼吸周期(默认 3000)
  breath-min          电源灯呼吸最暗亮度%(默认 0)
  breath-max          电源灯呼吸最亮亮度%(默认 100)
  flicker-hz          消闪频率(默认 250，范围 60~2000Hz)。调制开关事件的频率下限，
                      越高越不闪、越费 CPU。250Hz 虽高于人眼融合频率，但眼睛扫视时
                      小颗高对比 LED 会拖出点状重影（幻影阵列），仍有人看得出闪；
                      这种情况直接往 1000~2000 调试试
"""

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time

CONF = os.environ.get("LED_CONFIG", "/etc/nas-led.json")
RUNTIME = os.environ.get("LED_RUNTIME", "/var/run/nas-led.runtime.json")
STATUS = os.environ.get("LED_STATUS", "/var/run/nas-led.status.json")
SVC = "nas-led-engine"

PARAM_MAP = {
    "heartbeat-ms": ("disk", "heartbeat_ms"),
    "heartbeat-pulse-ms": ("disk", "heartbeat_pulse_ms"),
    "blink-ms": ("disk", "activity_blink_ms"),
    "hold-ms": ("disk", "activity_hold_ms"),
    "alarm-ms": ("disk", "alarm_blink_ms"),
    "threshold-kb": ("disk", "activity_threshold_kb"),
    "disk-breath-ms": ("disk", "breath_ms"),
    "disk-breath-min": ("disk", "breath_min"),
    "disk-breath-step-ms": ("disk", "breath_step_ms"),
    "night-hb-ms": ("night", "power_heartbeat_ms"),
    "night-hb-pulse-ms": ("night", "power_heartbeat_pulse_ms"),
    "breath-ms": ("power", "breath_ms"),
    "breath-min": ("power", "breath_min"),
    "breath-max": ("power", "breath_max"),
    "flicker-hz": ("engine", "flicker_hz"),
}

# 枚举型参数（不是数字，单独校验）
ENUM_PARAM = {
    "idle-mode": ("disk", "idle_mode", ("heartbeat", "on", "breath")),
    "breath-mode": ("disk", "breath_mode", ("sync", "wave", "phase")),
}

BREATH_MODE_TEXT = {"sync": "三盘同步", "wave": "波浪追光", "phase": "相位错落"}

PRESETS = {
    "all-off": {"power": True, "disk": True, "mode": "off"},
    "power-heartbeat": {"power": True, "disk": True, "mode": "heartbeat"},
    "power-only": {"power": True, "disk": False, "mode": "off"},
    "disk-only": {"power": False, "disk": True, "mode": "off"},
    "none": {"power": False, "disk": False, "mode": "off"},
}


# --------------------------------------------------------------- 读写

WIN_ITEM_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


def bad_window(w):
    """单个时间段校验；返回错误文案或 None"""
    m = WIN_ITEM_RE.match((w or "").strip())
    if not m:
        return "格式不对（应形如 22:00-07:00）"
    h1, m1, h2, m2 = [int(x) for x in m.groups()]
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return "超出范围（小时 00-23，分钟 00-59）"
    if h1 * 60 + m1 == h2 * 60 + m2:
        return "起止相同，不会生效"
    return None


@contextlib.contextmanager
def conf_lock():
    """配置都是"读-改-写"，多个 ledctl 并发（面板会同时保存两组时间表）
    会互相覆盖，用文件锁串行化。"""
    f = open(CONF + ".lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def touch_runtime(**kw):
    """写运行时文件（mtime 变化会触发引擎重载配置）"""
    rt = load(RUNTIME, {}) or {}
    rt.update(kw)
    save(RUNTIME, rt)


def touch_config(**kw):
    """按 'a.b' 路径写配置，并让运行时文件跳动以触发引擎重载"""
    with conf_lock():
        cfg = load(CONF, {}) or {}
        for path, val in kw.items():
            sect, key = path.split(".", 1)
            cfg.setdefault(sect, {})[key] = val
        save(CONF, cfg)
    touch_runtime(reload=int(time.time() * 1000))


def engine_reload():
    touch_runtime(reload=int(time.time() * 1000))


def systemctl(*args):
    if not os.path.exists("/run/systemd/system"):
        # 容器内没有 systemd: 引擎由容器/compose 管理
        if args and args[0] == "is-active":
            return "active"
        if args and args[0] == "is-enabled":
            return "enabled"
        return "(容器内由应用中心管理)"
    try:
        p = subprocess.run(["systemctl"] + list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=30)
        return (p.stdout or b"").decode("utf-8", "replace").strip()
    except Exception as e:
        return "systemctl 调用失败: %s" % e


# --------------------------------------------------------------- 输出

def fmt_rate(r):
    if r >= 1:
        return "%.1f MB/s" % r
    return "%d KB/s" % int(r * 1024)


MODE_TEXT = {
    "idle": "在线(心跳)",
    "breath": "在线(呼吸)",
    "activity": "读写中(快闪)",
    "alarm": "掉盘告警(慢闪)",
    "solid": "常亮(演示)",
    "blink": "闪烁(演示)",
    "levels": "亮度分级探测",
    "off": "熄灭",
}


def do_status(as_json=False):
    st = load(STATUS, None)
    if st is None:
        print("状态文件不存在：灯控引擎可能未运行（systemctl status %s）" % SVC)
        return 1
    fresh = (time.time() - float(st.get("ts") or 0)) < 6
    if as_json:
        st["fresh"] = fresh
        st["service"] = {"active": systemctl("is-active", SVC),
                         "enabled": systemctl("is-enabled", SVC)}
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0

    print("=== 灯控引擎 ===")
    print("  引擎    : %s (%s)" % ("运行中" if fresh else "未响应", systemctl("is-active", SVC)))
    print("  开机自启: %s" % (st.get("enabled") or "unknown"))
    print("  硬件映射: %s" % ("正常" if st.get("hw_ok") else "不可用"))
    print("  已运行  : %d 秒" % int(st.get("uptime") or 0))
    print("=== 电源灯 ===")
    pw = st["power"]
    print("  设定    : %s" % {"on": "常亮", "breath": "呼吸", "off": "熄灭"}.get(pw.get("mode"), pw.get("mode")))
    if pw.get("mode") == "breath":
        print("  呼吸    : 周期 %sms · 亮度 %s%%~%s%%"
              % (pw.get("breath_ms"), pw.get("breath_min"), pw.get("breath_max")))
    print("  实际    : %s" % ("亮" if pw["state"] == "on" else "灭"))
    if pw.get("port_ok") is False:
        print("  注意    : /dev/port 不可用，呼吸已降级为常亮")
    n = st["night"]
    print("  夜间    : %s%s" % ("生效中" if n["power"] else "未生效",
                               "（电源灯极简心跳）" if n["power_mode"] == "heartbeat" else ""))
    print("=== 夜间定时 ===")
    for k, name in (("power", "电源灯"), ("disk", "硬盘灯")):
        s = st["schedule"][k]
        print("  %s    : %s  %s" % (name, "开启" if s.get("enabled") else "关闭",
                                    " ".join(s.get("windows") or []) or "-"))
    print("  告警破例: %s" % ("是" if n["exception"] else "否"))
    if n["mute_left"]:
        print("  临时熄灯: 剩余 %d 分钟" % n["mute_left"])
    print("=== 硬盘灯 ===")
    d = st["disk"]
    idle = d.get("idle_mode", "heartbeat")
    print("  自动灯效: %s" % ("开启" if d["enabled"] else "关闭"))
    sw = d.get("switch", "on")
    if sw == "off":
        print("  总开关  : 熄灭（掉盘告警仍会闪；手动打开恢复）")
    else:
        print("  总开关  : 跟随系统")
    print("  空闲灯语: %s" % {"on": "长亮", "breath": "呼吸"}.get(idle, "心跳"))
    if idle == "breath":
        bm = d.get("breath_mode", "sync")
        print("  呼吸方式: %s%s · 周期 %sms · 最暗 %s%%"
              % (BREATH_MODE_TEXT.get(bm, bm), 
                 "（相邻错开 %sms）" % d.get("breath_step_ms") if bm == "wave" else "",
                 d.get("breath_ms"), d.get("breath_min")))
    print("  参数    : 心跳 %sms/%sms  快闪 %sms 保持 %sms  告警 %sms  阈值 %sKB"
          % (d["heartbeat_ms"], d["heartbeat_pulse_ms"], d["activity_blink_ms"],
             d["activity_hold_ms"], d["alarm_blink_ms"], d["threshold_kb"]))
    print("  优先级  : 告警 > 活动 > 空闲灯语（有 IO 立刻打断呼吸）")
    print("  槽位    设备      容量     在线   当前状态          速率")
    for s in st["disks"]:
        dev = s["dev"] or "(空)"
        print("  %-6s %-9s %5sGB  %-5s %-16s %s"
              % (s["slot"], dev, s["gb"], "是" if s["online"] else "否",
                 MODE_TEXT.get(s["mode"], s["mode"]), fmt_rate(s["rate"]) if s["dev"] else "-"))
    return 0


# --------------------------------------------------------------- 子命令

def do_power(arg):
    if arg not in ("on", "off", "breath"):
        print("用法: ledctl power on|off|breath")
        return 1
    if arg == "breath":
        # 呼吸只在白天生效；若当前处于夜间时段会先按夜间策略走，这里给出提示
        touch_config(**{"power.mode": arg})
        time.sleep(0.4)
        st = load(STATUS, {}) or {}
        if (st.get("night") or {}).get("power"):
            print("电源灯 -> 呼吸（当前处于夜间时段，仍按夜间策略显示）")
        else:
            print("电源灯 -> 呼吸（周期 %sms）"
                  % int((st.get("power") or {}).get("breath_ms") or 3000))
        return 0
    touch_config(**{"power.mode": arg})
    time.sleep(0.4)
    print("电源灯 -> %s" % ("常亮" if arg == "on" else "熄灭"))
    return 0


def do_disk(argv):
    if not argv:
        print("用法: ledctl disk on|off | disk switch on|off"
              " | disk idle heartbeat|on|breath | disk breath sync|wave|phase")
        return 1
    arg = argv[0]
    if arg in ("on", "off"):
        touch_config(**{"disk.enabled": arg == "on"})
        time.sleep(0.4)
        print("自动灯效 -> %s" % ("开启" if arg == "on" else "关闭"))
        return 0
    if arg == "switch":
        v = argv[1] if len(argv) > 1 else ""
        if v not in ("on", "off"):
            print("用法: ledctl disk switch on|off")
            return 1
        touch_config(**{"disk.switch": v})
        time.sleep(0.4)
        print("硬盘灯 -> %s" % ("跟随系统" if v == "on" else "熄灭（掉盘告警仍会闪）"))
        return 0
    if arg == "idle":
        v = argv[1] if len(argv) > 1 else ""
        if v not in ("breath", "heartbeat", "on"):
            print("用法: ledctl disk idle heartbeat|on|breath")
            return 1
        touch_config(**{"disk.idle_mode": v})
        time.sleep(0.4)
        print("空闲灯语 -> %s" % {"on": "长亮", "breath": "呼吸"}.get(v, "心跳"))
        return 0
    if arg == "breath":
        v = argv[1] if len(argv) > 1 else ""
        if v not in ("sync", "wave", "phase"):
            print("用法: ledctl disk breath sync(同步)|wave(波浪)|phase(错落)")
            return 1
        touch_config(**{"disk.breath_mode": v})
        time.sleep(0.4)
        print("呼吸方式 -> %s" % BREATH_MODE_TEXT[v])
        return 0
    print("用法: ledctl disk on|off | disk idle heartbeat|on|breath | disk breath sync|wave|phase")
    return 1


def do_engine(arg):
    if arg == "restart":
        print(systemctl("restart", SVC))
        time.sleep(1)
        print("当前: %s" % systemctl("is-active", SVC))
    elif arg == "reload":
        engine_reload()
        print("已请求重新加载配置")
    elif arg == "stop":
        print(systemctl("stop", SVC))
    elif arg == "start":
        print(systemctl("start", SVC))
    else:
        print("用法: ledctl engine restart|reload|start|stop")
        return 1
    return 0


def do_night(argv):
    if len(argv) < 2:
        print("用法: ledctl night power-mode off|heartbeat | night exception on|off")
        return 1
    if argv[0] == "power-mode":
        v = argv[1]
        if v not in ("off", "heartbeat"):
            print("只能是 off 或 heartbeat")
            return 1
        touch_config(**{"night.power_mode": v})
        print("夜间电源灯 -> %s" % ("极简心跳" if v == "heartbeat" else "熄灭"))
    elif argv[0] == "exception":
        v = argv[1] == "on"
        touch_config(**{"alarm.night_exception": v})
        print("夜间掉盘告警破例 -> %s" % ("开" if v else "关"))
    else:
        print("用法: ledctl night power-mode off|heartbeat | night exception on|off")
        return 1
    return 0


def do_schedule(argv):
    if not argv:
        print("用法: ledctl schedule set <power|disk> on|off [窗口] | schedule preset <方案>")
        return 1
    if argv[0] == "preset":
        if len(argv) < 2 or argv[1] not in PRESETS:
            print("可用方案: %s" % " | ".join(PRESETS))
            return 1
        p = PRESETS[argv[1]]
        with conf_lock():
            cfg = load(CONF, {}) or {}
            for g, en in (("power", p["power"]), ("disk", p["disk"])):
                s = cfg.setdefault("schedule", {}).setdefault(g, {})
                s["enabled"] = en
                if en and not s.get("windows"):
                    s["windows"] = ["22:00-07:00"]
            cfg.setdefault("night", {})["power_mode"] = p["mode"]
            save(CONF, cfg)
        touch_runtime(reload=int(time.time() * 1000))
        print("已套用方案: %s" % argv[1])
        return 0
    if argv[0] == "set" and len(argv) >= 3:
        grp = argv[1]
        if grp not in ("power", "disk"):
            print("组只能是 power 或 disk")
            return 1
        en = argv[2] == "on"
        wins = None
        if len(argv) >= 4:
            raw = argv[3].strip()
            if raw in ("", "-", "none", "clear"):
                wins = []          # 显式清空时间段
            else:
                wins = [w.strip() for w in raw.replace(";", ",").split(",") if w.strip()]
                for w in wins:
                    err = bad_window(w)
                    if err:
                        print("时间段 %s 不合法: %s" % (w, err))
                        return 1
        with conf_lock():
            cfg = load(CONF, {}) or {}
            s = cfg.setdefault("schedule", {}).setdefault(grp, {})
            s["enabled"] = en
            if wins is not None:
                s["windows"] = wins
            save(CONF, cfg)
        touch_runtime(reload=int(time.time() * 1000))
        shown = " ".join(s.get("windows") or []) or "(空)"
        print("%s 定时 -> %s  %s" % (grp, "开启" if en else "关闭", shown))
        if en and not s.get("windows"):
            print("提示: 已启用但时间段为空，当前不会有任何效果")
        return 0
    print("用法: ledctl schedule set <power|disk> on|off [窗口] | schedule preset <方案>")
    return 1


def do_mute(arg):
    try:
        minutes = int(arg)
    except Exception:
        print("用法: ledctl mute <分钟>  (0 取消)")
        return 1
    if minutes <= 0:
        touch_runtime(mute_until=0)
        print("临时熄灯已取消")
    else:
        until = time.time() + minutes * 60
        touch_runtime(mute_until=until)
        print("已临时熄灯 %d 分钟（到 %s 自动恢复）"
              % (minutes, time.strftime("%H:%M", time.localtime(until))))
    return 0


def do_param(argv):
    if len(argv) < 2 or (argv[0] not in PARAM_MAP and argv[0] not in ENUM_PARAM):
        print("可用参数: %s" % " ".join(sorted(list(PARAM_MAP) + list(ENUM_PARAM))))
        return 1
    if argv[0] in ENUM_PARAM:
        sect, key, allowed = ENUM_PARAM[argv[0]]
        val = argv[1]
        if val not in allowed:
            print("只能是: %s" % " | ".join(allowed))
            return 1
        touch_config(**{"%s.%s" % (sect, key): val})
        print("%s -> %s" % (argv[0], val))
        return 0
    sect, key = PARAM_MAP[argv[0]]
    try:
        val = float(argv[1])
        val = int(val) if val == int(val) else val
    except Exception:
        print("值必须是数字")
        return 1
    touch_config(**{"%s.%s" % (sect, key): val})
    print("%s -> %s" % (argv[0], val))
    return 0


def do_map(argv):
    if len(argv) < 2:
        print("用法: ledctl map <槽位0-3> <设备名|空>")
        return 1
    slot = int(argv[0])
    dev = "" if argv[1] in ("-", "empty", "空") else argv[1]
    cfg = load(CONF, {}) or {}
    slots = list(cfg.setdefault("disk", {}).get("slots") or [])
    while len(slots) < 4:
        slots.append("")
    slots[slot] = dev
    cfg["disk"]["slots"] = slots
    save(CONF, cfg)
    touch_runtime(reload=int(time.time() * 1000))
    print("槽位 %d -> %s" % (slot, dev or "(空)"))
    return 0


def do_demo(argv):
    if len(argv) < 2 or not argv[0].isdigit():
        print("用法: ledctl demo <槽位0-3> blink|solid|alarm|levels|off")
        return 1
    slot, mode = int(argv[0]), argv[1]
    if mode == "off":
        touch_runtime(demo={"slot": -1, "mode": "", "until": 0})
        print("演示已停止")
        return 0
    if mode not in ("blink", "solid", "alarm", "levels"):
        print("模式只能是 blink / solid / alarm / levels / off")
        return 1
    until = time.time() + (600 if mode == "alarm" else (12 if mode == "levels" else 6))
    touch_runtime(demo={"slot": slot, "mode": mode, "until": until})
    print("槽位 %d 演示: %s" % (slot, mode))
    return 0


def do_probe(argv):
    if not argv or argv[0] != "levels":
        print("用法: ledctl probe levels")
        return 1
    touch_runtime(demo={"slot": 0, "mode": "levels", "until": time.time() + 12})
    print("0 号盘「亮度分级探测」开始（每 2 秒换一档，共 10 秒）")
    print("顺序: 0x00 全亮 -> 0x40 -> 0x80 -> 0xc0 -> 0xff 全灭")
    print("看灯有没有中间亮度：有 5 级 = 这组寄存器支持模拟调光；")
    print("只有亮/灭两态 = 纯开关，呼吸只能靠 PWM（当前实现就是这样，也够用）")
    return 0


def do_sweep():
    now = time.time()
    touch_runtime(sweep={"start": now, "until": now + 0.8 * 4 + 0.6})
    print("四槽依次自检开始（每槽 0.8 秒）")
    return 0


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    cmd = a[0]
    if cmd == "status":
        return do_status("--json" in a)
    if cmd == "power":
        return do_power(a[1] if len(a) > 1 else "")
    if cmd == "disk":
        return do_disk(a[1:])
    if cmd == "engine":
        return do_engine(a[1] if len(a) > 1 else "")
    if cmd == "probe":
        return do_probe(a[1:])
    if cmd == "night":
        return do_night(a[1:])
    if cmd == "schedule":
        return do_schedule(a[1:])
    if cmd == "mute":
        return do_mute(a[1] if len(a) > 1 else "")
    if cmd == "param":
        return do_param(a[1:])
    if cmd == "map":
        return do_map(a[1:])
    if cmd == "demo":
        return do_demo(a[1:])
    if cmd in ("sweep", "all"):
        return do_sweep()
    if cmd in ("lights-off", "off"):
        touch_runtime(demo={"slot": -1, "mode": "", "until": 0}, mute_until=0)
        print(systemctl("stop", SVC))
        print("已熄灭全部灯")
        return 0
    print("未知命令: %s（执行 ledctl help 看用法）" % cmd)
    return 1


if __name__ == "__main__":
    sys.exit(main())
