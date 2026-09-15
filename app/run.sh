#!/bin/sh
# 灯控应用容器入口: 先起引擎, 再起面板(前台)
mkdir -p /data
if [ ! -f /data/nas-led.json ]; then
    if [ -f /etc/nas-led.json ]; then
        cp /etc/nas-led.json /data/nas-led.json
    fi
fi
python3 /opt/app/led_engine.py >>/data/engine.log 2>&1 &
sleep 1
exec python3 /opt/app/panel_nas.py
