#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
铁牛NAS Zero1 Pro 灯控面板 v3（本地网页界面）
运行后浏览器打开 http://<NAS地址>:8977

与同容器内的 ledctl / led_engine 引擎协作控制:
  电源灯 -> Super I/O 端口 0xA01 bit1 (常亮 / 熄灭 / 呼吸)
  硬盘灯 -> SATA 控制器 BAR0 LED 寄存器 (自带引擎直接驱动)
"""
import base64
import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 容器内 ledctl 是 /opt/app 下的 Python 脚本（只读挂载、无执行位，不能直接执行）。
# 宿主机部署可用环境变量 LEDCTL 覆盖为宿主机上的 ledctl 路径。
LEDCTL = os.environ.get("LEDCTL") or "python3 /opt/app/ledctl.py"
SVC = "nas-led-engine"
PORT = 8977

WIN_RE = re.compile(r"^[0-9:\-,;\s]*$")
WIN_ITEM_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


def bad_window(wins):
    """逐个校验时间段，返回错误文案或 None。

    面板原来只查字符集，"25:00-30:00" 这类能过校验 → 写进配置 → 引擎
    parse_windows 静默跳过 → 永远不生效，用户只看到"设了没用"。
    """
    for w in (wins or "").replace(";", ",").split(","):
        w = w.strip()
        if not w:
            continue
        m = WIN_ITEM_RE.match(w)
        if not m:
            return "时间段 %s 格式不对（应形如 22:00-07:00）" % w
        h1, m1, h2, m2 = [int(x) for x in m.groups()]
        if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
            return "时间段 %s 超出范围（小时 00-23，分钟 00-59）" % w
        if h1 * 60 + m1 == h2 * 60 + m2:
            return "时间段 %s 起止相同，不会生效" % w
    return None


def remote(cmd, timeout=45):
    args = ["/bin/bash", "-c", cmd]
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        if p.returncode != 0 and err.strip():
            return False, (out + "\n" + err).strip()
        return True, out.strip()
    except subprocess.TimeoutExpired:
        return False, "本地命令超时"
    except FileNotFoundError:
        return False, "找不到 bash"


STATUS_CMD = (
    'cat "${LED_STATUS:-/var/run/nas-led.status.json}" 2>/dev/null; echo;'
    'echo active; echo enabled'
)


def parse_status(text):
    lines = [l for l in text.splitlines()]
    if not lines:
        return None
    try:
        d = json.loads(lines[0])
    except Exception:
        return None
    d["service"] = {
        "active": lines[1].strip() if len(lines) > 1 else "unknown",
        "enabled": lines[2].strip() if len(lines) > 2 else "unknown",
    }
    return d


FAVICON_B64 = "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"

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zero1 Pro 灯控面板</title>
<link rel="icon" type="image/png" href="data:image/png;base64,__FAVICON_B64__">
<style>
  :root{
    --bg:#eef1f6;
    --bg2:#f7f9fc;
    --card:rgba(255,255,255,.86);
    --line:#e2e8f2;
    --line2:#eef2f8;
    --text:#141c28;
    --muted:#69778e;
    --muted2:#93a0b4;
    --accent:#3b6ef6;
    --accent2:#7aa2ff;
    --accent-soft:#eef3ff;
    --red:#e5484d;
    --green:#0f9d58;
    --green-soft:#e8f7ee;
    --amber:#e79a13;
    --amber-soft:#fdf4e4;
    --shadow:0 1px 2px rgba(20,28,40,.04), 0 8px 24px -8px rgba(20,28,40,.12);
    --shadow-lg:0 2px 6px rgba(20,28,40,.05), 0 20px 40px -16px rgba(20,28,40,.18);
    --r:16px;
  }

  /* ---------- dark theme ---------- */
  [data-theme="dark"]{
    --bg:#0d141f; --bg2:#0b111b; --card:rgba(23,32,46,.82);
    --line:#27313f; --line2:#1b2433;
    --text:#e7edf6; --muted:#94a3ba; --muted2:#67768d;
    --accent:#5b86f7; --accent2:#86a8ff; --accent-soft:#15233c;
    --green:#19b56a; --green-soft:#0f2a1d; --amber:#f0ab2e; --amber-soft:#2a2113;
    --shadow:0 1px 2px rgba(0,0,0,.18), 0 8px 24px -8px rgba(0,0,0,.42);
    --shadow-lg:0 2px 6px rgba(0,0,0,.22), 0 20px 40px -16px rgba(0,0,0,.55);
  }
  [data-theme="dark"] body{
    background:
      radial-gradient(1100px 460px at 12% -8%, #15203a 0%, rgba(21,32,58,0) 62%),
      radial-gradient(900px 420px at 96% 4%, #10302a 0%, rgba(16,48,42,0) 58%),
      linear-gradient(180deg,var(--bg2),var(--bg) 58%);
  }
  [data-theme="dark"] .card{background:var(--card)}
  [data-theme="dark"] .pill{background:var(--card);border-color:var(--line)}
  [data-theme="dark"] .kbd{background:#16202e;border-color:var(--line);color:var(--muted)}
  [data-theme="dark"] button{background:#172231;border-color:var(--line);color:var(--text)}
  [data-theme="dark"] button:hover{background:#1e2b3d;border-color:#33425a}
  [data-theme="dark"] button.primary{background:linear-gradient(180deg,#4a7bf8,#3766eb);border-color:#3766eb;color:#fff}
  [data-theme="dark"] button.primary:hover{background:linear-gradient(180deg,#4074f6,#2f5fe0)}
  [data-theme="dark"] button.warn{color:#ff9b90;border-color:#5a2c28;background:#241615}
  [data-theme="dark"] input[type=number],[data-theme="dark"] input[type=text]{
    background:#0f1825;color:var(--text);border-color:var(--line)}
  [data-theme="dark"] .seg{background:#131c29;border-color:var(--line)}
  [data-theme="dark"] .seg button{color:#9fb0c8}
  [data-theme="dark"] .seg button:hover{background:#1e2b3d}
  [data-theme="dark"] .slot{background:linear-gradient(180deg,#16202e,#111a26);border-color:var(--line)}
  [data-theme="dark"] .led.off,[data-theme="dark"] .led.idle.off{background:#2b3645}
  [data-theme="dark"] .disks tbody tr:hover{background:#16202e}
  [data-theme="dark"] .prefill button{background:#131c29;color:var(--muted)}
  [data-theme="dark"] .prefill button:hover{color:var(--accent);border-color:#33425a;background:#1a2636}

  /* ---------- theme toggle (top-right) ---------- */
  .themebtn{display:inline-flex;align-items:center;gap:7px;padding:7px 13px;border-radius:10px;font-weight:540}
  .theme-ico{display:inline-flex;width:16px;height:16px;align-items:center;justify-content:center}
  .theme-ico svg{width:16px;height:16px}
  .theme-txt{font-size:12.5px;font-weight:520}

  /* ---------- responsive (phone) ---------- */
  @media(max-width:600px){
    html,body{overflow-x:hidden;max-width:100%}
    .wrap{padding:12px 12px 50px}
    .card{padding:13px}
    .stage{padding:14px;gap:14px}
    .stageName{min-width:0;gap:11px}
    header{gap:8px;margin-bottom:16px}
    .logo{width:38px;height:38px;border-radius:11px}
    h1{font-size:17px}
    .hdright{width:100%;flex-wrap:nowrap;gap:6px;margin-left:0}
    .pill{padding:6px 10px;font-size:11.5px;gap:5px;min-width:0;flex:1 1 auto;justify-content:center}
    .iconbtn{width:32px;height:32px;flex:0 0 auto}
    .theme-txt{display:none}
    .themebtn{padding:8px;flex:0 0 auto}
    .slot{min-width:0}
    .nextInfo{font-size:11.5px;min-width:0}
    .field label,.pgrid .field label{min-width:84px}
    input[type=number]{width:84px}
    .cols2{gap:14px}
    .legend{grid-template-columns:1fr}
    .disks{display:block;overflow-x:auto}
    .disks th,.disks td{padding:7px 5px;font-size:12px;white-space:nowrap}
    #demoRow{grid-template-columns:1fr}
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  /* 桌面端整体缩放（手机端不受影响），对齐 fanctl 视觉比例 */
  @media(min-width:601px){body{zoom:1.25}}
  body{
    margin:0;color:var(--text);
    background:
      radial-gradient(1100px 460px at 12% -8%, #e5ecff 0%, rgba(229,236,255,0) 62%),
      radial-gradient(900px 420px at 96% 4%, #e7f6f1 0%, rgba(231,246,241,0) 58%),
      linear-gradient(180deg,var(--bg2),var(--bg) 58%);
    background-attachment:fixed;
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI Variable","Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:1180px;margin:0 auto;padding:30px 22px 70px}

  /* ---------- header ---------- */
  header{display:flex;align-items:center;gap:15px;flex-wrap:wrap;margin-bottom:22px}
  .logo{
    width:46px;height:46px;border-radius:14px;flex:0 0 auto;
    background:linear-gradient(145deg,#3b6ef6,#7aa2ff 55%,#9ec4ff);
    display:flex;align-items:center;justify-content:center;
    box-shadow:0 8px 20px -6px rgba(59,110,246,.55), inset 0 1px 0 rgba(255,255,255,.5);
  }
  .logo i{width:15px;height:15px;border-radius:50%;background:#fff;display:block;
    box-shadow:0 0 12px 4px rgba(255,255,255,.75)}
  h1{font-size:19.5px;margin:0;font-weight:680;letter-spacing:.1px;
    display:flex;align-items:center;gap:9px}
  .sub{display:none}
  .hdright{margin-left:auto;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
  .pill{display:flex;align-items:center;gap:8px;background:var(--card);backdrop-filter:blur(8px);
    border:1px solid var(--line);border-radius:999px;padding:8px 15px;font-size:12.5px;
    box-shadow:var(--shadow);font-weight:520}
  .pill.badge{border-color:#f3e0bb;background:var(--amber-soft);color:#8a5c06}
  .pill.badge.hb{border-color:#d9e3ff;background:var(--accent-soft);color:#2a4fbe}
  .dot{width:8px;height:8px;border-radius:50%;background:#c4ccda;flex:0 0 auto}
  .dot.on{background:var(--green);box-shadow:0 0 0 3.5px rgba(15,157,88,.16)}
  .dot.off{background:#c4ccda}
  .dot.busy{background:var(--amber);animation:pulseDot 1.1s infinite}
  @keyframes pulseDot{0%,100%{opacity:1}50%{opacity:.35}}
  .iconbtn{width:34px;height:34px;padding:0;justify-content:center;border-radius:10px}

  /* ---------- layout ---------- */
  .grid{display:grid;gap:17px;margin-bottom:17px}
  .g2{grid-template-columns:1fr 1fr}
  @media(max-width:900px){.g2{grid-template-columns:1fr}}
  .pgrid{display:grid;grid-template-columns:1fr 1fr;column-gap:22px}
  .pgrid .field{margin-bottom:8px}
  .pgrid .field label{min-width:96px}
  @media(max-width:560px){.pgrid{grid-template-columns:1fr}}
  .legend{display:grid;grid-template-columns:1fr 1fr;gap:8px 18px;margin-top:13px;
    font-size:12.5px;color:var(--muted);line-height:1.7}
  .legend>div{display:flex;align-items:center;gap:8px}
  .card{
    background:var(--card);backdrop-filter:blur(10px);
    border:1px solid var(--line);border-radius:var(--r);padding:20px;
    box-shadow:var(--shadow);transition:box-shadow .2s,transform .2s
  }
  .card:hover{box-shadow:var(--shadow-lg)}
  .card.wide{grid-column:1/-1}
  .card h2{font-size:14.5px;margin:0 0 4px;font-weight:660;display:flex;align-items:center;gap:9px}
  .card h2 svg{flex:0 0 auto;color:var(--accent);opacity:.9}
  .card h2 .tag{font-size:10.5px;font-weight:600;color:var(--accent);background:var(--accent-soft);
    border-radius:6px;padding:2px 7px;letter-spacing:.2px}
  .card h2 .tag.g{color:#0d7a45;background:var(--green-soft)}
  .hint{color:var(--muted);font-size:12.5px;margin:0 0 15px;line-height:1.62}
  .row{display:flex;gap:9px;flex-wrap:wrap;align-items:center}
  /* 灯效测试：2x2 网格，槽位数字对齐 */
  #demoRow{display:grid;grid-template-columns:1fr 1fr;gap:9px 16px}
  .democell{display:flex;align-items:center;gap:8px;flex-wrap:nowrap}
  .democell .sm{flex:1 1 0;min-width:0;display:flex;align-items:center;justify-content:center;padding-left:4px;padding-right:4px}
  .democell .chip{flex:0 0 auto}
  @media(max-width:600px){#demoRow{grid-template-columns:1fr}}
  .sep{height:1px;background:var(--line2);margin:16px 0}
  .cols2{display:grid;grid-template-columns:1fr 1fr;gap:22px}
  @media(max-width:760px){.cols2{grid-template-columns:1fr}}

  /* ---------- stage ---------- */
  .stage{display:flex;gap:26px;align-items:center;flex-wrap:wrap;padding:22px 24px}
  .stageName{display:flex;align-items:center;gap:15px;min-width:186px}
  .stageTitle{font-size:13px;font-weight:640;letter-spacing:.2px}
  .stageSub{color:var(--muted);font-size:12px;margin-top:2px}
  .slots{display:flex;gap:13px;flex-wrap:wrap;flex:1}
  .slot{
    border:1px solid var(--line);border-radius:13px;padding:12px 14px;min-width:112px;
    background:linear-gradient(180deg,#fff,#fbfcfe);display:flex;flex-direction:column;gap:9px
  }
  .slot .top{display:flex;align-items:center;justify-content:space-between;gap:10px}
  .slot .nm{font-size:11.5px;color:var(--muted);font-weight:600;letter-spacing:.3px}
  .slot .dev{font-family:ui-monospace,Consolas,monospace;font-size:12px;font-weight:600}
  .slot .rt{font-size:11px;color:var(--muted);font-family:ui-monospace,Consolas,monospace}
  .slot.isoff{opacity:.55}
  .nextInfo{font-size:12px;color:var(--muted);min-width:196px;
    border-left:1px solid var(--line2);padding-left:22px;line-height:1.85}
  .nextInfo b{color:var(--text);font-weight:640}
  @media(max-width:760px){.nextInfo{border-left:none;padding-left:0;border-top:1px solid var(--line2);padding-top:13px;width:100%}}

  /* ---------- LED orbs ---------- */
  .led{width:42px;height:42px;border-radius:50%;position:relative;flex:0 0 auto;
    background:#ccd4e2;box-shadow:inset 0 1px 3px rgba(20,28,40,.22)}
  .led::after{content:"";position:absolute;inset:-7px;border-radius:50%;
    background:radial-gradient(circle,rgba(59,110,246,.42),rgba(59,110,246,0) 70%);opacity:0}
  .led.sm{width:15px;height:15px;box-shadow:inset 0 1px 2px rgba(20,28,40,.2)}
  .led.sm::after{inset:-4px}

  .led.solid{background:radial-gradient(circle at 34% 30%,#cfe0ff,#3b6ef6 72%);
    box-shadow:0 0 16px 3px rgba(59,110,246,.45), inset 0 1px 3px rgba(20,28,40,.2)}
  .led.solid::after{opacity:.75}
  .led.heartbeat{background:radial-gradient(circle at 34% 30%,#cfe0ff,#3b6ef6 72%);
    animation:ledHeart 3s infinite}
  .led.heartbeat::after{animation:ledHeartHalo 3s infinite}
  .led.activity{background:radial-gradient(circle at 34% 30%,#ffe9b8,#e79a13 72%);
    animation:ledBlink .16s infinite}
  .led.alarm{background:radial-gradient(circle at 34% 30%,#ffc9c9,#e5484d 72%);
    animation:ledBlink 1s infinite}
  .led.dbreath{background:radial-gradient(circle at 34% 30%,#d6f7e5,#0f9d58 72%);
    box-shadow:0 0 14px 3px rgba(15,157,88,.4), inset 0 1px 3px rgba(20,28,40,.2)}
  .led.levels{background:radial-gradient(circle at 34% 30%,#e7ecf5,#8896ad 72%)}
  .led.off,.led.idle.off{background:#ccd4e2}
  .led.off::after{opacity:0}

  @keyframes ledHeart{
    0%,91%{background:#ccd4e2;box-shadow:inset 0 1px 3px rgba(20,28,40,.22);filter:none}
    92%{background:radial-gradient(circle at 34% 30%,#cfe0ff,#3b6ef6 72%);
        box-shadow:0 0 18px 4px rgba(59,110,246,.5), inset 0 1px 3px rgba(20,28,40,.2)}
    99%,100%{background:#ccd4e2;box-shadow:inset 0 1px 3px rgba(20,28,40,.22)}
  }
  @keyframes ledHeartHalo{0%,91%{opacity:0}92%{opacity:1}99%,100%{opacity:0}}
  @keyframes ledBlink{
    0%,49%{opacity:1;filter:drop-shadow(0 0 6px rgba(231,154,19,.5))}
    50%,100%{opacity:.16;filter:none}
  }

  /* ---------- buttons ---------- */
  button{font:inherit;font-size:13px;border:1px solid var(--line);background:#fff;color:var(--text);
    border-radius:10px;padding:8.5px 15px;cursor:pointer;transition:.15s;
    display:inline-flex;align-items:center;gap:6px;font-weight:520;
    box-shadow:0 1px 1.5px rgba(20,28,40,.03)}
  button:hover{border-color:#c9d5e8;background:#fbfcff;transform:translateY(-.5px);
    box-shadow:0 3px 10px -4px rgba(20,28,40,.2)}
  button:active{transform:translateY(.5px)}
  button.primary{background:linear-gradient(180deg,#4a7bf8,#3766eb);border-color:#3766eb;color:#fff;
    box-shadow:0 2px 8px -2px rgba(59,110,246,.5)}
  button.primary:hover{background:linear-gradient(180deg,#4074f6,#2f5fe0);border-color:#2f5fe0}
  button.warn{color:#b42318;border-color:#f4cdca;background:#fff8f7}
  button.warn:hover{background:#fef1ef;border-color:#eeb6b2}
  button.slot{min-width:74px;justify-content:center}
  button.sm{padding:6.5px 12px;font-size:12.5px;border-radius:9px}
  button[disabled]{opacity:.45;cursor:not-allowed;transform:none;box-shadow:none}

  /* ---------- segmented ---------- */
  .seg{display:inline-flex;background:#edf1f8;border:1px solid var(--line);border-radius:12px;
    padding:3.5px;gap:3px}
  .seg button{border:none;background:transparent;border-radius:9px;padding:7px 18px;
    color:#48586f;box-shadow:none;font-weight:540}
  .seg button:hover{background:#e2e9f5;transform:none;box-shadow:none;border-color:transparent}
  .seg button.active,.seg button.active:hover{
    background:linear-gradient(180deg,#4a7bf8,#3766eb);color:#fff;
    box-shadow:0 2px 7px -1px rgba(59,110,246,.45)}
  .seg button.active::before{content:"";width:5px;height:5px;border-radius:50%;background:#fff;
    box-shadow:0 0 5px 1px rgba(255,255,255,.85)}
  .seg.sm button{padding:6px 13px;font-size:12.5px}

  /* ---------- fields ---------- */
  .field{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}
  .field label{color:var(--muted);font-size:12.5px;min-width:112px;flex:0 0 auto}
  input[type=number],input[type=text]{font:inherit;padding:8px 11px;border:1px solid var(--line);
    border-radius:9px;background:#fff;color:var(--text);transition:.15s}
  input:focus{outline:none;border-color:var(--accent2);box-shadow:0 0 0 3.5px rgba(59,110,246,.13)}
  input[type=number]{width:96px}
  input[type=text]{width:100%;font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
  .fgroup{margin-bottom:2px}
  .fgroup .lbl{font-size:12px;color:var(--muted);font-weight:600;margin:0 0 8px;
    display:flex;align-items:center;gap:7px}
  .fgroup .lbl .mini{font-size:11px;font-weight:500;color:var(--muted2)}
  .prefill{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
  .prefill button{padding:5px 10px;font-size:11.5px;border-radius:8px;color:var(--muted);
    background:#f7f9fc}
  .prefill button:hover{color:var(--accent);border-color:#cfdcf8;background:#f4f8ff}

  /* ---------- table ---------- */
  .disks{width:100%;border-collapse:collapse;font-size:13px}
  .disks th{text-align:left;color:var(--muted);font-weight:580;font-size:11.5px;
    padding:7px 9px;border-bottom:1px solid var(--line);letter-spacing:.3px}
  .disks td{padding:10px 9px;border-bottom:1px solid var(--line2);vertical-align:middle}
  .disks tr:last-child td{border-bottom:none}
  .disks tbody tr:hover{background:#fafcff}
  .mono{font-family:ui-monospace,Consolas,"Courier New",monospace}
  .chip{display:inline-flex;align-items:center;gap:5px;border-radius:7px;padding:2.5px 9px;white-space:nowrap;
    font-size:11.5px;font-weight:620;background:var(--accent-soft);color:var(--accent)}
  .chip.gray{background:#f1f4f9;color:var(--muted)}
  .chip.green{background:var(--green-soft);color:#0d7a45}
  .chip.amber{background:var(--amber-soft);color:#96650a}
  .chip.red{background:#fdecec;color:#c0343a}
  .bar{width:76px;height:5px;border-radius:4px;background:#eef2f8;overflow:hidden}
  .bar i{display:block;height:100%;border-radius:4px;
    background:linear-gradient(90deg,#8fb4ff,#3b6ef6);transition:width .35s}
  .bar-wrap{display:flex;align-items:center;gap:9px}

  /* ---------- log / toast ---------- */
  pre{margin:0;background:linear-gradient(180deg,#141c28,#101720);color:#cfdcf0;border-radius:12px;
    padding:13px 16px;font-size:12px;overflow:auto;max-height:190px;white-space:pre-wrap;
    font-family:ui-monospace,Consolas,"Courier New",monospace;line-height:1.65;
    box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
  #toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(22px);
    background:rgba(20,28,40,.94);backdrop-filter:blur(8px);color:#fff;
    padding:11px 20px;border-radius:12px;font-size:13px;font-weight:520;
    opacity:0;pointer-events:none;transition:.26s cubic-bezier(.2,.8,.2,1);
    box-shadow:0 16px 40px -10px rgba(20,28,40,.5);z-index:9;max-width:80vw}
  #toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  .foot{color:var(--muted2);font-size:12px;margin-top:20px;margin-bottom:26px;text-align:center;line-height:2;letter-spacing:.4px}
  .foot .hl{color:var(--accent);font-weight:600}
  .kbd{background:#fff;border:1px solid var(--line);border-radius:6px;padding:2px 7px;
    font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--muted)}
  .sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo"><i></i></div>
    <div>
      <h1>Zero1 Pro 灯控面板</h1>
    </div>
    <div class="hdright">
      <div class="pill"><span id="dot" class="dot"></span><span id="svcText">连接中…</span></div>
      <button class="themebtn" id="themeBtn" title="切换深色 / 浅色模式" onclick="toggleTheme()">
        <span class="theme-ico" id="themeIco"></span>
        <span class="theme-txt" id="themeTxt">深色模式</span>
      </button>
      <button class="iconbtn" title="立即刷新" onclick="refresh()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2.1" stroke-linecap="round"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/></svg>
      </button>
    </div>
  </header>

  <!-- ============ 状态总览 ============ -->
  <div class="grid" style="margin-bottom:17px">
    <div class="card wide stage">
      <div class="stageName">
        <div class="led off" id="pwLed"></div>
        <div>
          <div class="stageTitle">电源灯</div>
          <div class="stageSub" id="pwLedSub">读取中…</div>
        </div>
      </div>
      <div class="slots" id="slotsWrap"></div>
      <div class="nextInfo" id="nextInfo">—</div>
    </div>
  </div>

  <div class="grid g2">
    <!-- ============ 电源灯 ============ -->
    <div class="card">
      <h2>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round"><path d="M12 3v9"/><path d="M6.4 6.4a8 8 0 1 0 11.2 0"/></svg>
        电源灯
      </h2>
      <p class="hint">直写 Super I/O 的 PWR_LED 位。呼吸用 delta-sigma 调制做无级调光，人眼看不到抖动</p>
      <div class="seg" id="powerSeg" style="margin-bottom:13px">
        <button data-v="on" onclick="act('power',{state:'on'})">常亮</button>
        <button data-v="breath" onclick="act('power',{state:'breath'})">呼吸</button>
        <button data-v="off" onclick="act('power',{state:'off'})">熄灭</button>
      </div>
      <div class="cols2">
        <div class="field"><label>呼吸周期 ms</label>
          <input type="number" id="cBms" min="800" max="20000" step="100" onchange="saveBreath()"></div>
        <div class="field"><label>最暗亮度 %</label>
          <input type="number" id="cBmin" min="0" max="90" step="1" onchange="saveBreath()"></div>
      </div>
      <p class="hint" style="margin:10px 0 0;font-size:12px">
        一个周期 = 一次「渐亮 → 渐暗」往复。最暗设 <span class="mono">0</span> 为完全熄灭；
        设 <span class="mono">8~20</span> 留一点余光，夜里更柔和。
      </p>
    </div>

    <!-- ============ 硬盘灯 ============ -->
    <div class="card">
      <h2>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round"><rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/>
          <path d="M7 7.5h.01M7 16.5h.01"/></svg>
        硬盘灯 <span class="tag">C3 灯语</span>
      </h2>
      <p class="hint">有读写就快闪、掉盘慢闪告警（任何状态都压不掉告警）；空闲可选心跳或长亮</p>
      <div class="seg" id="diskSeg" style="margin-bottom:13px">
        <button data-v="on" onclick="setDisk('on')">常亮</button>
        <button data-v="heartbeat" onclick="setDisk('heartbeat')">心跳</button>
        <button data-v="off" onclick="setDisk('off')">熄灭</button>
      </div>

      <div class="sep"></div>
      <div class="hint" id="diskSub" style="margin:0"></div>

      <div class="legend">
        <div><span class="chip">常亮</span> 盘在线时常亮</div>
        <div><span class="chip gray">心跳</span> 空闲时每 3 秒轻眨一下</div>
        <div><span class="chip amber">快闪</span> 有读写时密集闪烁</div>
        <div><span class="chip red">慢闪</span> 掉盘 / 离线告急</div>
      </div>
    </div>
  </div>

  <div class="grid">
    <!-- ============ 夜间与定时 ============ -->
    <div class="card wide">
      <h2>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round"><path d="M20.5 13.5A8.5 8.5 0 1 1 10.5 3.5a7 7 0 0 0 10 10Z"/></svg>
        夜间模式与定时
        <span class="tag g" id="nightTag" style="display:none">当前正在生效</span>
      </h2>
      <p class="hint">到点自动熄灭刺眼的灯。电源灯和硬盘灯各有一张独立时间表，跨午夜（如 22:00-07:00）自动处理</p>

      <div class="cols2">
        <div class="fgroup">
          <div class="lbl">夜间电源灯做什么</div>
          <div class="seg sm" id="nightSeg">
            <button data-v="heartbeat" onclick="setNight('heartbeat')">极简心跳</button>
            <button data-v="off" onclick="setNight('off')">彻底熄灭</button>
          </div>
          <div class="hint" style="margin:9px 0 0;font-size:12px">
            极简心跳：每 10 秒亮 80ms，占空比 &lt;1%，暗处几乎无感但能看出机器还活着
          </div>
        </div>
        <div class="fgroup">
          <div class="lbl">夜间掉盘告警 <span class="mini">建议保持开启</span></div>
          <div class="seg sm" id="excSeg">
            <button data-v="on" onclick="setExc('on')">破例闪灯</button>
            <button data-v="off" onclick="setExc('off')">彻底静默</button>
          </div>
          <div class="hint" style="margin:9px 0 0;font-size:12px">
            掉盘 / 离线时即使处于夜间时段也照常告警——这比刺眼重要得多
          </div>
        </div>
      </div>

      <div class="sep"></div>

      <div class="cols2">
        <div class="fgroup">
          <div class="lbl">电源灯时间表</div>
          <div class="field" style="margin-bottom:8px">
            <div class="seg sm" id="pwSchSeg">
              <button data-v="on">启用</button>
              <button data-v="off">停用</button>
            </div>
          </div>
          <input type="text" id="pwWin" placeholder="22:00-07:00">
          <div class="prefill">
            <button onclick="fillWin('pwWin','22:00-07:00')">22-07</button>
            <button onclick="fillWin('pwWin','23:00-07:00')">23-07</button>
            <button onclick="fillWin('pwWin','01:00-06:00')">01-06</button>
            <button onclick="fillWin('pwWin','22:00-07:00,13:00-14:00')">含午休</button>
          </div>
        </div>
        <div class="fgroup">
          <div class="lbl">硬盘灯时间表</div>
          <div class="field" style="margin-bottom:8px">
            <div class="seg sm" id="dkSchSeg">
              <button data-v="on">启用</button>
              <button data-v="off">停用</button>
            </div>
          </div>
          <input type="text" id="dkWin" placeholder="22:00-07:00">
          <div class="prefill">
            <button onclick="fillWin('dkWin','22:00-07:00')">22-07</button>
            <button onclick="fillWin('dkWin','23:00-07:00')">23-07</button>
            <button onclick="fillWin('dkWin','01:00-06:00')">01-06</button>
            <button onclick="fillWin('dkWin','22:00-07:00,13:00-14:00')">含午休</button>
          </div>
        </div>
      </div>

      <div class="row" style="margin-top:15px">
        <button class="primary" onclick="saveSch()">保存时间表</button>
        <span style="color:var(--muted2);font-size:12px">多个时段用英文逗号分隔，如 <span class="mono">22:00-07:00,13:00-14:00</span></span>
      </div>

      <div class="sep"></div>
      <div class="row">
        <span style="color:var(--muted);font-size:12.5px;min-width:78px">常用方案</span>
        <button class="sm" id="pbNone" onclick="preset('none')">关闭定时</button>
        <button class="sm" id="pbAllOff" onclick="preset('all-off')">夜晚全灭</button>
        <button class="sm" id="pbPwOnly" onclick="preset('power-only')">只灭电源灯</button>
        <button class="sm" id="pbDiskOnly" onclick="preset('disk-only')">只灭硬盘灯</button>
        <button class="sm" id="pbPwHb" onclick="preset('power-heartbeat')">全灭 + 电源灯心跳</button>
      </div>
    </div>
  </div>

  <div class="grid g2">
    <!-- ============ 参数 ============ -->
    <div class="card">
      <h2>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h10"/></svg>
        灯效参数
      </h2>
      <p class="hint">改完保存即时生效，无需重启服务</p>
      <div class="pgrid">
        <div class="field"><label>心跳周期 ms</label><input type="number" id="cHb" min="500" max="20000" step="100"></div>
        <div class="field"><label>心跳脉冲 ms</label><input type="number" id="cHbp" min="20" max="500" step="10"></div>
        <div class="field"><label>快闪半周期 ms</label><input type="number" id="cBlink" min="20" max="500" step="10"></div>
        <div class="field"><label>活动保持 ms</label><input type="number" id="cHold" min="100" max="5000" step="100"></div>
        <div class="field"><label>告警半周期 ms</label><input type="number" id="cAlarm" min="100" max="2000" step="50"></div>
        <div class="field"><label>活动阈值 KB</label><input type="number" id="cThr" min="0" max="4096" step="16"></div>
      </div>
      <div class="sep"></div>
      <div class="pgrid">
        <div class="field"><label>夜间心跳周期 ms</label><input type="number" id="cNHb" min="1000" max="60000" step="1000"></div>
        <div class="field"><label>夜间脉冲 ms</label><input type="number" id="cNHbp" min="20" max="500" step="10"></div>
      </div>
      <div class="row" style="margin-top:6px"><button class="primary" onclick="saveParam()">保存参数</button>
        <button class="sm" onclick="act('engine_restart',{})">重启引擎</button></div>
    </div>

    <!-- ============ 测试 ============ -->
    <div class="card">
      <h2>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round"><path d="M9 18h6M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.7V18h8v-3.3A7 7 0 0 0 12 2Z"/></svg>
        灯效测试 <span class="tag">核对槽位</span>
      </h2>
      <p class="hint">先点「闪」确认是哪盏灯亮，把实际灯位与槽位对应关系核对一遍</p>
      <div class="row" id="demoRow"></div>
      <div class="sep"></div>
      <div class="row">
        <button onclick="act('sweep',{})">四槽依次自检</button>
        <button onclick="act('probe',{})">亮度分级探测</button>
        <button onclick="act('demo',{slot:0,mode:'off'})">停止演示</button>
        <button onclick="act('engine_restart',{})">重启引擎</button>
      </div>
      <div class="hint" style="margin:13px 0 0;font-size:12px">
        演示会临时接管灯效，6 秒后自动交还引擎；告警演示持续 10 分钟，点「停止演示」即可中断。<br>
        「亮度分级探测」会让 0 号盘依次走 0x00→0x40→0x80→0xc0→0xff（每档 2 秒）：
        如果看到 5 级不同亮度，说明这组寄存器支持模拟调光；只有亮/灭两态则是纯开关（当前的呼吸走 PWM，够用）。
      </div>
    </div>
  </div>

  <div class="foot">
    © 2026 灯控中心 <span class="hl">v1.0.7</span> · Crafted by 西了个瓜
  </div>
</div>
<div id="toast"></div>

<script>
function toast(msg, bad){
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background = bad ? 'rgba(180,35,24,.95)' : 'rgba(20,28,40,.94)';
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(function(){ t.classList.remove('show'); }, 2800);
}
function logOut(txt){ /* 执行输出卡片已移除，保留空函数避免调用点报错 */ }
function setSeg(id, val){
  var seg = document.getElementById(id);
  if(!seg) return;
  Array.prototype.forEach.call(seg.querySelectorAll('button'), function(b){
    b.classList.toggle('active', b.dataset.v === val);
  });
}
function segVal(id){
  var seg = document.getElementById(id);
  var b = seg && seg.querySelector('button.active');
  return b ? b.dataset.v : null;
}
function esc(s){ return String(s).replace(/[<>&"]/g, function(c){
  return {'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]; }); }
function $(id){ return document.getElementById(id); }

/* ---------- schedule helpers (client side) ---------- */
function parseRanges(windows){
  var out = [];
  (windows||[]).forEach(function(w){
    var m = /^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$/.exec(String(w).trim());
    if(!m) return;
    out.push({ a:+m[1]*60 + +m[2], b:+m[3]*60 + +m[4] });
  });
  return out;
}
function inAnyRange(ranges, min){
  for(var i=0;i<ranges.length;i++){
    var r = ranges[i];
    if(r.a === r.b) continue;
    if(r.a < r.b){ if(min >= r.a && min < r.b) return true; }
    else { if(min >= r.a || min < r.b) return true; }   /* 跨午夜 */
  }
  return false;
}
function fmtMin(min){
  min = ((min % 1440) + 1440) % 1440;
  var h = Math.floor(min/60), m = min%60;
  return (h<10?'0':'') + h + ':' + (m<10?'0':'') + m;
}
function nextBoundary(ranges, min){
  var best = null;
  ranges.forEach(function(r){
    [r.a, r.b].forEach(function(x){
      var d = x - min;
      if(d <= 0) d += 1440;
      if(best === null || d < best.d) best = { d:d, t:x, on: (x === r.a ? true : false) };
    });
  });
  return best;
}
function humanDelta(mins){
  var h = Math.floor(mins/60), m = mins%60;
  if(h && m) return h + ' 小时 ' + m + ' 分';
  if(h) return h + ' 小时';
  return m + ' 分';
}

/* ---------- LED rendering ---------- */
var MODE_LED = { idle:'heartbeat', breath:'dbreath', activity:'activity', alarm:'alarm',
                 solid:'solid', blink:'activity', off:'off' };
var MODE_TEXT = {
  idle:['在线 · 心跳','green'], breath:['在线 · 呼吸','green'],
  activity:['读写中 · 快闪','amber'],
  alarm:['掉盘告警','red'], solid:['常亮','gray'],
  blink:['演示闪烁','gray'], off:['熄灭','gray']
};
var LED_FX = {};   /* 供 CSS 动画时长使用 */
var LED_FX_SIG = '';

function applyLedFx(){
  var hb = LED_FX.hb || 3000, bl = LED_FX.blink || 80, al = LED_FX.alarm || 500,
      nhb = LED_FX.nhb || 10000, bms = LED_FX.breath || 3000,
      dbms = LED_FX.dbreath || 4000, dbmin = LED_FX.dbmin;
  if(dbmin === undefined || dbmin === null) dbmin = 30;
  var sig = [hb, bl, al, nhb, bms, dbms, dbmin].join('/');
  if(sig === LED_FX_SIG) return;      /* 参数没变就不要重建样式，否则会打断动画 */
  LED_FX_SIG = sig;
  var st = document.getElementById('ledfx');
  if(!st){ st = document.createElement('style'); st.id = 'ledfx'; document.head.appendChild(st); }
  var mn = Math.max(0, Math.min(90, dbmin)) / 100;
  st.textContent =
    '.led.heartbeat{animation:ledHeartEx ' + hb + 'ms infinite}' +
    '@keyframes ledHeartEx{0%,91%{background:#ccd4e2;box-shadow:inset 0 1px 3px rgba(20,28,40,.22)}' +
    '92%{background:radial-gradient(circle at 34% 30%,#cfe0ff,#3b6ef6 72%);' +
    'box-shadow:0 0 18px 4px rgba(59,110,246,.5),inset 0 1px 3px rgba(20,28,40,.2)}' +
    '99%,100%{background:#ccd4e2;box-shadow:inset 0 1px 3px rgba(20,28,40,.22)}}' +
    '.led.heartbeat::after{animation:ledHeartHaloEx ' + hb + 'ms infinite}' +
    '@keyframes ledHeartHaloEx{0%,91%{opacity:0}92%{opacity:1}99%,100%{opacity:0}}' +
    '.led.nightheartbeat{animation:ledNightEx ' + nhb + 'ms infinite}' +
    '@keyframes ledNightEx{0%,97%{background:#ccd4e2;box-shadow:inset 0 1px 3px rgba(20,28,40,.22)}' +
    '98%{background:radial-gradient(circle at 34% 30%,#cfe0ff,#3b6ef6 72%);' +
    'box-shadow:0 0 18px 4px rgba(59,110,246,.5),inset 0 1px 3px rgba(20,28,40,.2)}' +
    '99.6%,100%{background:#ccd4e2;box-shadow:inset 0 1px 3px rgba(20,28,40,.22)}}' +
    '.led.activity{animation:ledBlinkEx ' + (bl*2) + 'ms infinite}' +
    '@keyframes ledBlinkEx{0%,49%{opacity:1;filter:drop-shadow(0 0 5px rgba(231,154,19,.45))}' +
    '50%,100%{opacity:.14;filter:none}}' +
    '.led.alarm{animation:ledBlinkEx ' + (al*2) + 'ms infinite}' +
    '.led.breath{animation:ledBreathEx ' + bms + 'ms ease-in-out infinite}' +
    '@keyframes ledBreathEx{0%,100%{background:radial-gradient(circle at 34% 30%,#e6ebf4,#c6cfdd);' +
    'box-shadow:inset 0 1px 3px rgba(20,28,40,.22)}' +
    '50%{background:radial-gradient(circle at 34% 30%,#cfe0ff,#3b6ef6 72%);' +
    'box-shadow:0 0 18px 4px rgba(59,110,246,.5),inset 0 1px 3px rgba(20,28,40,.2)}}' +
    /* 硬盘灯呼吸：与引擎的 delta-sigma 调制同周期，最暗亮度也一致 */
    '.led.dbreath{animation:ledDBreathEx ' + dbms + 'ms linear infinite}' +
    '@keyframes ledDBreathEx{0%,100%{opacity:' + mn.toFixed(2) +
    ';box-shadow:inset 0 1px 2px rgba(20,28,40,.2)}' +
    '50%{opacity:1;box-shadow:0 0 14px 3px rgba(15,157,88,.42),inset 0 1px 3px rgba(20,28,40,.2)}}';
}

/* 呼吸相位：wave 按"相邻错开"、phase 按"均分一个周期"，
   再叠加引擎报来的真实相位，用负 animation-delay 让预览和前台真实灯同相。
   （不给延迟的话，每次轮询重建元素动画都会从相位 0 重新开始，看起来就是"跳"和"不同步"） */
function breathOffsetMs(slotOrder, n, mode, perMs, stepMs){
  if(mode === 'wave') return slotOrder * stepMs;
  if(mode === 'phase') return Math.round(slotOrder * perMs / Math.max(1, n));
  return 0;
}
function breathDelayMs(slotOrder, n, mode, perMs, stepMs){
  var base = LED_FX.dphase || 0;
  var off = breathOffsetMs(slotOrder, n, mode, perMs, stepMs);
  var v = ((base + off) % perMs + perMs) % perMs;
  return -v;
}

/* ---------- main refresh ---------- */
function refresh(){
  var t0 = performance.now();
  fetch('/api/status').then(function(r){ return r.json(); }).then(function(d){
    if(!d.ok) throw new Error(d.error || 'status failed');
    d.data.__rtt = performance.now() - t0;   /* 用来补偿状态回传的延迟，让预览相位对得上 */
    apply(d.data);
  }).catch(function(e){
    $('dot').className = 'dot off';
    $('svcText').textContent = 'NAS 未连通';
  });
}

function apply(d){
  var fresh = (Date.now()/1000 - (d.ts||0)) < 6;
  var ok = d.service && d.service.active === 'active';
  $('dot').className = 'dot ' + (fresh && ok ? 'on' : (fresh ? 'off' : 'busy'));
  $('svcText').textContent = !fresh ? '引擎无响应' : (ok ? '引擎运行中' : '引擎已停止');

  var now = new Date();
  var nowMin = now.getHours()*60 + now.getMinutes();
  var pwRanges = parseRanges(d.schedule.power.windows);
  var dkRanges = parseRanges(d.schedule.disk.windows);
  var nightOn = (d.schedule.power.enabled && inAnyRange(pwRanges, nowMin))
             || (d.schedule.disk.enabled && inAnyRange(dkRanges, nowMin));

  /* 参数 -> 动画（LED 预览与实际引擎参数保持一致） */
  var ncfg = d.night_cfg || {};
  LED_FX = { hb:d.disk.heartbeat_ms, blink:d.disk.activity_blink_ms,
             alarm:d.disk.alarm_blink_ms,
             nhb:(ncfg.power_heartbeat_ms || 10000),
             nhbp:(ncfg.power_heartbeat_pulse_ms || 80),
             breath:((d.power || {}).breath_ms || 3000),
             dbreath:(d.disk.breath_ms || 4000),
             dbmin:(d.disk.breath_min === undefined ? 30 : d.disk.breath_min),
             dmode:(d.disk.breath_mode || 'sync'),
             dstep:(d.disk.breath_step_ms || 220),
             dphase:0 };
  /* 引擎报来的呼吸相位 -> 预览相位（加上状态生成到现在的耗时，以及半个往返延迟） */
  var dT = LED_FX.dbreath || 4000;
  var ph = (d.disk.breath_phase_ms || 0);
  if(d.disk.phase_at_ms) ph += Math.max(0, Date.now() - d.disk.phase_at_ms);
  ph += (d.__rtt || 0) / 2;
  LED_FX.dphase = ((ph % dT) + dT) % dT;
  applyLedFx();

  /* 电源灯总览 */
  var pwLed = $('pwLed');
  var cls = 'off', sub = '';
  if(d.night.power){
    cls = (d.night.power_mode === 'heartbeat') ? 'nightheartbeat' : 'off';
    sub = '夜间时段 · ' + (d.night.power_mode === 'heartbeat' ? '极简心跳' : '已熄灭');
  } else if(d.power.mode === 'breath'){
    cls = 'breath';
    sub = '呼吸中 · 周期 ' + (d.power.breath_ms || 3000) + 'ms';
  } else if(d.power.state === 'on'){
    cls = 'solid'; sub = '常亮中';
  } else {
    cls = 'off'; sub = '已熄灭';
  }
  pwLed.className = 'led ' + cls;
  $('pwLedSub').textContent = sub;

  /* 夜间 / 熄灯 徽标（已按需求移除） */
  var ntag = $('nightTag');
  if(nightOn){
    ntag.className = 'tag g';
    ntag.textContent = '当前正在生效';
    ntag.style.display = 'inline-block';
  } else {
    ntag.style.display = 'none';
  }

  /* 电源灯卡 */
  setSeg('powerSeg', d.power.mode);
  /* 硬盘灯卡 */
  /* 硬盘灯卡：switch=off → 熄灭；否则按 idle_mode 高亮 常亮/心跳 */
  setSeg('diskSeg', d.disk.switch === 'off' ? 'off' : (d.disk.idle_mode === 'on' ? 'on' : 'heartbeat'));
  $('diskSub').textContent = '快闪 ' + d.disk.activity_blink_ms + 'ms · 阈值 ' + d.disk.threshold_kb + 'KB'
    + (d.disk.mod_tick_ms ? ' · 调制 ' + d.disk.mod_tick_ms + 'ms' : '')
    + (d.enabled === 'enabled' ? ' · 已设开机自启' : ' · 未设自启');

  /* 定时控件 */
  setSeg('nightSeg', d.night.power_mode);
  setSeg('excSeg', d.night.exception ? 'on' : 'off');
  if(!segPending.pwSchSeg) setSeg('pwSchSeg', d.schedule.power.enabled ? 'on' : 'off');
  if(!segPending.dkSchSeg) setSeg('dkSchSeg', d.schedule.disk.enabled ? 'on' : 'off');

  /* 常用方案高亮：由引擎真实时间表状态推导，均不匹配则无高亮 */
  var pwOn = d.schedule.power.enabled, dkOn = d.schedule.disk.enabled;
  var activePreset = '';
  if(!pwOn && !dkOn) activePreset = 'pbNone';
  else if(pwOn && !dkOn) activePreset = 'pbPwOnly';
  else if(!pwOn && dkOn) activePreset = 'pbDiskOnly';
  else if(pwOn && dkOn) activePreset = (d.night.power_mode === 'off') ? 'pbAllOff' : 'pbPwHb';
  ['pbAllOff','pbPwHb','pbPwOnly','pbDiskOnly','pbNone'].forEach(function(id){
    var b = $(id);
    if(b) b.className = 'sm' + (id === activePreset ? ' primary' : '');
  });

  var pw = $('pwWin'), dk = $('dkWin');
  if(!inpPending.pwWin && document.activeElement !== pw)
    pw.value = (d.schedule.power.windows||[]).join(',');
  if(!inpPending.dkWin && document.activeElement !== dk)
    dk.value = (d.schedule.disk.windows||[]).join(',');

  /* 参数框 */
  var f = {'cHb':['disk','heartbeat_ms'],'cHbp':['disk','heartbeat_pulse_ms'],
           'cBlink':['disk','activity_blink_ms'],'cHold':['disk','activity_hold_ms'],
           'cAlarm':['disk','alarm_blink_ms'],'cThr':['disk','threshold_kb'],
           'cNHb':['night_cfg','power_heartbeat_ms'],
           'cNHbp':['night_cfg','power_heartbeat_pulse_ms']};
  Object.keys(f).forEach(function(id){
    var el = $(id), src = id.indexOf('cN') === 0 ? ncfg : d.disk;
    var v = src[f[id][1]];
    if(el && !inpPending[id] && document.activeElement !== el
       && v !== undefined && v !== null) el.value = v;
  });

  /* 下次切换 */
  var ni = $('nextInfo'), lines = [];
  if(nightOn){ lines.push('<b>当前处于夜间时段</b>'); }
  if(d.disk && d.disk.switch_cfg === 'off'){
    lines.push('硬盘灯总开关为「<b>熄灭</b>」，需切到「跟随系统」后定时才会改变显示');
  }
  [['电源灯', pwRanges, d.schedule.power.enabled], ['硬盘灯', dkRanges, d.schedule.disk.enabled]].forEach(function(x){
    if(!x[2] || !x[1].length){ lines.push(x[0] + '定时：未启用'); return; }
    var nb = nextBoundary(x[1], nowMin);
    if(!nb){ lines.push(x[0] + '时间表为空'); return; }
    var active = inAnyRange(x[1], nowMin);
    lines.push(x[0] + ' ' + (active ? '恢复' : '熄灭') + '倒计时 <b>' + humanDelta(nb.d) + '</b>（' + fmtMin(nb.t) + '）');
  });
  ni.innerHTML = '<div style="font-weight:640;color:var(--text);margin-bottom:5px">定时概览</div>'
    + lines.join('<br>');

  /* 槽位 LED */
  var slots = d.disks || [];
  var usedN = 0, ordOf = {};
  slots.forEach(function(x){ if(x.dev){ ordOf[x.slot] = usedN++; } });
  var wrap = $('slotsWrap'), html = '';
  slots.forEach(function(x){
    var mt = MODE_TEXT[x.mode] || [x.mode,'gray'];
    var ledCls = MODE_LED[x.mode] || 'off';
    var dev = x.dev ? esc(x.dev) : '未使用';
    var rate = '<span class="rt">' + (x.dev ? (x.rate >= 1 ? x.rate.toFixed(1)+' MB/s'
              : Math.round(x.rate*1024)+' KB/s') : '—') + '</span>';
    var extra = '';
    if(ledCls === 'dbreath'){
      extra = ' style="animation-delay:' + breathDelayMs(ordOf[x.slot]||0, usedN,
              LED_FX.dmode, LED_FX.dbreath, LED_FX.dstep) + 'ms"';
    }
    html += '<div class="slot' + (x.dev ? '' : ' isoff') + '">'
      + '<div class="top"><span class="nm">槽位 ' + (x.slot + 1) + '</span>'
      + '<span class="chip ' + mt[1] + '">' + mt[0] + '</span></div>'
      + '<div style="display:flex;align-items:center;gap:10px">'
      + '<div class="led sm ' + ledCls + '"' + extra + '></div>'
      + '<div><div class="dev">' + dev + '</div>' + rate + '</div></div></div>';
  });
  wrap.innerHTML = html || '<div style="color:var(--muted);font-size:12.5px">未发现硬盘</div>';
}

/* ---------- actions ---------- */
var DEMO_MODES = [['blink','闪'],['solid','常亮'],['alarm','告警']];
(function buildDemo(){
  var html = '';
  for(var s=0;s<4;s++){
    html += '<div class="democell"><span class="chip">' + (s + 1) + '</span>';
    DEMO_MODES.forEach(function(m){
      html += '<button class="sm" onclick="act(\'demo\',{slot:'+s+',mode:\''+m[0]+'\'})">'+m[1]+'</button>';
    });
    html += '</div>';
  }
  $('demoRow').innerHTML = html;
})();

function busyOn(){ Array.prototype.forEach.call(document.querySelectorAll('button'), function(b){ b.disabled = true; }); }
function busyOff(){ Array.prototype.forEach.call(document.querySelectorAll('button'), function(b){ b.disabled = false; }); }

function act(api, body, silent){
  busyOn();
  return fetch('/api/' + api, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})
  }).then(function(r){ return r.json(); }).then(function(d){
    logOut(d.output || d.error || '');
    if(!silent) toast(d.ok ? (d.message || '已执行') : (d.error || '执行失败'), !d.ok);
  }).catch(function(e){
    toast('请求失败：' + e.message, true);
  }).then(function(){ busyOff(); refresh(); });
}

function setNight(v){ act('night', {power_mode: v}); }
function setExc(v){ act('exception', {state: v}); }
function preset(p){ act('schedule_preset', {preset: p}); }
function fillWin(id, v){ $(id).value = v; }
function saveSch(){
  busyOn();
  function saveOne(g, segId, winId){
    return fetch('/api/schedule_set', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({group:g, state: segVal(segId) || 'off',
                              windows: $(winId).value})})
      .then(function(r){ return r.json(); });
  }
  /* 必须串行：每次保存都是「读配置-改-写」，并发时后写者会用旧快照
     把前者的改动冲掉（丢更新） */
  saveOne('power','pwSchSeg','pwWin')
    .then(function(d1){ return saveOne('disk','dkSchSeg','dkWin')
      .then(function(d2){ return [d1,d2]; }); })
    .then(function(ds){
      logOut(ds.map(function(d){ return d.output || d.error || ''; }).join('\n'));
      var bad = ds.filter(function(d){ return !d.ok; });
      toast(bad.length ? (bad[0].error || '保存失败') : '时间表已保存', !!bad.length);
    }).catch(function(e){ toast('请求失败：'+e.message, true); })
    .then(function(){
      segPending.pwSchSeg = segPending.dkSchSeg = false;
      inpPending.pwWin = inpPending.dkWin = false;
      busyOff(); refresh();
    });
}
function saveParam(){
  ['cHb','cHbp','cBlink','cHold','cAlarm','cThr','cNHb','cNHbp']
    .forEach(function(id){ inpPending[id] = false; });
  act('param', {items:{
    'heartbeat-ms': +$('cHb').value,
    'heartbeat-pulse-ms': +$('cHbp').value,
    'blink-ms': +$('cBlink').value,
    'hold-ms': +$('cHold').value,
    'alarm-ms': +$('cAlarm').value,
    'threshold-kb': +$('cThr').value,
    'night-hb-ms': +$('cNHb').value,
    'night-hb-pulse-ms': +$('cNHbp').value
  }});
}
function saveBreath(){
  inpPending.cBms = inpPending.cBmin = false;
  act('param', {items:{
    'breath-ms': +$('cBms').value,
    'breath-min': +$('cBmin').value
  }});
}
function setBreathMode(v){ act('disk_breath', {mode: v}); }
function setDisk(v){ act('disk_mode', {state: v}); }

/* segmented toggles that should NOT fire on click (saved by 保存时间表).
   pending 标记：点过但未保存前，3 秒轮询不得回写高亮（否则选择被清掉、
   保存时 segVal 读回旧值，表现为"启用不了"） */
var segPending = {};
['pwSchSeg','dkSchSeg'].forEach(function(id){
  var seg = $(id);
  seg.addEventListener('click', function(e){
    var b = e.target.closest('button');
    if(!b || !seg.contains(b)) return;
    segPending[id] = true;
    setSeg(id, b.dataset.v);
  });
});

/* 输入框 pending：改过但未保存前，3 秒轮询不得回写（否则失焦后改动被旧值冲掉） */
var inpPending = {};
['pwWin','dkWin',
 'cHb','cHbp','cBlink','cHold','cAlarm','cThr','cNHb','cNHbp',
 'cBms','cBmin'].forEach(function(id){
  var el = $(id);
  if(el) el.addEventListener('input', function(){ inpPending[id] = true; });
});

/* ---------- dark / light theme ---------- */
var SUN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v2.2M12 19.8V22M2 12h2.2M19.8 12H22M4.6 4.6l1.6 1.6M17.8 17.8l1.6 1.6M19.4 4.6l-1.6 1.6M6.2 17.8l-1.6 1.6"/></svg>';
var MOON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.5 13.5A8.5 8.5 0 1 1 10.5 3.5a7 7 0 0 0 10 10Z"/></svg>';
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  var ico = $('themeIco'), txt = $('themeTxt');
  if(!ico) return;
  if(t === 'dark'){ ico.innerHTML = SUN_SVG; txt.textContent = '浅色模式'; }
  else { ico.innerHTML = MOON_SVG; txt.textContent = '深色模式'; }
}
function toggleTheme(){
  var t = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  applyTheme(t);
  try{ localStorage.setItem('led-theme', t); }catch(e){}
}
(function(){
  var saved = null;
  try{ saved = localStorage.getItem('led-theme'); }catch(e){}
  var t = saved || (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  applyTheme(t);
})();

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, ok, message="", output="", error=None):
        self._send(200, json.dumps(
            {"ok": ok, "message": message, "output": output,
             "error": error if error is not None else (None if ok else message)},
            ensure_ascii=False))

    def do_GET(self):
        if self.path == "/favicon.ico":
            self._send(200, FAVICON_B64 and base64.b64decode(FAVICON_B64), "image/png")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path.startswith("/api/status"):
            ok, out = remote(STATUS_CMD)
            data = parse_status(out) if ok else None
            if not ok or data is None:
                self._send(200, json.dumps(
                    {"ok": False, "error": out or "无法读取引擎状态文件"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"ok": True, "data": data}, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}

        path = self.path.split("?")[0]
        cmd, msg = None, ""

        if path == "/api/power":
            state = body.get("state")
            if state not in ("on", "off", "breath"):
                return self._json(False, "state 不合法")
            cmd, msg = ("%s power %s" % (LEDCTL, state),
                        "电源灯 " + {"on": "常亮", "off": "熄灭", "breath": "呼吸"}[state])

        elif path == "/api/disk":
            state = body.get("state")
            if state not in ("on", "off"):
                return self._json(False, "state 不合法")
            if state == "on":
                # 顺带兜底把引擎拉起来，避免之前的 lights-off 之后无法一键恢复
                cmd = "%s disk on" % LEDCTL
                msg = "自动灯效已开启"
            else:
                cmd, msg = "%s disk off" % LEDCTL, "自动灯效已关闭"

        elif path == "/api/night":
            v = body.get("power_mode")
            if v not in ("off", "heartbeat"):
                return self._json(False, "power_mode 不合法")
            cmd = "%s night power-mode %s" % (LEDCTL, v)
            msg = "夜间电源灯：" + ("极简心跳" if v == "heartbeat" else "熄灭")

        elif path == "/api/exception":
            v = body.get("state")
            if v not in ("on", "off"):
                return self._json(False, "state 不合法")
            cmd, msg = "%s night exception %s" % (LEDCTL, v), ("夜间告警破例已开" if v == "on" else "夜间告警破例已关")

        elif path == "/api/schedule_preset":
            p = body.get("preset")
            if p not in ("all-off", "power-heartbeat", "power-only", "disk-only", "none"):
                return self._json(False, "preset 不合法")
            cmd = "%s schedule preset %s" % (LEDCTL, p)
            msg = {"all-off": "已套用：夜晚全灭", "power-heartbeat": "已套用：全灭 + 电源灯心跳",
                   "power-only": "已套用：只灭电源灯", "disk-only": "已套用：只灭硬盘灯",
                   "none": "已关闭定时"}[p]

        elif path == "/api/schedule_set":
            grp = body.get("group")
            state = body.get("state")
            if grp not in ("power", "disk") or state not in ("on", "off"):
                return self._json(False, "参数不合法")
            wins = (body.get("windows") or "").strip()
            if wins and not WIN_RE.match(wins):
                return self._json(False, "时间段格式不合法（示例 22:00-07:00,13:00-14:00）")
            bad = bad_window(wins)
            if bad:
                return self._json(False, bad)
            if wins:
                cmd = '%s schedule set %s %s "%s"' % (LEDCTL, grp, state, wins)
            else:
                # 传空串 = 显式清空时间段（原来"清空输入框再保存"会保留旧值，永远删不掉）
                cmd = '%s schedule set %s %s ""' % (LEDCTL, grp, state)
            who = "电源灯" if grp == "power" else "硬盘灯"
            msg = ("%s时间表已" % who) + ("启用" if state == "on" else "停用")
            if state == "on" and not wins:
                msg += "（时间段为空，不会生效）"

        elif path == "/api/param":
            items = body.get("items") or {}
            allow = {"heartbeat-ms": (500, 20000), "heartbeat-pulse-ms": (20, 500),
                     "blink-ms": (20, 500), "hold-ms": (100, 5000),
                     "alarm-ms": (100, 2000), "threshold-kb": (0, 4096),
                     "disk-breath-ms": (1500, 20000), "disk-breath-min": (0, 90),
                     "disk-breath-step-ms": (40, 2000),
                     "night-hb-ms": (1000, 60000), "night-hb-pulse-ms": (20, 500),
                     "breath-ms": (800, 20000), "breath-min": (0, 90),
                     "breath-max": (10, 100), "flicker-hz": (60, 2000)}
            parts = []
            for k, v in items.items():
                if k not in allow:
                    continue
                try:
                    v = int(v)
                except Exception:
                    continue
                lo, hi = allow[k]
                parts.append("%s param %s %d" % (LEDCTL, k, max(lo, min(hi, v))))
            if not parts:
                return self._json(False, "没有可保存的参数")
            cmd, msg = "; ".join(parts), "灯效参数已保存并生效"

        elif path == "/api/disk_idle":
            v = body.get("mode")
            if v not in ("breath", "heartbeat", "on"):
                return self._json(False, "mode 不合法")
            cmd = "%s disk idle %s" % (LEDCTL, v)
            msg = "空闲灯语 -> " + {"on": "长亮", "breath": "呼吸"}.get(v, "心跳")

        elif path == "/api/disk_breath":
            v = body.get("mode")
            if v not in ("sync", "wave", "phase"):
                return self._json(False, "mode 不合法")
            cmd = "%s disk breath %s" % (LEDCTL, v)
            msg = "呼吸方式 -> " + {"sync": "同步", "wave": "波浪", "phase": "错落"}[v]

        elif path == "/api/disk_mode":
            v = body.get("state")
            if v not in ("on", "heartbeat", "off"):
                return self._json(False, "state 不合法")
            if v == "off":
                cmd, msg = "%s disk switch off" % LEDCTL, "硬盘灯已熄灭（掉盘告警仍会闪）"
            else:
                # 先开总开关（清残留定时状态），再设空闲灯语
                cmd = "%s disk switch on; %s disk idle %s" % (LEDCTL, LEDCTL, v)
                msg = "硬盘灯 -> " + ("常亮" if v == "on" else "心跳（跟随系统）")

        elif path == "/api/disk_switch":
            v = body.get("state")
            if v not in ("on", "off"):
                return self._json(False, "state 不合法")
            cmd = "%s disk switch %s" % (LEDCTL, v)
            msg = "硬盘灯 -> 跟随系统" if v == "on" else "硬盘灯已熄灭（掉盘告警仍会闪）"

        elif path == "/api/probe":
            cmd, msg = "%s probe levels" % LEDCTL, "亮度分级探测完成（看 0 号盘）"

        elif path == "/api/demo":
            try:
                slot = int(body.get("slot", 0))
            except Exception:
                return self._json(False, "槽位不合法")
            mode = body.get("mode", "blink")
            if mode not in ("blink", "solid", "alarm", "off") or not (0 <= slot <= 3):
                return self._json(False, "参数不合法")
            cmd = "%s demo %d %s" % (LEDCTL, slot, mode)
            msg = "槽位 %d 演示：%s" % (slot + 1, {"blink": "闪烁", "solid": "常亮",
                                             "alarm": "告警慢闪", "off": "停止"}.get(mode, mode))

        elif path == "/api/sweep":
            cmd, msg = "%s sweep" % LEDCTL, "四槽依次自检开始"

        elif path == "/api/lights_off":
            cmd, msg = "%s lights-off" % LEDCTL, "已熄灭全部灯（引擎已停止，需手动重新开启）"

        elif path == "/api/engine_restart":
            cmd, msg = "%s engine restart" % LEDCTL, "引擎已重启"

        else:
            return self._send(404, json.dumps({"ok": False, "error": "not found"}))

        ok, out = remote(cmd)
        if ok:
            self._json(True, msg, out)
        else:
            # 失败时绝不能把"已启用/已关闭"这类成功文案回给界面：
            # 历史 bug 就是 ledctl 路径失效 + 回成功文案 = 静默失败
            tail = ""
            for line in (out or "").splitlines()[::-1]:
                if line.strip():
                    tail = line.strip()[:160]
                    break
            self._json(False, "执行失败：" + (tail or "无输出"), out)


PAGE = PAGE.replace("__FAVICON_B64__", FAVICON_B64)


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("灯控面板已启动(容器版): 0.0.0.0:%d" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
