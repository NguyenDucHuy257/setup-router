"""Provisioning logic for one OpenWrt router (no GUI here, so it can be tested).

Pipeline:  probe() -> user reviews/edits Params -> apply() -> build_json() -> file for the web import.
The Tailscale auth key is used once and never written to the JSON or to the router's disk.
"""
from __future__ import annotations

import ipaddress
import json
import secrets
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import paramiko

import remote

SCHEMA = "autodoor-router/1"
VIA6_PREFIX = int(ipaddress.IPv6Address("fd7a:115c:a1e0:b1a::"))
MAX_SITE_ID = 65535
ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I: easy to read aloud
MIN_FREE_KB = 20_000                             # Tailscale needs roughly this much on /overlay
Log = Callable[[str], None]


class ProvisionError(Exception):
    pass


# ---- pure helpers ----------------------------------------------------------------------------

def gen_device_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(8))


def gen_token() -> str:
    return secrets.token_urlsafe(32)


def lan_cidr(ip: str, mask: str | None = None) -> str:
    """'192.168.0.1' + '255.255.255.0' (or '192.168.0.1/24') -> '192.168.0.0/24'."""
    text = ip if "/" in ip else f"{ip}/{mask or '255.255.255.0'}"
    return str(ipaddress.ip_interface(text).network)


def via6_route(site_id: int, cidr: str) -> str:
    """Tailscale 4via6: fd7a:115c:a1e0:b1a:0:<site id>:<ipv4 hi>:<ipv4 lo>/<96 + prefix>."""
    if not 1 <= site_id <= MAX_SITE_ID:
        raise ValueError(f"Site ID phải từ 1 đến {MAX_SITE_ID}.")
    net = ipaddress.IPv4Network(cidr, strict=True)
    value = VIA6_PREFIX | (site_id << 32) | int(net.network_address)
    return f"{ipaddress.IPv6Address(value)}/{96 + net.prefixlen}"


def tailscale_hostname(device_id: str) -> str:
    return f"ad-{device_id.lower()}"


@dataclass
class Params:
    host: str
    ssh_user: str
    ssh_password: str          # may be empty (fresh OpenWrt, or key login)
    new_password: str          # empty = keep the current one
    auth_key: str
    site_id: int
    device_name: str
    device_id: str = ""
    cidr: str = ""             # LAN to advertise, e.g. 192.168.0.0/24
    tag: str = "tag:site-router"
    heartbeat_url: str = ""    # empty = agent installed but idle
    ssh_public_key: str = ""   # optional: added to dropbear authorized_keys
    telegram_token: str = ""   # optional: the router itself can message a Telegram group
    telegram_chat_id: str = ""
    skip_space_check: bool = False
    token: str = field(default_factory=gen_token)

    @property
    def route(self) -> str:
        return via6_route(self.site_id, self.cidr)


# ---- SSH ------------------------------------------------------------------------------------

class Router:
    """Thin paramiko wrapper. OpenWrt's dropbear has no sftp, so files are streamed to `cat`."""

    def __init__(self, host: str, user: str, password: str):
        self.host, self.user, self.password = host, user, password
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    def connect(self, password: str | None = None) -> None:
        pw = self.password if password is None else password
        try:
            # Empty password: try "none" auth first (fresh OpenWrt), then the PC's keys/agent.
            self.client.connect(self.host, username=self.user, password=pw or None, timeout=12,
                                banner_timeout=12, auth_timeout=12,
                                look_for_keys=not pw, allow_agent=not pw)
        except paramiko.AuthenticationException as exc:
            raise ProvisionError("Sai tài khoản/mật khẩu SSH (hoặc router chưa cho đăng nhập bằng mật khẩu "
                                 "trống: hãy nhập mật khẩu hiện tại).") from exc
        except (OSError, paramiko.SSHException) as exc:
            raise ProvisionError(f"Không kết nối SSH được tới {self.host}: {exc}") from exc

    def close(self) -> None:
        self.client.close()

    def run(self, command: str, timeout: int = 60, stdin: str | None = None) -> tuple[int, str]:
        channel_in, out, err = self.client.exec_command(command, timeout=timeout)[0:3]
        if stdin is not None:
            channel_in.write(stdin)
            channel_in.channel.shutdown_write()
        text = out.read().decode("utf-8", "replace") + err.read().decode("utf-8", "replace")
        return out.channel.recv_exit_status(), text.strip()

    def out(self, command: str, timeout: int = 30) -> str:
        return self.run(command, timeout)[1]

    def put(self, path: str, content: str, mode: str = "644") -> None:
        code, text = self.run(f"cat > {shlex.quote(path)} && chmod {mode} {shlex.quote(path)}", 30,
                              stdin=content.replace("\r\n", "\n"))
        if code != 0:
            raise ProvisionError(f"Không ghi được {path}: {text}")


