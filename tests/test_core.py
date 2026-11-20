import ipaddress, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
import core, remote

def test_via6_known_value():
    assert core.via6_route(7, "192.168.1.0/24") == "fd7a:115c:a1e0:b1a:0:7:c0a8:100/120"

def test_via6_roundtrip_with_app_parser():
    sys.path.insert(0, r"D:/PM/auto-door/backend")
    try:
        from app.adapters.tailscale import parse_route
    except Exception:
        pytest.skip("auto-door backend not importable")
    for site, cidr in [(1, "192.168.0.0/24"), (65535, "10.1.2.0/24"), (300, "172.16.0.0/16")]:
        sid, lan = parse_route(core.via6_route(site, cidr))
        assert (sid, lan) == (site, cidr)

def test_via6_rejects_bad_input():
    with pytest.raises(ValueError): core.via6_route(0, "192.168.1.0/24")
    with pytest.raises(ValueError): core.via6_route(70000, "192.168.1.0/24")
    with pytest.raises(ValueError): core.via6_route(1, "192.168.1.5/24")

def test_lan_cidr():
    assert core.lan_cidr("192.168.0.1", "255.255.255.0") == "192.168.0.0/24"
    assert core.lan_cidr("192.168.3.1/24") == "192.168.3.0/24"

def test_device_id():
    ids = {core.gen_device_id() for _ in range(200)}
    assert len(ids) == 200 and all(len(i) == 8 and i.isalnum() and i.upper() == i for i in ids)

def test_json_has_no_auth_key():
    p = core.Params("h", "root", "", "", "tskey-auth-SECRET", 7, "Tên", "ABCD2345", "192.168.1.0/24")
    info = dict(board="b", arch="a", openwrt="OpenWrt", pkgManager="opkg", lanIp="192.168.1.1")
    out = core.build_json(p, info, {"publicIp": "1.2.3.4", "tailscale": {}, "security": {}})
    assert "SECRET" not in str(out) and out["network"]["via6Route"].endswith("/120")
    assert out["telegram"] == {"configured": False}
    p.telegram_token, p.telegram_chat_id = "123:BOTSECRET", "-100"
    info["gatewayMac"] = "00:1d:aa:78:49:d8"
    out = core.build_json(p, info, {"publicIp": "1.2.3.4", "tailscale": {}, "security": {}})
    assert out["telegram"] == {"configured": True} and "BOTSECRET" not in str(out)
    assert out["network"]["gatewayMac"] == "00:1d:aa:78:49:d8"

@pytest.mark.parametrize("path", list(remote.FILES))
def test_remote_shell_syntax(path):
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n", encoding="utf-8") as f:
        f.write(remote.FILES[path][0]); name = f.name
    r = subprocess.run(["sh", "-n", name], capture_output=True, text=True)
    os.unlink(name)
    assert r.returncode == 0, r.stderr
