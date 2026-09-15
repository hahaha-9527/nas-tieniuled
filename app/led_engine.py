#!/usr/device/face_venv/bin/python3.11
# -*- coding: utf-8 -*-
"""
铁牛NAS Zero1 Pro 灯控引擎 (C3 灯语 + 硬盘灯呼吸)

灯语设计 (C3 = 心跳 + 快闪):
  空闲(在线)   : 每 3 秒短亮 60ms（在线心跳） 或 无级"呼吸"（可选，见 idle_mode）
  读写活动     : 80ms 亮/灭 密集快闪, 活动停止后继续 0.9 秒
  掉盘告警     : 500ms 亮/灭 慢闪  —— 与活动快闪明显区分
  夜间         : 按定时表熄灭, 严重告警可破例(可配)

优先级（重要）:
  告警  >  活动  >  空闲灯语(呼吸 / 心跳)
  任何 IO 一进来立刻打断呼吸变快闪，IO 停 0.9 秒后回到呼吸，
  于是"哪块盘在忙"一眼可见：忙的在闪，闲的在呼吸。

呼吸的三种错开方式 (disk.breath_mode):
  sync   三盘同相 —— 一起亮一起暗，整齐
  wave   相邻盘错开 breath_step_ms（默认 220ms）—— 追光感
  phase  均匀铺满一个周期（0, T/3, 2T/3）—— 此起彼伏

硬件路径 (实测):
  硬盘灯 = SATA 控制器 0000:01:00.0 BAR0 的 LED 寄存器
           槽位 n: 0x1d00+n (0x00=亮/0xff=灭), 0x1d08+n 取反,
                   0x1d10+n / 0x1d18+n 为第二组(实测与第一组无可见差异)
           实测单次写 0.12us、带 flush 0.37us，因此在 LedMod 里用 2ms 一跳的
           delta-sigma 调制做无级调光（约 250Hz 抖动，人眼完全融合）。
           注意：寄存器只有通/断两态，没有模拟调光——呼吸是"平均亮度"。
  电源灯 = Super I/O 端口 0xA01 的 bit1 (0=亮 1=灭), 直接写 /dev/port
           (superio_ctrl led 0|1 就是对这一位做 |=0x02 / &=~0x02;
            直写单次约 5us, 比 fork superio_ctrl 的 0.9ms 快两个数量级)

配置 : /etc/nas-led.json
运行时: /var/run/nas-led.runtime.json  (临时熄灯 / 单槽演示 / 四槽自检)
状态 : /var/run/nas-led.status.json   (每秒刷新, 供网页面板读取)

用法:
  led_engine.py                前台运行(由 systemd 拉起)
  led_engine.py --stop-once    只把四槽灯全部熄灭后退出(ExecStop 用)
"""

import json
import math
import mmap
import os
import signal
import subprocess
import sys
import threading
import time

RES = "/sys/bus/pci/devices/0000:01:00.0/resource0"
LED_END = 0x1d40
CONF = os.environ.get("LED_CONFIG", "/etc/nas-led.json")
RUNTIME = os.environ.get("LED_RUNTIME", "/var/run/nas-led.runtime.json")
STATUS = os.environ.get("LED_STATUS", "/var/run/nas-led.status.json")
SUPERIO = "/usr/bin/superio_ctrl"
SERVICE = "nas-led-engine"

DEFAULTS = {
    "engine": {"tick_ms": 20, "sample_ms": 200,
               "flicker_hz": 1000},  # 调制开关事件频率下限(Hz)。默认 1000：
                                     # 250Hz 虽高于人眼融合频率，但眼睛扫视时小颗高对比 LED
                                     # 会拖出点状重影（幻影阵列），有人到上千 Hz 才看不出。
                                     # 可调范围 60~2000（实测 2000 约占 32% 单核）
    "power": {
        "mode": "on",          # on | off | breath
        "breath_ms": 3000,     # 呼吸一个完整周期(ms)
        "breath_min": 0,       # 最暗亮度(%)
        "breath_max": 100,     # 最亮亮度(%)
    },
    "disk": {
        "enabled": True,
        "slots": ["sda", "sdb", "sdc", ""],
        "idle_mode": "heartbeat",   # heartbeat(心跳) | on(长亮) | breath(呼吸，已退役，仅兼容保留)
        "switch": "on",             # 硬盘灯总开关: on(跟随系统) | off(熄灭，掉盘告警仍闪)
        "breath_mode": "sync",      # sync(同步) | wave(波浪) | phase(错落)
        "breath_ms": 4000,          # 呼吸一个完整周期(ms)
        "breath_min": 30,           # 呼吸最暗亮度(%)  0 = 全黑(会更像"闪")
        "breath_step_ms": 220,      # wave 模式相邻盘的相位差(ms)
        "heartbeat_ms": 3000,
        "heartbeat_pulse_ms": 60,
        "activity_blink_ms": 80,
        "activity_hold_ms": 900,
        "alarm_blink_ms": 500,
        "activity_threshold_kb": 64,
    },
    "night": {
        "power_mode": "off",
        "power_heartbeat_ms": 10000,
        "power_heartbeat_pulse_ms": 80,
    },
    "schedule": {
        "power": {"enabled": False, "windows": []},
        "disk": {"enabled": False, "windows": []},
    },
    "alarm": {"night_exception": True},
}


# ---------------------------------------------------------------- 小工具

def log(msg):
    sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