# ---- step 1: probe ---------------------------------------------------------------------------

def probe(router: Router, log: Log) -> dict:
    log("Đọc thông tin router...")
    release = router.out(". /etc/openwrt_release 2>/dev/null; echo \"$DISTRIB_DESCRIPTION\"")
    if "openwrt" not in release.lower():
        raise ProvisionError(f"Thiết bị này không giống OpenWrt (đọc được: '{release or 'trống'}').")
    board = router.out("ubus call system board 2>/dev/null | jsonfilter -e '@.model'") or "?"
    arch = router.out("uname -m")
    pkg = "apk" if router.out("command -v apk") else ("opkg" if router.out("command -v opkg") else "")
    free_kb = int(router.out("df -k /overlay 2>/dev/null | awk 'NR==2{print $4}'") or 0)
    ip = router.out("uci -q get network.lan.ipaddr").split()[0:1]
    if not ip:
        raise ProvisionError("Không đọc được địa chỉ LAN của router (network.lan.ipaddr).")
    mask = router.out("uci -q get network.lan.netmask") or None
    # The network on the WAN side (e.g. the parent router's LAN where the cameras are) is a valid target too.
    wan_dev = router.out("ip -4 route show default | awk '{for(i=1;i<=NF;i++) if($i==\"dev\"){print $(i+1); exit}}'")
    wan_addr = router.out(f"ip -4 -o addr show dev {shlex.quote(wan_dev)} | awk '{{print $4}}' | head -n 1") if wan_dev else ""
    wan_cidr = lan_cidr(wan_addr) if wan_addr else ""
    missing = []
    if not router.out("command -v tailscale"):
        missing.append("tailscale")
    if not router.out("command -v curl"):
        missing.append("curl")
    if router.run("[ -e /etc/ssl/certs/ca-certificates.crt ] || [ -d /etc/ssl/certs ]")[0] != 0:
        missing.append("ca-bundle")
    internet = router.run("ping -c 1 -W 3 1.1.1.1 >/dev/null 2>&1")[0] == 0
    dns = router.run("nslookup downloads.openwrt.org >/dev/null 2>&1")[0] == 0
    router_epoch = int(router.out("date -u +%s") or 0)
    # MAC of the parent router (stays the same while the router is plugged in at the same place).
    gateway_mac = router.out("gw=$(ip -4 route show default | awk '{print $3; exit}'); "
                             "ip neigh show $gw 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i==\"lladdr\")"
                             "{print $(i+1); exit}}'")
    info = {
        "openwrt": release, "board": board, "arch": arch, "pkgManager": pkg, "freeKb": free_kb,
        "lanIp": ip[0].split("/")[0], "gatewayMac": gateway_mac.lower(), "wanCidr": wan_cidr, "lanCidr": lan_cidr(ip[0], mask),
        "tailscaleInstalled": "tailscale" not in missing, "missing": missing, "internet": internet, "dns": dns,
        "clockSkewSeconds": router_epoch - int(time.time()),
    }
    log(f"  {release} | {board} | {arch} | gói: {pkg or 'KHÔNG CÓ'} | trống {free_kb // 1024} MB")
    log(f"  LAN {info['lanIp']} -> {info['lanCidr']} | mạng phía WAN: {info['wanCidr'] or '?'} | Internet: {'có' if internet else 'KHÔNG'} | "
        f"DNS: {'có' if dns else 'KHÔNG'} | Thiếu gói: {', '.join(missing) or 'không'}")
    return info


# ---- step 2: apply ---------------------------------------------------------------------------

def _step(log: Log, text: str) -> None:
    log(f"▶ {text}")


