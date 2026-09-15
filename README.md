# tieniuled — NAS 机箱灯控中心（铁牛OS / 铁牛NAS / ZeroNAS）

> 单容器、零依赖的 NAS 机箱灯控面板：把电源灯与硬盘灯从"厂商固定灯语"变成**可控可调**。

纯 Python 标准库实现（无需 pip 安装任何包），提供一个可视化 Web 控制台（默认 8977 端口），
通过直写 Super I/O 寄存器与 SATA 控制器 LED 寄存器驱动机箱指示灯。已在 Centerm Zero 1 Pro
上长期实机运行，适用于任何带 Docker 的 x86 NAS —— **铁牛OS（铁牛NAS / ZeroNAS）**、
群晖 DSM、威联通 QTS、TrueNAS、UNRAID、PVE 及自建 x86 NAS 均可尝试。

## 功能特性

- **电源灯三态**：常亮 / 呼吸（周期与最低亮度可调）/ 熄灭。
- **硬盘灯多灯语**：跟随系统读写灯语 / 常亮 / 心跳 / 呼吸 / 熄灭。
- **夜间计划**：按时间段自动切换白天 / 夜间方案，电源灯与硬盘灯各自独立时间表。
- **常用方案一键切换**：预设方案一键套用，改完即存。
- **Web 面板**：3 秒实时状态轮询，带灯效实时预览，手机电脑都能用。
- **配置持久化**：所有设置写在 `data/`，容器升级重建不丢。
- **写操作可靠**：参数双端校验、保存串行化、配置读写加文件锁，失败明确回报而不是静默。
- **运维自检**：`tools/verify.py` 一键检查面板与灯控状态，`tools/register_icon.py` 一键注册桌面图标。

## 适用机型与系统

在 **Centerm Zero 1 Pro** 上开发并长期实机运行，已验证 / 可用的平台：

| 平台 / 系统 | 支持情况 |
|---|---|
| **铁牛OS · 铁牛NAS · ZeroNAS**（Centerm Zero 系列） | ⭐ 原生支持：Docker Compose 一键部署、`tools/register_icon.py` 一键注册桌面图标、应用中心信息登记 |
| Centerm Zero 1 Pro | ✅ 实测机型：电源灯（Super I/O 端口 0xA01 bit1）与硬盘灯（SATA 控制器 BAR0 LED 寄存器）均已打通 |
| 群晖 DSM / 威联通 QTS / TrueNAS / UNRAID / PVE / 自建 x86 NAS | ⚠️ 同为该 Super I/O 方案即可用；不同主板灯控寄存器可能不同，需按"常见问题"自行确认 |

> 搜索关键词：铁牛 NAS、铁牛NAS、铁牛OS、tieniu nas、ZeroNAS 灯控、NAS 电源灯、NAS 硬盘灯、
> 机箱指示灯控制、硬盘灯定时熄灭、硬盘灯关闭、LED 灯语、Super I/O 灯控。

## 环境要求

- x86 NAS，已安装 Docker / Docker Compose
- 需要 **root / privileged** 权限（引擎直写 I/O 端口）+ **host 网络**
- 主板 LED 由 Super I/O 或 SATA 控制器控制——这既是能直接驱动灯的原因，也是能与厂商风扇控制共存的方式

## 包内容

| 文件 | 说明 |
|---|---|
| `app/led_engine.py` | 灯控引擎：呼吸调制 / 灯语复刻 / 夜间计划状态机 |
| `app/ledctl.py` | 配置与状态管理 CLI（面板后端同源，也可单独命令行调用） |
| `app/panel_nas.py` | Web 面板（标准库 HTTP，无第三方依赖） |
| `app/run.sh` | 容器入口：先起引擎，再起面板 |
| `docker-compose.yml` | Compose 编排（特权 + host 网络 + `/sys` 挂载 + 时区） |
| `tools/register_icon.py` | 注册铁牛OS / ZeroNAS 桌面"灯控中心"图标 + 快捷方式 |
| `tools/verify.py` | 安装 / 重启后一键验证 |
| `CHANGELOG.md` | 版本更新记录 |
| `LICENSE` | MIT 开源许可 |

## 安装步骤（铁牛OS / ZeroNAS）

