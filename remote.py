"""Shell files that are copied onto the OpenWrt router (BusyBox ash, no bash features).

- autodoor-watchdog : every minute; keeps Tailscale running even when it started before the WAN was up.
- hotplug 99-autodoor: when WAN comes up, restart Tailscale a few seconds later.
- autodoor-agent    : every minute; HTTPS heartbeat to the web server and a small whitelist of remote commands.
"""

WATCHDOG = r"""#!/bin/sh
# AutoDoor watchdog (installed by router-provisioner). Runs from cron every minute.
STATE=/tmp/autodoor-watchdog
mkdir -p "$STATE"
LOG="logger -t autodoor-watchdog"

get() { cat "$STATE/$1" 2>/dev/null || echo 0; }
put() { echo "$2" > "$STATE/$1"; }

online() {
    ping -c 1 -W 3 1.1.1.1 >/dev/null 2>&1 || ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1
}

state() {
    tailscale status --json 2>/dev/null | jsonfilter -e '@.BackendState' 2>/dev/null
}

if online; then
    put offline 0
    st=$(state)
    case "$st" in
        Running) put bad 0; exit 0 ;;
        NeedsLogin|NeedsMachineAuth)
            # Restarting cannot fix this (key removed/expired, or the admin must approve): never reboot for it.
            if [ "$(get warned)" -eq 0 ]; then $LOG "tailscale state $st: needs action in the Tailscale admin console"; put warned 1; fi
            exit 0 ;;
    esac
    put warned 0
    bad=$(( $(get bad) + 1 ))
    put bad "$bad"
    $LOG "internet ok but tailscale is '${st:-not responding}' (count $bad)"
    [ "$bad" -eq 1 ] && autodoor-notify "Tailscale không chạy (${st:-không phản hồi}), đang restart." &
    /etc/init.d/tailscale restart >/dev/null 2>&1
    if [ "$bad" -ge 3 ] && [ $((bad % 3)) -eq 0 ]; then
        $LOG "tailscale still down: ifup wan"
        ifup wan >/dev/null 2>&1
    fi
    # Last resort: reboot after 10 minutes, but never when the router has been up less than an hour,
    # so a persistent fault cannot cause a reboot loop (uptime resets on reboot; /tmp does too).
    if [ "$bad" -ge 10 ] && [ "$(cut -d. -f1 /proc/uptime)" -ge 3600 ]; then
        $LOG "tailscale down for $bad minutes: reboot"
        autodoor-notify "Tailscale hỏng $bad phút, router sắp khởi động lại."
        sleep 2; reboot
    fi
else
    off=$(( $(get offline) + 1 ))
    put offline "$off"
    if [ "$off" -ge 5 ] && [ $((off % 5)) -eq 0 ]; then
        $LOG "no internet for $off minutes: ifup wan"
        ifup wan >/dev/null 2>&1
    fi
fi
"""

HOTPLUG = r"""#!/bin/sh
# AutoDoor: restart Tailscale shortly after the WAN interface comes up.
[ "$ACTION" = "ifup" ] || exit 0
case "$INTERFACE" in wan|wan6|wwan) ;; *) exit 0 ;; esac
[ -x /etc/init.d/tailscale ] || exit 0
( sleep 8; logger -t autodoor-hotplug "$INTERFACE up: restarting tailscale"; /etc/init.d/tailscale restart ) >/dev/null 2>&1 &
"""

AGENT = r"""#!/bin/sh
# AutoDoor heartbeat agent. Runs from cron every minute. The router calls the server; nothing listens here.
CONF=/etc/autodoor/agent.conf
[ -r "$CONF" ] || exit 0
. "$CONF"                      # DEVICE_ID, TOKEN, URL
[ -n "$URL" ] && [ -n "$TOKEN" ] || exit 0
LOG="logger -t autodoor-agent"
ACK=/tmp/autodoor-ack

state=$(tailscale status --json 2>/dev/null | jsonfilter -e '@.BackendState' 2>/dev/null)
tsip=$(tailscale ip -4 2>/dev/null | head -n 1)
tsver=$(tailscale version 2>/dev/null | head -n 1)
uptime=$(cut -d. -f1 /proc/uptime)
release=$(. /etc/openwrt_release 2>/dev/null; echo "$DISTRIB_RELEASE")
ack=$(cat "$ACK" 2>/dev/null | tr '\n' ',' | sed 's/,$//')
# MAC of the parent router: the web warns when it changes (the router was plugged in somewhere else).
gw=$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')
gwmac=$(ip neigh show "$gw" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="lladdr"){print $(i+1); exit}}')

body=$(printf '{"deviceId":"%s","uptime":%s,"tailscaleState":"%s","tailscaleIp":"%s","tailscaleVersion":"%s","openwrt":"%s","ack":"%s","gatewayMac":"%s"}' \
    "$DEVICE_ID" "${uptime:-0}" "${state:-Unknown}" "$tsip" "$tsver" "$release" "$ack" "$gwmac")

reply=$(curl -fsS --max-time 20 -X POST \
    -H "Authorization: Bearer $TOKEN" -H "X-Device-Id: $DEVICE_ID" -H "Content-Type: application/json" \
    -d "$body" "$URL" 2>/dev/null) || exit 0
rm -f "$ACK"

# Reply lines: "CMD <id> <name>". Only these names run; anything else is ignored.
echo "$reply" | while read -r kind id name rest; do
    [ "$kind" = "CMD" ] || continue
    case "$id" in *[!A-Za-z0-9_-]*|"") continue ;; esac
    case "$name" in
        restart_tailscale) $LOG "cmd $id: restart tailscale"; /etc/init.d/tailscale restart >/dev/null 2>&1; r=$? ;;
        ifup_wan)          $LOG "cmd $id: ifup wan"; ifup wan >/dev/null 2>&1; r=$? ;;
        reboot)            $LOG "cmd $id: reboot"; echo "$id:0" >> "$ACK"; ( sleep 3; reboot ) >/dev/null 2>&1 & continue ;;
        *)                 $LOG "cmd $id: unknown '$name' ignored"; echo "$id:unknown" >> "$ACK"; continue ;;
    esac
    echo "$id:$r" >> "$ACK"
done
"""

NOTIFY = r"""#!/bin/sh
# AutoDoor: send one Telegram message from the router (only when a bot token + chat id were configured).
# usage: autodoor-notify "text"
[ -r /etc/autodoor/telegram.conf ] || exit 0
. /etc/autodoor/telegram.conf            # TG_TOKEN, TG_CHAT
[ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ] || exit 0
who=$(. /etc/autodoor/agent.conf 2>/dev/null; echo "$NAME ($DEVICE_ID)")
curl -fsS --max-time 15 -d "chat_id=$TG_CHAT" --data-urlencode "text=$who: $1" \
    "https://api.telegram.org/bot$TG_TOKEN/sendMessage" >/dev/null 2>&1
exit 0
"""

FILES = {
    "/usr/bin/autodoor-notify": (NOTIFY, "755"),
    "/usr/bin/autodoor-watchdog": (WATCHDOG, "755"),
    "/etc/hotplug.d/iface/99-autodoor": (HOTPLUG, "755"),
    "/usr/bin/autodoor-agent": (AGENT, "755"),
}

CRON_LINES = [
    "* * * * * /usr/bin/autodoor-watchdog",
    "* * * * * /usr/bin/autodoor-agent",
]