def _run(router: Router, log: Log, command: str, what: str, timeout: int = 180, ok: tuple[int, ...] = (0,)) -> str:
    code, text = router.run(command, timeout)
    if code not in ok:
        raise ProvisionError(f"{what} thất bại (mã {code}): {text[-400:]}")
    return text


def validate(params: Params, info: dict) -> list[str]:
    """Problems that must be fixed before applying (empty list = good)."""
    problems = []
    if not params.auth_key.startswith("tskey-"):
        problems.append("Auth key phải bắt đầu bằng 'tskey-' (tạo ở Tailscale > Settings > Keys).")
    if not 1 <= params.site_id <= MAX_SITE_ID:
        problems.append(f"Site ID phải từ 1 đến {MAX_SITE_ID}.")
    if not params.device_name.strip():
        problems.append("Chưa nhập tên thiết bị.")
    try:
        ipaddress.IPv4Network(params.cidr, strict=True)
    except ValueError:
        problems.append("CIDR LAN không hợp lệ (ví dụ 192.168.0.0/24, phần host phải là .0).")
    if not params.tag.startswith("tag:"):
        problems.append("Tag phải bắt đầu bằng 'tag:'.")
    if not info["pkgManager"]:
        problems.append("Router không có opkg/apk nên không cài được gói.")
    if info["missing"]:
        if not (info["internet"] and info["dns"]):
            problems.append("Router chưa ra được Internet/DNS nên không tải được gói Tailscale.")
        if info["freeKb"] < MIN_FREE_KB and not params.skip_space_check:
            problems.append(f"Bộ nhớ flash trống chỉ {info['freeKb'] // 1024} MB (cần ~{MIN_FREE_KB // 1024} MB). "
                            "Tick 'Bỏ qua kiểm tra dung lượng' nếu vẫn muốn thử.")
    if params.new_password and len(params.new_password) < 8:
        problems.append("Mật khẩu mới nên có ít nhất 8 ký tự.")
    return problems


def apply(router: Router, params: Params, info: dict, log: Log, login: bool = True) -> dict:
    """Install and configure everything. Returns the facts gathered at the end."""
    if abs(info["clockSkewSeconds"]) > 300:
        _step(log, "Đồng hồ router lệch, chỉnh theo giờ máy tính (cần cho HTTPS)")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        _run(router, log, f"date -u -s '{now}'", "Chỉnh giờ", 15)

    if info["missing"]:
        _step(log, f"Cài gói còn thiếu: {', '.join(info['missing'])} (có thể mất 1-3 phút)")
        pkgs = " ".join(info["missing"])
        cmd = f"apk update && apk add {pkgs}" if info["pkgManager"] == "apk" else f"opkg update && opkg install {pkgs}"
        _run(router, log, cmd, "Cài gói", 420)
    else:
        _step(log, "Đủ gói (tailscale, curl, ca-bundle), bỏ qua bước cài")

    _step(log, "Cấu hình mạng/tường lửa cho Tailscale (interface tailscale0, forward tailscale <-> lan)")
    uci = "\n".join([
        "uci -q delete network.tailscale; uci -q delete firewall.tailscale",
        "uci -q delete firewall.ad_ts_lan; uci -q delete firewall.ad_lan_ts",
        "uci set network.tailscale=interface",
        "uci set network.tailscale.proto='none'",
        "uci set network.tailscale.device='tailscale0'",
        "uci set firewall.tailscale=zone",
        "uci set firewall.tailscale.name='tailscale'",
        "uci set firewall.tailscale.network='tailscale'",
        "uci set firewall.tailscale.input='ACCEPT'",
        "uci set firewall.tailscale.output='ACCEPT'",
        "uci set firewall.tailscale.forward='ACCEPT'",
        "uci set firewall.tailscale.masq='1'",
        "uci set firewall.tailscale.mtu_fix='1'",
        "uci set firewall.ad_ts_lan=forwarding; uci set firewall.ad_ts_lan.src='tailscale'; "
        "uci set firewall.ad_ts_lan.dest='lan'",
        "uci set firewall.ad_lan_ts=forwarding; uci set firewall.ad_lan_ts.src='lan'; "
        "uci set firewall.ad_lan_ts.dest='tailscale'",
        "uci commit network; uci commit firewall",
        "/etc/init.d/network reload >/dev/null 2>&1; /etc/init.d/firewall reload >/dev/null 2>&1",
        "uci -q set tailscale.settings.fw_mode='nftables' 2>/dev/null && uci commit tailscale || true",
    ])
    _run(router, log, uci, "Cấu hình mạng", 60)

    _step(log, "Bật Tailscale tự chạy khi khởi động")
    _run(router, log, "/etc/init.d/tailscale enable && /etc/init.d/tailscale restart", "Bật dịch vụ tailscale", 60)
    time.sleep(4)

    if not login:
        _step(log, "(chế độ thử) bỏ qua đăng nhập Tailscale")
        return install_files(router, params, log, login=False)
    _step(log, "Đăng nhập tailnet bằng auth key và advertise route 4via6")
    hostname = tailscale_hostname(params.device_id)
    flags = (f"--hostname={shlex.quote(hostname)} --advertise-routes={shlex.quote(params.route)} "
             f"--advertise-tags={shlex.quote(params.tag)} --accept-dns=false --accept-routes=false --reset")
    router.put("/tmp/.ad_authkey", params.auth_key + "\n", "600")
    try:
        code, text = router.run(f"tailscale up --auth-key=file:/tmp/.ad_authkey {flags}", 90)
        if code != 0 and ("file:" in text or "invalid" in text.lower() or "unknown" in text.lower()):
            log("  (bản tailscale cũ không đọc key từ file, dùng cách truyền trực tiếp)")
            code, text = router.run(f"tailscale up --auth-key={shlex.quote(params.auth_key)} {flags}", 90)
    finally:
        router.run("rm -f /tmp/.ad_authkey", 10)
    if code != 0:
        hint = " Kiểm tra: key còn hạn? tag đã khai trong ACL (tagOwners)?" if "tag" in text.lower() or "key" in text.lower() else ""
        raise ProvisionError(f"tailscale up thất bại: {text[-400:]}{hint}")

    return install_files(router, params, log, login=True)