def set_thread_realtime(name):
    """把当前线程提为实时调度（SCHED_RR，rtprio=1）。

    为什么必须做：调制线程一旦被普通负载饿住几百毫秒，寄存器就冻在
    上一状态，灯会打出肉眼可见的 ~0.5~1 秒方波（用户拍视频实测）。
    SCHED_RR 只影响本线程的 0.25~4ms 小切片，且总是马上睡眠，
    不会饿死系统里的其它任务。失败（无权限/不支持）就降级为普通调度。
    """
    try:
        os.sched_setscheduler(0, os.SCHED_RR, os.sched_param(1))
        log("%s 线程已提为 SCHED_RR(1)" % name)
    except Exception as e:
        try:
            os.nice(-10)
            log("%s 线程 SCHED_RR 不可用(%s)，降级 nice=-10" % (name, e))
        except Exception as e2:
            log("%s 线程提权失败: %s / %s" % (name, e, e2))


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def atomic_write_json(path, obj):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log("写 %s 失败: %s" % (path, e))


def parse_windows(wins):
    """'22:00-07:00,13:00-14:00' -> [(1320, 420), (780, 840)]  单位=当日分钟"""
    out = []
    for w in wins or []:
        try:
            a, b = str(w).split("-", 1)
            sh, sm = [int(x) for x in a.strip().split(":")]
            eh, em = [int(x) for x in b.strip().split(":")]
        except Exception:
            continue
        s, e = sh * 60 + sm, eh * 60 + em
        if s == e:
            continue
        if not (0 <= s < 1440 and 0 <= e < 1440):
            continue        # 越界时间(如 25:00)会静默失效且极难排查，直接丢弃
        out.append((s, e))
    return out


def in_windows(minutes, parsed):
    for s, e in parsed:
        if s < e:
            if s <= minutes < e:
                return True
        else:  # 跨午夜
            if minutes >= s or minutes < e:
                return True
    return False


def read_diskstats():
    """{dev: (读扇区, 写扇区)}"""
    out = {}
    try:
        with open("/proc/diskstats", "r") as f:
            for line in f:
                p = line.split()
                if len(p) < 10:
                    continue
                try:
                    out[p[2]] = (int(p[5]), int(p[9]))
                except ValueError:
                    continue
    except Exception:
        pass
    return out


def dev_size_gb(dev):
    try:
        with open("/sys/block/%s/size" % dev, "r") as f:
            return int(round(int(f.read().strip()) * 512 / 1000.0 ** 3))
    except Exception:
        return 0


def svc_state(which):
    try:
        p = subprocess.run(["systemctl", which, SERVICE],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=5)
        return (p.stdout or b"").decode().strip() or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- 硬件

class LedHw(object):
    """SATA 控制器 LED 寄存器（mmap BAR0）

    寄存器是"通/断"两态：0x00 亮 / 0xff 灭，0x1d08+n 与 0x1d10+n 取反。
    所以亮度只能靠 PWM（LedMod 里做 delta-sigma 调制）。
    """

    def __init__(self):
        self.fd = None
        self.m = None
        self.dirty = False
        self.next_try = 0

    def open(self):
        size = os.stat(RES).st_size
        length = min(size, 0x8000) // 4096 * 4096
        if length < LED_END:
            raise RuntimeError("resource0 仅 %d 字节, 访问不到 0x%04x" % (size, LED_END - 1))
        self.fd = os.open(RES, os.O_RDWR | os.O_SYNC)
        self.m = mmap.mmap(self.fd, length, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE)
        log("LED 寄存器已映射: resource0=%d 字节, mmap=%d" % (size, length))

    def ensure(self, now):
        if self.m is not None:
            return True
        if now < self.next_try:
            return False
        self.next_try = now + 5.0
        try:
            self.open()
            return True
        except Exception as e:
            log("映射 LED 寄存器失败(5 秒后重试): %s" % e)
            self.m = None
            return False

    def write_raw(self, slot, val):
        """val: 0x00=亮 / 0xff=灭，中间值留给"亮度分级"探测用"""
        if self.m is None or not (0 <= slot < 8):
            return
        v = val & 0xff
        inv = 0xff - v
        a, b, c, d = 0x1d00 + slot, 0x1d08 + slot, 0x1d10 + slot, 0x1d18 + slot
        self.m[a], self.m[b], self.m[c], self.m[d] = v, inv, v, inv
        self.dirty = True

    def write_on(self, slot, on):
        self.write_raw(slot, 0x00 if on else 0xff)

    def flush(self):
        if self.dirty and self.m is not None:
            try:
                self.m.flush()
            except Exception:
                pass
            self.dirty = False

    def all_off(self):
        for s in range(8):
            self.write_raw(s, 0xff)
        self.flush()