### 1. 创建 Compose 项目
ZeroNAS 网页 → Docker → Compose 项目 → 新建，项目名 `nas-tieniuled`，
把 `docker-compose.yml` 的内容粘贴进去。

### 2. 上传程序文件
ZeroNAS 文件管理器，把 `app/` 整个目录（4 个文件）上传到
`docker-projects/nas-tieniuled/` 目录（即 Compose 项目目录）。

### 3. 启动容器
在 Compose 项目里启动。日志出现下面这行即为成功：
```
灯控面板已启动(容器版): 0.0.0.0:8977
```

### 4. 页面配置
浏览器打开 `http://NAS_IP:8977`：

1. 「电源灯」选常亮或呼吸（呼吸可调周期与最低亮度）；
2. 「硬盘灯」按喜好选灯语；**想让定时看得出效果，总开关要选「跟随系统」**；
3. 「夜间计划」启用时间段，形如 `22:00-07:00`，多个用逗号分隔，保存即生效；
4. 页头会显示当前是否处于夜间时段，以及下一次切换时间。

### 5. 注册桌面图标（铁牛OS / ZeroNAS）
在你自己的电脑上执行（需要 Python 3）：
```
python tools/register_icon.py http://NAS_IP:8977
```
自动备份 `appstore.db`（.bak-tieniuled）、注册应用、为桌面用户添加快捷方式。
刷新 ZeroNAS 桌面即可看到「灯控中心」图标，点击直接打开面板。

### 6. 验证
```
python tools/verify.py http://NAS_IP:8977
```
检查面板可达性、灯控硬件状态（`hw_ok`）、电源灯 / 硬盘灯当前模式与夜间计划。以后每次重启完也可跑一遍。

## 为什么不用装进应用中心也能用

引擎与面板都在容器内运行，配置写在 `data/`，升级就是替换 `app/` 后 `docker compose up -d`——
不需要往宿主机塞 systemd 服务或脚本，也不会和厂商的风扇 / 灯控服务抢资源。
如果你更习惯应用中心入口，也可以把它打成 `.tpk` 走应用商店那条路，本仓库的 Docker 方式与它互不影响。

## 常见问题

**面板能打开，但点设置没反应？** → v1.0.4 及以前的已知问题：面板里的 `ledctl` 路径被写死成宿主机路径，
容器内不存在，导致 12 个写接口全部静默失败（界面还照弹成功文案）。升级到 v1.0.5 及以上即可。

**定了时到点没变化？** → 先看硬盘灯总开关：如果是「熄灭」，定时改的也还是熄灭，
把总开关切到「跟随系统」才看得出效果；面板页头会直接提示这一点。

**时间段填了不生效？** → v1.0.5 起会校验（小时 00-23、分钟 00-59、起止不能相同），填错会直接报错；
更早的版本会静默接受 `25:00-30:00` 这类值，然后永远不生效。

**构建时报 `Client.Timeout exceeded`** → 国内直连 Docker Hub 超时导致基础镜像拉不下来。
compose 里默认已走国内加速源；若该源也不可用，换注释里的其他加速源再构建，海外网络可换回官方
`python:3.12-slim-bookworm`。

**为什么必须特权容器 + host 网络？** → 引擎要直接写 I/O 端口与 SATA 控制器寄存器，
没有特权拿不到 `ioperm`；host 网络则保证面板端口直接可用。

**会不会影响风扇调速？** → 不会。本项目只碰 LED 相关寄存器，不做风扇控制
（那是 [nas-fanctl](https://github.com/hahaha-9527/nas-fanctl) 的事），两者可以同时跑。

**换主板 / 换机型还能用吗？** → 需要确认新主板的灯控寄存器。先跑 `tools/verify.py` 看
`hw_ok` 是否为 true、各灯是否可写；不同 Super I/O 方案的端口不同，需要按实际调整引擎里的地址。

**升级怎么操作？** → 替换 `app/` 里的文件，`docker compose up -d` 重建容器即可，`data/` 不受影响。

## 版本记录

各版本的新增与修复详见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

[MIT](LICENSE) © 2026 西了个瓜

> 本项目通过直接操作硬件寄存器控制机箱指示灯，仅在个人设备上用于学习与定制。
> 不同主板的灯控寄存器可能不同，上机前建议先用 `tools/verify.py` 确认状态，风险自担。