def install_files(router: Router, params: Params, log: Log, login: bool) -> dict:
    _step(log, "Cài watchdog, hotplug, agent heartbeat và cron")
    router.run("mkdir -p /etc/autodoor && chmod 700 /etc/autodoor", 10)
    conf = (f"DEVICE_ID={shlex.quote(params.device_id)}\nTOKEN={shlex.quote(params.token)}\n"
            f"URL={shlex.quote(params.heartbeat_url)}\nSITE_ID={params.site_id}\n"
            f"NAME={shlex.quote(params.device_name)}\n")
    router.put("/etc/autodoor/agent.conf", conf, "600")
    for path, (content, mode) in remote.FILES.items():
        router.put(path, content, mode)
    cron = "\n".join(remote.CRON_LINES)
    _run(router, log,
         "touch /etc/crontabs/root; grep -v autodoor- /etc/crontabs/root > /tmp/.ad_cron; "
         f"printf '%s\\n' {shlex.quote(cron)} >> /tmp/.ad_cron; cp /tmp/.ad_cron /etc/crontabs/root; "
         "rm -f /tmp/.ad_cron; /etc/init.d/cron enable; /etc/init.d/cron restart", "Cài cron", 30)

    if params.telegram_token.strip() and params.telegram_chat_id.strip():
        _step(log, "Lưu cấu hình Telegram (router tự báo khi phải restart Tailscale)")
        router.put("/etc/autodoor/telegram.conf",
                   f"TG_TOKEN={shlex.quote(params.telegram_token.strip())}\n"
                   f"TG_CHAT={shlex.quote(params.telegram_chat_id.strip())}\n", "600")
    if params.ssh_public_key.strip():
        _step(log, "Thêm SSH public key của máy chủ vào dropbear")
        key = params.ssh_public_key.strip()
        _run(router, log, f"mkdir -p /etc/dropbear; touch /etc/dropbear/authorized_keys; "
             f"grep -qxF {shlex.quote(key)} /etc/dropbear/authorized_keys || "
             f"echo {shlex.quote(key)} >> /etc/dropbear/authorized_keys; chmod 600 /etc/dropbear/authorized_keys",
             "Thêm SSH key", 20)

    if not login:
        return collect(router, params, log)
    _step(log, "Chờ Tailscale lên trạng thái Running (tối đa 40 giây)")
    state = ""
    for _ in range(20):
        state = router.out("tailscale status --json 2>/dev/null | jsonfilter -e '@.BackendState'")
        if state == "Running":
            break
        time.sleep(2)
    log(f"  Trạng thái Tailscale: {state or 'không đọc được'}")
    if state != "Running":
        raise ProvisionError("Tailscale chưa Running. Xem 'logread | grep tailscale' trên router.")

    if params.new_password:
        _step(log, "Đặt mật khẩu mới cho tài khoản SSH (làm cuối cùng)")
        _run(router, log, f"printf '%s\\n%s\\n' {shlex.quote(params.new_password)} "
             f"{shlex.quote(params.new_password)} | passwd {shlex.quote(params.ssh_user)}", "Đổi mật khẩu", 20)
    return collect(router, params, log)


