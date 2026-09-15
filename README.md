# 灯控中心 tieniuled

x86 NAS 的机箱灯控面板：直写 Super I/O 寄存器，把电源灯 / 硬盘灯变成**可控可调**的状态指示。Web 面板，手机电脑都能用。

> 作者：**西了个瓜**

## 功能

- **电源灯**：常亮 / 呼吸（可调周期与最低亮度）/ 熄灭
- **硬盘灯**：跟随系统读写灯语 / 常亮 / 心跳 / 熄灭
- **夜间计划**：按时间段自动切换白天/夜间方案（双时间表）
- **Web 面板**：3 秒实时状态轮询，局域网任意设备浏览器访问
- **配置持久化**：所有设置保存在 `./data/`，容器升级重建不丢

## 硬件要求

- x86 架构 NAS / 软路由（需能直连 Super I/O 的 LED 引脚，Linux 内核）
- **特权容器**（`privileged: true`）+ host 网络：引擎通过 `/sys` 与 I/O 端口直接驱动灯，这是能与厂商风扇控制共存的方式
- 已在铁牛 ZeroNAS（升腾）上验证

## 快速开始

```bash
git clone https://github.com/hahaha-9527/tieniuled.git
cd tieniuled
docker compose up -d
```

然后浏览器打开：

```
http://<NAS的IP>:8977
```

首次启动会生成默认配置 `./data/nas-led.json`，之后所有调整都在网页面板上完成。

## 目录结构

```
├── docker-compose.yml   # 一键部署（特权 + host 网络 + /sys 挂载 + 时区）
├── app/
│   ├── led_engine.py    # 灯控引擎：呼吸调制 / 灯语复刻 / 夜间计划状态机
│   ├── ledctl.py        # 配置与状态管理 CLI（面板后端同源）
│   ├── panel_nas.py     # Web 面板（标准库 HTTP，无第三方依赖）
│   └── run.sh           # 容器入口：先引擎后面板
├── icon.png
└── data/                # 运行时生成：配置 / 状态 / 日志
```

零第三方 Python 依赖——面板和引擎只用标准库，基础镜像 `python:3.12-slim-bookworm`。

## 常见问题

- **端口能改吗？** 改 `docker-compose.yml`（host 网络模式下端口由 `panel_nas.py` 监听，默认 8977）。
- **时区？** 容器默认 `TZ=Asia/Shanghai`，夜间计划按此时区计算，跨机器部署请自行调整。
- **卸载**：`docker compose down`，删除本目录即可；`./data` 是你的全部数据。

## 免责声明

本项目通过直接操作硬件寄存器控制机箱指示灯，仅在个人设备上用于学习与定制。因使用不当造成的任何硬件或软件问题由使用者自行承担。请遵守设备厂商的相关约定。

## License

MIT