class LedMod(object):
    """硬盘灯调制器：把"灯语"变成 SATA 寄存器上的通断

    为什么单独开一个线程：
      引擎主循环 20ms 一跳，直接拿它做 PWM 只有 50Hz，低亮度时肉眼能看到频闪。
      这里 2ms 一跳（约 250Hz 抖动），人眼完全融合成连续亮度，
      于是"呼吸"能做到无级，"心跳脉冲"也能精确到 2ms 而不是 20ms。

    spec 是一个 dict（每槽一个，由引擎在锁内整体替换）:
      kind    on | off | blink | pulse | breath | raw
      t0      相位基准时刻（秒）
      period  周期（秒）; blink 是"亮+灭"一轮, pulse 是脉冲间隔, breath 是完整呼吸周期
      pulse   pulse 模式的亮灯时长（秒）
      lo      breath 模式的最暗亮度 0~1
      raw     raw 模式的寄存器字节（探测亮度分级用）
    """

    # 调制帧长必须自适应，不能只看"最暗亮度"：
    #   delta-sigma 的"开关事件重复频率" ≈ min(d, 1-d) / T。
    #   d=0.9 时 min(d,1-d)=0.1，6ms 帧下就是"亮 54ms、暗 6ms"的 16Hz 眨眼；
    #   d=0.43 时是 6ms 亮 / 6ms 暗的 82Hz 方波 —— 两者人眼都看得见，
    #   而 250ms 窗口平均出来仍是完美余弦（所以平均值验收会漏掉这个问题）。
    # 正确做法：T <= min(d, 1-d) / RATE_HZ，按当前占空比动态取。
    RATE_HZ = 250.0       # 开关事件频率下限(Hz)
    # 最快帧 0.25ms -> 事件频率上限 2000Hz。
    # 不要以为"250Hz 就一定融合"：眼睛扫视时 LED 会拖出一串点状重影（幻影阵列），
    # 对小颗高对比 LED 尤其明显，有人到上千 Hz 才看不出。所以留足上调空间。
    # 实测这个平台 sleep 到 0.25ms（4000 次/秒唤醒）只花 3.3% 单核，完全负担得起。
    TICK_MIN = 0.00025
    TICK_MAX = 0.004      # 最慢帧 4ms（只在把 RATE_HZ 调很低时才会碰到）
    STEADY_HI = 0.998     # 亮度高于此直接常亮，不做调制
    STEADY_LO = 0.002     # 低于此直接熄灭，不做调制
    IDLE_TICK = 0.01      # 没有槽位在调光时的巡检周期，精度够 60ms 心跳脉冲用

    def __init__(self, hw, anchor=None):
        self.hw = hw
        self.anchor = float(anchor if anchor is not None else time.time())
        self.spec = [{"kind": "off"} for _ in range(8)]
        self.acc = [0.0] * 8
        self.state = [None] * 8     # 最近一次真正写进寄存器的字节
        self.lv = [0.0] * 8         # 最近一次算出的目标亮度（供状态显示）
        self.tick = self.IDLE_TICK  # 当前实际用的帧长（供状态显示）
        self._lock = threading.Lock()
        self._stop = False
        self._th = None

    def start(self):
        self._th = threading.Thread(target=self._loop, name="ledmod")
        self._th.daemon = True
        self._th.start()

    def shutdown(self):
        self._stop = True
        if self._th is not None:
            try:
                self._th.join(timeout=0.6)
            except Exception:
                pass

    def set_specs(self, specs):
        with self._lock:
            for i, s in enumerate(specs[:8]):
                self.spec[i] = s

    def levels(self):
        return list(self.lv)

    def level(self, sp, now):
        """按灯语算出当前目标亮度 0.0 ~ 1.0（raw 由调用处直接取用）"""
        kind = sp.get("kind") or "off"
        if kind == "on":
            return 1.0
        if kind == "off":
            return 0.0
        per = float(sp.get("period") or 0.0)
        t0 = float(sp.get("t0") or now)
        if kind == "blink":
            if per <= 0.0:
                return 0.0
            return 1.0 if ((now - t0) % per) < per / 2.0 else 0.0
        if kind == "pulse":
            if per <= 0.0:
                return 0.0
            return 1.0 if ((now - t0) % per) < float(sp.get("pulse") or 0.0) else 0.0
        if kind == "breath":
            t = max(0.3, per)
            ph = ((now - t0) % t) / t
            w = 0.5 - 0.5 * math.cos(2.0 * math.pi * ph)   # 0->1->0 平滑往复
            lo = float(sp.get("lo") or 0.0)
            return lo + (1.0 - lo) * w
        return 0.0

    def frame(self, levels):
        """按当前各槽占空比反推调制帧长

        要让开关事件快到人眼看不见，就要 T <= min(d, 1-d) / RATE_HZ。
        取所有"正在调光"的槽位里最严的那个（在 d 和 1-d 之间取小，
        所以 d=0.95 时真正起作用的是那 5% 的暗缝，而不是 95% 的亮）。
        """
        tick = self.IDLE_TICK
        for lvl in levels:
            edge = lvl if lvl < 0.5 else (1.0 - lvl)
            t = edge / self.RATE_HZ
            if t < self.TICK_MIN:
                t = self.TICK_MIN
            elif t > self.TICK_MAX:
                t = self.TICK_MAX
            if t < tick:
                tick = t
        return tick

    def _loop(self):
        """外层护栏：调制线程万一抛异常会静默死掉、灯僵在最后状态，所以必须兜住"""
        set_thread_realtime("硬盘灯调制")
        last_t = time.perf_counter()
        while not self._stop:
            try:
                self._loop_inner()
                return
            except Exception as e:
                log("硬盘灯调制线程异常(1 秒后重试): %s" % e)
                time.sleep(1.0)

    def _loop_inner(self):
        dbg_checked = 0.0
        dbg_on = False
        dbg_fh = None
        dbg_tid = threading.get_ident()
        while not self._stop:
            t0 = time.perf_counter()
            # 停顿观测：上一轮到现在超过 50ms，说明线程被饿住过，
            # 这段时间寄存器冻在上一状态，肉眼看就是一次"闪"。记下来便于定位。
            _last = getattr(self, "_last_pc", None)
            if _last is not None and t0 - _last > 0.05:
                log("调制停顿 %d ms（灯在上一状态冻了这么久）"
                    % int((t0 - _last) * 1000))
            self._last_pc = t0
            with self._lock:
                specs = list(self.spec)
            now = time.time()
            vals = [0xff] * len(specs)
            # 诊断：调试模式下每 0.5s 把 spec 快照写进调试日志
            if dbg_on and dbg_fh is not None and int(t0 * 2) != getattr(self, "_dbg_spec_mark", -1):
                self._dbg_spec_mark = int(t0 * 2)
                try:
                    s0 = specs[0] if specs else {}
                    dbg_fh.write("SPEC %.3f kind=%s t0=%.3f period=%.3f lo=%.3f now=%.3f\n"
                                 % (t0, s0.get("kind"), float(s0.get("t0") or 0),
                                    float(s0.get("period") or 0), float(s0.get("lo") or 0), now))
                except Exception:
                    pass
            # 1) 算出每个槽位的目标亮度，并按"调制签名"分组
            groups = {}
            active = []
            for i, sp in enumerate(specs):
                kind = sp.get("kind") or "off"
                if kind == "raw":
                    vals[i] = int(sp.get("raw") or 0) & 0xff
                    self.lv[i] = 1.0 - vals[i] / 255.0
                    continue
                lvl = self.level(sp, now)
                self.lv[i] = lvl
                if kind == "breath":
                    # 贴近全亮/全灭时直接给稳态：省 CPU，也免掉"偶尔暗一帧"的抖动
                    if lvl >= self.STEADY_HI:
                        vals[i] = 0x00
                        self.acc[i] = 0.0
                        continue
                    if lvl <= self.STEADY_LO:
                        vals[i] = 0xff
                        self.acc[i] = 0.0
                        continue
                    active.append(lvl)
                    # 参数完全一致的呼吸槽位属于同一组。
                    # 关键：一组每轮只能推进一次累加器，然后组内共用同一个结果。
                    # 若每个槽位各推进一次（共享累加器但循环里逐槽累加），
                    # 累加器会被推进 N 次，三槽各自读到不同阈值 —— 波形就错开了。
                    sig = ("breath", round(lvl, 6),
                           round(float(sp.get("period") or 0.0), 6),
                           round(float(sp.get("t0") or 0.0), 6),
                           round(float(sp.get("lo") or 0.0), 6))
                else:
                    sig = (i,)      # 其它灯语（快闪/心跳/常亮）本就确定，各槽独立即可
                g = groups.get(sig)
                if g is None:
                    groups[sig] = {"lvl": lvl, "slots": [i]}
                else:
                    g["slots"].append(i)
            # 2) 帧长按当前占空比自适应（必须在调制之前定下来）
            tick = self.frame(active)
            self.tick = tick
            # 3) 每个签名推进一次累加器，组内共用结果 -> 三灯 PWM 逐位一致
            for g in groups.values():
                sl = g["slots"]
                acc = self.acc[sl[0]] + g["lvl"]
                if acc >= 1.0:
                    acc -= 1.0
                    val = 0x00
                else:
                    val = 0xff
                for i in sl:
                    self.acc[i] = acc
                    vals[i] = val
            # 4) 写入
            if dbg_on and dbg_fh is not None and self._dbg_spec_mark == int(t0 * 2):
                try:
                    dbg_fh.write("LVL %.3f %s\n"
                                 % (t0, " ".join("%.4f" % g["lvl"] for g in groups.values())))
                except Exception:
                    pass
            dirty = False
            for i in range(len(specs)):
                if self.state[i] != vals[i]:
                    self.hw.write_raw(i, vals[i])
                    self.state[i] = vals[i]
                    dirty = True
            if dirty:
                self.hw.flush()
            # 调试：存在 /tmp/nas-led-debug 时，把每一轮的实际写出记下来
            if t0 - dbg_checked > 1.0:
                dbg_checked = t0
                on = os.path.exists("/tmp/nas-led-debug")
                if on and not dbg_on:
                    try:
                        dbg_fh = open("/tmp/nas-led-mod.log", "w")
                    except Exception:
                        dbg_fh = None
                elif dbg_on and not on and dbg_fh is not None:
                    dbg_fh.close()
                    dbg_fh = None
                dbg_on = on
            if dbg_on and dbg_fh is not None and dirty:
                try:
                    dbg_fh.write("%.6f %s %s tid=%d\n"
                                 % (now, "".join("%02x" % v for v in vals),
                                    "".join("%02x" % s for s in self.state), dbg_tid))
                    dbg_fh.flush()
                except Exception:
                    pass
            d = tick - (time.perf_counter() - t0)
            if d > 0:
                time.sleep(d)


