#!/bin/sh
# Tap-test the Tea panel on the real kiosk, then put the kiosk back exactly as it runs normally.
# Run detached so a dropped SSH can't leave the kiosk in debug mode:
#   setsid -f sh /tmp/kiosk_test_run.sh > /tmp/kiosk_run.log 2>&1
# Needs /tmp/kiosk_cdp.py and /tmp/kiosk_test.json. The launch flags are the production ones from
# ~/.config/autostart/kitchen-dashboard.desktop (see HANDOVER.md, "Restart Chromium Kiosk").
ENV="env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 XDG_SESSION_TYPE=wayland"
FLAGS="--js-flags= --kiosk --noerrdialogs --disable-infobars --no-first-run --touch-events=enabled --enable-pinch --ignore-certificate-errors --unsafely-treat-insecure-origin-as-secure=https://kitchen.local --password-store=basic --use-mock-keychain"
URL=https://kitchen.local/dashboard/

echo "$(date +%T) relaunching the kiosk with DevTools on localhost:9222"
pkill -9 chromium; sleep 2
setsid -f $ENV chromium $FLAGS --remote-debugging-port=9222 '--remote-allow-origins=*' $URL > /tmp/chromium-debug.log 2>&1 < /dev/null
for i in $(seq 1 60); do curl -s http://127.0.0.1:9222/json | grep -q '"title": "Kitchen Hub"' && break; sleep 1; done
echo "$(date +%T) debug kiosk up after ${i}s"

python3 /tmp/kiosk_cdp.py /tmp/kiosk_test.json
echo "steps exit=$?"

echo "$(date +%T) restoring the production kiosk"
pkill -9 chromium; sleep 2
setsid -f $ENV chromium $FLAGS $URL > /tmp/chromium.log 2>&1 < /dev/null
for i in $(seq 1 60); do
  [ "$(pgrep -fa chromium | grep -c -- --type=renderer)" -ge 1 ] && [ "$(pgrep -fa chromium | grep -c -- --type=gpu-process)" -ge 1 ] && break
  sleep 1
done
echo "$(date +%T) production kiosk: renderer=$(pgrep -fa chromium | grep -c -- --type=renderer) gpu=$(pgrep -fa chromium | grep -c -- --type=gpu-process) debug-port-listening=$(ss -ltn | grep -c ':9222 ')"
echo DONE