def collect(router: Router, params: Params, log: Log) -> dict:
    _step(log, "Thu thập thông tin để xuất JSON")
    status = router.out("tailscale status --json 2>/dev/null")
    try:
        data = json.loads(status)
    except ValueError:
        data = {}
    me = data.get("Self") or {}
    public_ip = router.out("curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null "
                           "|| curl -fsS --max-time 10 https://ifconfig.me 2>/dev/null")
    fingerprint = router.out("dropbearkey -y -f /etc/dropbear/dropbear_ed25519_host_key 2>/dev/null "
                             "| grep -i fingerprint || dropbearkey -y -f /etc/dropbear/dropbear_rsa_host_key "
                             "2>/dev/null | grep -i fingerprint")
    https_luci = router.out("uci -q get uhttpd.main.listen_https") or ""
    redirect = router.out("uci -q get uhttpd.main.redirect_https") or "0"
    pass_auth = router.out("uci -q get dropbear.@dropbear[0].PasswordAuth") or "on"
    ssh_port = router.out("uci -q get dropbear.@dropbear[0].Port") or "22"
    return {
        "publicIp": public_ip if public_ip.replace(".", "").isdigit() else "",
        "tailscale": {
            "version": router.out("tailscale version | head -n 1"),
            "state": data.get("BackendState"),
            "hostname": me.get("HostName"),
            "dnsName": (me.get("DNSName") or "").rstrip("."),
            "ips": me.get("TailscaleIPs") or [],
            "tags": me.get("Tags") or [],
            "keyExpiry": me.get("KeyExpiry"),
            "advertisedRoutes": [params.route],
            "approvedRoutes": me.get("PrimaryRoutes") or [],
            "health": data.get("Health") or [],
        },
        "security": {
            "tunnel": "WireGuard (Noise IK, ChaCha20-Poly1305) do Tailscale quản lý",
            "tailscaleKeyExpires": bool(me.get("KeyExpiry")),
            "sshPort": ssh_port,
            "sshPasswordAuth": pass_auth != "off",
            "sshHostKey": fingerprint,
            "luciHttps": bool(https_luci),
            "luciRedirectToHttps": redirect == "1",
            "firewallZoneTailscale": True,
            "heartbeat": "HTTPS + Bearer token" if params.heartbeat_url.startswith("https://")
                         else ("KHÔNG an toàn (không phải https)" if params.heartbeat_url else "chưa bật"),
        },
    }


def build_json(params: Params, info: dict, facts: dict) -> dict:
    """The file the web imports. The auth key is deliberately absent; the agent token is included once
    (the web must store only its hash)."""
    return {
        "schema": SCHEMA,
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "device": {
            "id": params.device_id, "name": params.device_name.strip(), "siteId": params.site_id,
            "model": info["board"], "arch": info["arch"], "openwrt": info["openwrt"],
            "packageManager": info["pkgManager"],
        },
        "network": {
            "lanIp": info["lanIp"], "lanCidr": params.cidr, "via6Route": params.route,
            "publicIp": facts["publicIp"], "gatewayMac": info.get("gatewayMac") or None,
        },
        "tailscale": facts["tailscale"],
        "security": facts["security"],
        "telegram": {"configured": bool(params.telegram_token.strip() and params.telegram_chat_id.strip())},
        "agent": {"token": params.token, "heartbeatUrl": params.heartbeat_url, "intervalSeconds": 60,
                  "commands": ["restart_tailscale", "ifup_wan", "reboot"]},
    }