class PowerLed(object):
    """电源灯：直接写 Super I/O 的 PWR_LED 位

    硬件路径（由 /usr/bin/superio_ctrl 反汇编 + 实测得出）：
      端口 0xA01 的 bit1 —— bit1=0 亮 / bit1=1 灭（低电平点亮）。
      superio_ctrl led 0/1 做的就是 |= 0x02 / &= ~0x02，其它位保持不变。

    为什么不直接 fork superio_ctrl：
      它每次 fork+exec+iopl 约 0.9ms，要做呼吸 PWM 既慢又抖；
      直写 /dev/port 单次约 5us，可用 delta-sigma 调制到 ~500Hz 抖动，
      人眼完全融合成连续亮度，从而实现真正的无级"呼吸"。

    模式:
      on     常亮
      off    熄灭
      breath 呼吸（亮度按余弦曲线在 breath_min..breath_max 之间往复）
      pulse  极短脉冲（夜间"极简心跳"用）
    """

    PORT = 0xA01
    BIT = 0x02
    RATE_HZ = 250.0       # 开关事件频率下限(Hz)：T <= min(d,1-d)/RATE_HZ
    TICK_MIN = 0.00025    # 最快帧 0.25ms（/dev/port 单次写 5~7us，4000 次/秒约 5% 单核）
    TICK_MAX = 0.004      # 最慢帧 4ms
    STEADY_HI = 0.998     # 亮度高于此直接常亮，不做调制
    STEADY_LO = 0.002     # 低于此直接熄灭
    PULSE_TICK = 0.01     # 脉冲模式(夜间心跳)的帧长，足够卡准 80ms 脉冲
    IDLE_TICK = 0.05      # 常亮/熄灭时的巡检周期，稳态几乎不耗 CPU

    def __init__(self):
        self.fd = None
        self.base = 0x00
        self.ok = False
        self.mode = "on"
        self.breath_ms = 3000.0
        self.lo = 0.0
        self.hi = 1.0
        self.pulse_period_ms = 10000.0
        self.pulse_ms = 80.0
        self.tick = self.IDLE_TICK
        self._t0 = time.time()
        self._acc = 0.0
        self._last = None
        self._fallback = None
        self._lock = threading.Lock()
        self._stop = False
        self._th = None

    # -- 端口 ------------------------------------------------------
    def open_port(self):
        try:
            fd = os.open("/dev/port", os.O_RDWR)
            os.lseek(fd, self.PORT, os.SEEK_SET)
            self.base = os.read(fd, 1)[0]
            self.fd = fd
            self.ok = True
            log("电源灯端口 0x%04X 已打开, 初值 0x%02X (bit1=%d)"
                % (self.PORT, self.base, (self.base >> 1) & 1))
            return True
        except Exception as e:
            self.fd = None
            self.ok = False
            log("打开 /dev/port 失败, 电源灯回退 superio_ctrl(呼吸将降级为常亮): %s" % e)
            return False

    def _write(self, lit):
        """lit=True -> bit1=0 -> 灯亮"""
        if self.fd is not None:
            v = (self.base & ~self.BIT) | (0x00 if lit else self.BIT)
            try:
                os.lseek(self.fd, self.PORT, os.SEEK_SET)
                os.write(self.fd, bytes([v]))
                return
            except Exception as e:
                log("写电源灯端口失败, 回退 superio_ctrl: %s" % e)
                self.fd = None
                self.ok = False
        if self._fallback == lit:
            return
        self._fallback = lit
        try:
            subprocess.run([SUPERIO, "led", "1" if lit else "0"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=3)
        except Exception:
            pass

    # -- 生命周期 --------------------------------------------------
    def start(self):
        self.open_port()
        self._t0 = time.time()
        self._th = threading.Thread(target=self._loop, name="pwrled")
        self._th.daemon = True
        self._th.start()

    def shutdown(self):
        self._stop = True
        if self._th is not None:
            try:
                self._th.join(timeout=0.6)
            except Exception:
                pass
        # 退出时按语义落定，避免把灯停在某个抖动相位上
        self._write(self.mode != "off")

    # -- 状态 ------------------------------------------------------
    def set_mode(self, mode, **kw):
        with self._lock:
            same = (mode == self.mode)
            if same:
                for k, v in kw.items():
                    if abs(float(getattr(self, k, 0.0)) - float(v)) > 1e-9:
                        same = False
                        break
            if same:
                return
            self.mode = mode
            for k, v in kw.items():
                setattr(self, k, float(v))

    def semantic_on(self):
        return self.mode != "off"

    def level(self, now):
        """当前目标亮度 0.0 ~ 1.0"""
        m = self.mode
        if not self.ok:
            # 端口不可用时呼吸无意义，退化为常亮；但仍然尊重"熄灭"语义
            return 0.0 if m == "off" else 1.0
        if m == "on":
            return 1.0
        if m == "off":
            return 0.0
        if m == "pulse":
            per = max(0.05, self.pulse_period_ms / 1000.0)
            return 1.0 if ((now - self._t0) % per) < (self.pulse_ms / 1000.0) else 0.0
        if m == "breath":
            t = max(0.3, self.breath_ms / 1000.0)
            ph = ((now - self._t0) % t) / t
            wave = 0.5 - 0.5 * math.cos(2.0 * math.pi * ph)   # 0->1->0 平滑往复
            return self.lo + (self.hi - self.lo) * wave
        return 1.0

    def _loop(self):
        """外层护栏：调制线程抛异常会静默死掉、灯僵在最后状态，必须兜住"""
        set_thread_realtime("电源灯调制")
        while not self._stop:
            try:
                self._loop_inner()
                return
            except Exception as e:
                log("电源灯调制线程异常(1 秒后重试): %s" % e)
                time.sleep(1.0)

    def _loop_inner(self):
        while not self._stop:
            t = time.time()
            with self._lock:
                lvl = self.level(t)
                mode = self.mode
            # delta-sigma 调制：平均亮度精确等于 lvl，开关事件频率 >= RATE_HZ
            if lvl >= self.STEADY_HI:
                self._acc = 0.0
                lit = True
            elif lvl <= self.STEADY_LO:
                self._acc = 0.0
                lit = False
            else:
                self._acc += lvl
                if self._acc >= 1.0:
                    self._acc -= 1.0
                    lit = True
                else:
                    lit = False
            if lit != self._last:
                self._write(lit)
                self._last = lit
            # 帧长同理：按 min(d,1-d) 反推，否则高亮度时会出现"偶尔暗一帧"的眨眼
            if (not self.ok) or mode in ("on", "off"):
                tick = self.IDLE_TICK
            elif mode == "pulse":
                tick = self.PULSE_TICK
            else:
                edge = lvl if lvl < 0.5 else (1.0 - lvl)
                tick = min(max(edge / self.RATE_HZ, self.TICK_MIN), self.TICK_MAX)
            self.tick = tick
            d = tick - (time.time() - t)
            if d > 0:
                time.sleep(d)


# ---------------------------------------------------------------- 引擎

class Engine(object):
    def __init__(self):
        # GIL 交接间隔默认 5ms，对 0.25ms 级调制太粗，压到 1ms
        # （实测最大内部抖动从 ~5ms 降到 ~2ms）
        sys.setswitchinterval(0.001)
        self.cfg = deep_merge(DEFAULTS, load_json(CONF, {}) or {})
        self.hw = LedHw()
        self.mod = None
        self.pw = PowerLed()
        self.t0 = time.time()
        self.running = True
        self.rt = load_json(RUNTIME, {}) or {}
        self.rt_mtime = -1
        self.slots = []
        self.last_sample = 0.0
        self.last_status = 0.0
        self.power_on = None
        self.enabled = "unknown"
        self.sync_slots()
        self.apply_tuning()

    def apply_tuning(self):
        """把"防闪频率"下发给两个调制器（类属性，改完立即生效）

        这个值决定开关事件的重复频率下限：低占空比时是"亮脉冲的频率"，
        高占空比时是"暗缝的频率"。取小了就会出现人眼能看见的频闪。
        """
        try:
            hz = float(self.cfg.get("engine", {}).get("flicker_hz") or 250)
        except Exception:
            hz = 250.0
        hz = min(max(hz, 60.0), 2000.0)
        LedMod.RATE_HZ = hz
        PowerLed.RATE_HZ = hz

    # -- 配置/槽位 -------------------------------------------------
    def sync_slots(self):
        devs = list(self.cfg["disk"].get("slots") or [])
        while len(devs) < 4:
            devs.append("")
        old = {s["slot"]: s for s in self.slots}
        self.slots = []
        ordn = 0
        for i, dev in enumerate(devs[:8]):
            st = old.get(i) or {}
            st["slot"] = i
            st["dev"] = (dev or "").strip()
            st["ord"] = ordn
            if st["dev"]:
                ordn += 1
            st.setdefault("mode", "")
            st.setdefault("mode_since", 0.0)
            st.setdefault("last_act", 0.0)
            st.setdefault("online", False)
            st.setdefault("rate", 0.0)
            st.setdefault("prev_r", None)
            st.setdefault("prev_w", None)
            st.setdefault("on", False)
            st.setdefault("level", 0.0)
            self.slots.append(st)
        self.used = max(1, ordn)

    def reload(self, now):
        self.cfg = deep_merge(DEFAULTS, load_json(CONF, {}) or {})
        self.sync_slots()
        self.apply_tuning()
        log("配置已重新加载")

    # -- 运行时 ----------------------------------------------------
    def poll_runtime(self):
        """运行时文件一有变化就重读，并顺带重载配置（省掉发信号）"""
        try:
            mt = os.stat(RUNTIME).st_mtime
        except Exception:
            mt = -1
        if mt != self.rt_mtime:
            first = self.rt_mtime < 0
            self.rt_mtime = mt
            self.rt = load_json(RUNTIME, {}) or {}
            if not first:
                self.cfg = deep_merge(DEFAULTS, load_json(CONF, {}) or {})
                self.sync_slots()
                self.apply_tuning()
                log("检测到运行时变更，配置已重新加载")

    # -- 采样 ------------------------------------------------------
    def sample(self, now):
        ds = read_diskstats()
        dt = (now - self.last_sample) if self.last_sample else 0.2
        dt = max(dt, 1e-6)
        thr_kb = float(self.cfg["disk"].get("activity_threshold_kb") or 0)
        for st in self.slots:
            dev = st["dev"]
            if not dev:
                st["online"] = False
                st["rate"] = 0.0
                continue
            online = os.path.exists("/sys/block/" + dev)
            st["online"] = online
            if not online:
                st["rate"] = 0.0
                st["prev_r"] = st["prev_w"] = None
                continue
            row = ds.get(dev)
            if row is None:
                continue
            r, w = row
            if st["prev_r"] is not None:
                dr = max(0, r - st["prev_r"])
                dw = max(0, w - st["prev_w"])
                st["rate"] = (dr + dw) * 512 / 1048576.0 / dt
                if (dr + dw) * 512 / 1024.0 > thr_kb:
                    st["last_act"] = now
            st["prev_r"], st["prev_w"] = r, w
        self.last_sample = now

    # -- 灯语 ------------------------------------------------------
    def breath_on(self):
        """空闲时用呼吸代替心跳？"""
        d = self.cfg["disk"]
        if not d.get("enabled", True):
            return False
        return str(d.get("idle_mode") or "heartbeat").lower() == "breath"

    # ---- 硬盘灯总开关：手动 on/off ----

    def _parse_hhmm(self, s, dflt_min=8 * 60):
        try:
            h, m = str(s).split(":")
            h, m = int(h), int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h * 60 + m
        except Exception:
            pass
        return dflt_min

    def _save_rt(self):
        try:
            tmp = "%s.tmp.%d" % (RUNTIME, os.getpid())
            with open(tmp, "w") as f:
                json.dump(self.rt, f, ensure_ascii=False)
            os.replace(tmp, RUNTIME)
        except Exception:
            pass

    def disk_switch_on(self, now):
        """硬盘灯有效开关：仅由配置 switch 决定（手动 on/off，重启不清）。"""
        d = self.cfg["disk"]
        return str(d.get("switch") or "on").lower() != "off"

    def breath_offset(self, st):
        """呼吸相位错开量（秒）—— sync / wave / phase 三种方式的全部差别"""
        d = self.cfg["disk"]
        m = str(d.get("breath_mode") or "sync").lower()
        if m == "wave":
            return st.get("ord", 0) * float(d.get("breath_step_ms", 220) or 0) / 1000.0
        if m == "phase":
            per = float(d.get("breath_ms", 4000) or 4000) / 1000.0
            return st.get("ord", 0) * per / max(1, getattr(self, "used", 1))
        return 0.0

    def decide(self, st, now, night_disk):
        if not self.cfg["disk"].get("enabled", True) or not st["dev"]:
            return "off"
        if not getattr(self, "disk_on", True):
            # 总开关关闭：硬盘灯全灭，但掉盘告警仍然可见（安全优先）
            return "off" if st["online"] else "alarm"
        if not st["online"]:
            if night_disk and not self.cfg["alarm"].get("night_exception", True):
                return "off"
            return "alarm"
        if night_disk:
            return "off"
        if now - st["last_act"] < float(self.cfg["disk"].get("activity_hold_ms", 900)) / 1000.0:
            return "activity"
        im = str(self.cfg["disk"].get("idle_mode") or "heartbeat").lower()
        if im in ("on", "solid"):
            return "solid"          # 长亮档：在线时常亮
        return "breath" if im == "breath" else "idle"

    def spec_for(self, st, mode, now):
        """灯语 -> 调制器规格。活动/告警/心跳的时序与旧版完全一致。"""
        d = self.cfg["disk"]
        if st["mode"] != mode:
            st["mode"] = mode
            st["mode_since"] = now
        if mode == "activity":
            return {"kind": "blink", "t0": st["mode_since"],
                    "period": 2.0 * float(d.get("activity_blink_ms", 80)) / 1000.0}
        if mode == "blink":
            return {"kind": "blink", "t0": st["mode_since"], "period": 0.16}
        if mode == "alarm":
            return {"kind": "blink", "t0": st["mode_since"],
                    "period": 2.0 * float(d.get("alarm_blink_ms", 500)) / 1000.0}
        if mode == "idle":
            return {"kind": "pulse", "t0": self.t0,
                    "period": float(d.get("heartbeat_ms", 3000)) / 1000.0,
                    "pulse": float(d.get("heartbeat_pulse_ms", 60)) / 1000.0}
        if mode == "breath":
            return {"kind": "breath", "t0": self.t0 + self.breath_offset(st),
                    "period": float(d.get("breath_ms", 4000) or 4000) / 1000.0,
                    "lo": max(0.0, min(0.95, float(d.get("breath_min", 30) or 0) / 100.0))}
        if mode == "levels":
            # 亮度分级探测：每 2 秒换一个原始寄存器值，用肉眼判断有没有模拟调光
            k = int((now - st["mode_since"]) / 2.0) % 5
            return {"kind": "raw", "raw": (0x00, 0x40, 0x80, 0xc0, 0xff)[k]}
        if mode == "solid":
            return {"kind": "on"}
        return {"kind": "off"}

    def update(self, now):
        self.hw.ensure(now)

        tm = time.localtime(now)
        mins = tm.tm_hour * 60 + tm.tm_min
        sch = self.cfg["schedule"]
        mute = float(self.rt.get("mute_until") or 0) > now

        night_power = mute or (
            sch["power"].get("enabled")
            and in_windows(mins, parse_windows(sch["power"].get("windows")))
        )
        night_disk = mute or (
            sch["disk"].get("enabled")
            and in_windows(mins, parse_windows(sch["disk"].get("windows")))
        )

        demo = self.rt.get("demo") or {}
        demo_ok = (float(demo.get("until") or 0) > now
                   and demo.get("mode")
                   and 0 <= int(demo.get("slot", -1)) < len(self.slots))
        sweep = self.rt.get("sweep") or {}
        sweep_on = float(sweep.get("until") or 0) > now

        specs = []
        self.disk_on = self.disk_switch_on(now)
        for i, st in enumerate(self.slots):
            mode = self.decide(st, now, night_disk)
            if demo_ok and int(demo["slot"]) == i:
                mode = demo["mode"]
            elif sweep_on:
                k = int((now - float(sweep.get("start") or now)) / 0.8)
                mode = "solid" if k == i else "off"
            specs.append(self.spec_for(st, mode, now))
        if self.mod is not None:
            self.mod.set_specs(specs)

        # 电源灯
        if night_power:
            if (not mute) and self.cfg["night"].get("power_mode") == "heartbeat":
                self.pw.set_mode("pulse",
                                 pulse_period_ms=self.cfg["night"].get("power_heartbeat_ms", 10000),
                                 pulse_ms=self.cfg["night"].get("power_heartbeat_pulse_ms", 80))
            else:
                self.pw.set_mode("off")
        else:
            p = self.cfg["power"]
            m = p.get("mode", "on")
            if m not in ("on", "off", "breath"):
                m = "on"
            self.pw.set_mode(m,
                             breath_ms=p.get("breath_ms", 3000),
                             lo=float(p.get("breath_min", 0) or 0) / 100.0,
                             hi=float(p.get("breath_max", 100) or 100) / 100.0)
        self.power_on = self.pw.semantic_on()

        self.night_power = night_power
        self.night_disk = night_disk
        self.mute = mute

    # -- 状态文件 --------------------------------------------------
    def write_status(self, now):
        lv = self.mod.levels() if self.mod is not None else [0.0] * 8
        disks = []
        for st in self.slots:
            st["level"] = lv[st["slot"]] if st["slot"] < len(lv) else 0.0
            st["on"] = st["level"] > 0.02
            disks.append({
                "dev": st["dev"],
                "slot": st["slot"],
                "gb": dev_size_gb(st["dev"]) if st["dev"] else 0,
                "online": bool(st["online"]),
                "mode": st["mode"],
                "state": "on" if st["on"] else "off",
                "level": round(st["level"], 3),
                "rate": round(st["rate"], 2),
            })
        mute_until = float(self.rt.get("mute_until") or 0)
        obj = {
            "ts": now,
            "ts_str": time.strftime("%H:%M:%S", time.localtime(now)),
            "uptime": int(now - self.t0),
            "pid": os.getpid(),
            "hw_ok": self.hw.m is not None,
            "enabled": self.enabled,
            "night": {
                "power": bool(getattr(self, "night_power", False)),
                "disk": bool(getattr(self, "night_disk", False)),
                "mute": bool(getattr(self, "mute", False)),
                "mute_until": mute_until,
                "mute_left": max(0, int((mute_until - now) / 60.0 + 0.999)) if mute_until > now else 0,
                "power_mode": self.cfg["night"].get("power_mode", "off"),
                "exception": bool(self.cfg["alarm"].get("night_exception", True)),
            },
            "power": {
                "mode": self.cfg["power"].get("mode", "on"),
                "state": "on" if self.power_on else "off",
                "breath_ms": self.cfg["power"].get("breath_ms", 3000),
                "breath_min": self.cfg["power"].get("breath_min", 0),
                "breath_max": self.cfg["power"].get("breath_max", 100),
                "port_ok": bool(self.pw.ok),
            },
            "disk": {
                "enabled": bool(self.cfg["disk"].get("enabled", True)),
                "switch": "on" if getattr(self, "disk_on", True) else "off",
                "switch_cfg": str(self.cfg["disk"].get("switch") or "on"),
                "idle_mode": self.cfg["disk"].get("idle_mode", "heartbeat"),
                "breath_mode": self.cfg["disk"].get("breath_mode", "sync"),
                "breath_ms": self.cfg["disk"].get("breath_ms", 4000),
                "breath_min": self.cfg["disk"].get("breath_min", 30),
                "breath_step_ms": self.cfg["disk"].get("breath_step_ms", 220),
                "heartbeat_ms": self.cfg["disk"].get("heartbeat_ms"),
                "heartbeat_pulse_ms": self.cfg["disk"].get("heartbeat_pulse_ms"),
                "activity_blink_ms": self.cfg["disk"].get("activity_blink_ms"),
                "activity_hold_ms": self.cfg["disk"].get("activity_hold_ms"),
                "alarm_blink_ms": self.cfg["disk"].get("alarm_blink_ms"),
                "threshold_kb": self.cfg["disk"].get("activity_threshold_kb"),
                "mod_tick_ms": round((self.mod.tick if self.mod is not None else 0) * 1000, 2),
                "flicker_hz": round(LedMod.RATE_HZ, 1),
                # 呼吸相位（当前周期内已走过的毫秒数）+ 生成的时刻，供面板预览与真实灯同步
                "breath_phase_ms": round(
                    ((now - self.t0) % (float(self.cfg["disk"].get("breath_ms", 4000) or 4000) / 1000.0))
                    * 1000.0, 1),
                "phase_at_ms": int(now * 1000),
            },
            "night_cfg": {
                "power_heartbeat_ms": self.cfg["night"].get("power_heartbeat_ms"),
                "power_heartbeat_pulse_ms": self.cfg["night"].get("power_heartbeat_pulse_ms"),
            },
            "schedule": {
                "power": dict(self.cfg["schedule"]["power"]),
                "disk": dict(self.cfg["schedule"]["disk"]),
            },
            "disks": disks,
        }
        atomic_write_json(STATUS, obj)
        self.last_status = now

    # -- 主循环 ----------------------------------------------------
    def run(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_stop)
        signal.signal(signal.SIGHUP, self._on_hup)
        self.enabled = svc_state("is-enabled")
        self.poll_runtime()
        self.pw.start()
        self.mod = LedMod(self.hw, anchor=self.t0)
        self.mod.start()
        log("灯控引擎启动, 槽位映射: %s (空闲灯语=%s/%s)"
            % (self.cfg["disk"].get("slots"),
               self.cfg["disk"].get("idle_mode"),
               self.cfg["disk"].get("breath_mode")))
        tick = float(self.cfg["engine"].get("tick_ms", 20)) / 1000.0
        sample = float(self.cfg["engine"].get("sample_ms", 200)) / 1000.0
        while self.running:
            now = time.time()
            self.poll_runtime()
            if now - self.last_sample >= sample:
                self.sample(now)
            self.update(now)
            if now - self.last_status >= 1.0:
                self.write_status(now)
            time.sleep(tick)
        self.cleanup()

    def _on_stop(self, *_):
        self.running = False

    def _on_hup(self, *_):
        self.cfg = deep_merge(DEFAULTS, load_json(CONF, {}) or {})
        self.sync_slots()
        log("收到 SIGHUP, 配置已重新加载")

    def cleanup(self):
        log("停止中: 熄灭全部硬盘灯, 电源灯落定")
        if self.mod is not None:
            try:
                self.mod.shutdown()          # 先停调制线程, 否则它会把灯又点亮
            except Exception:
                pass
        try:
            self.pw.shutdown()
        except Exception:
            pass
        try:
            self.hw.ensure(time.time())
            self.hw.all_off()
        except Exception:
            pass


def stop_once():
    hw = LedHw()
    try:
        hw.open()
        hw.all_off()
    except Exception as e:
        log("熄灭失败: %s" % e)
        return 1
    return 0


def main():
    if "--stop-once" in sys.argv:
        return stop_once()
    Engine().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
