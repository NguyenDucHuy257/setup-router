# router-provisioner

Công cụ cài đặt **một router OpenWrt** vào hệ thống AutoDoor: cài Tailscale, advertise mạng LAN qua route 4via6, cài các dịch vụ tự hồi phục, rồi xuất file JSON để import vào web (tab "Quản lý router", sẽ làm sau).

Hướng dẫn từng bước từ tạo tài khoản Tailscale: **[HUONG_DAN.md](HUONG_DAN.md)**.

## Chạy nhanh

```powershell
cd D:\PM\router-provisioner
pip install -r requirements.txt
python provision.py
```

Giao diện có 3 bước, chuyển bước bằng phím **Enter**:

1. **Nhập thông tin** (IP router, tài khoản SSH, mật khẩu, auth key, Site ID, tên thiết bị) → Enter: tool dò router (chỉ đọc).
2. **Điều chỉnh** (mã thiết bị, LAN cần advertise, tag, URL heartbeat) → Enter → hộp thoại xác nhận → nạp cấu hình.
3. **Lưu JSON** vào `output\<mã thiết bị>.json`.

## Tool làm gì trên router

| Bước | Chi tiết |
|---|---|
| Dò | Bản OpenWrt, model, flash trống, LAN, Internet/DNS, gói còn thiếu (`tailscale`, `curl`, `ca-bundle`) |
| Cài gói | Dùng `apk` (OpenWrt 25.x) hoặc `opkg` (cũ hơn), chỉ cài gói còn thiếu |
| Mạng/tường lửa | Interface `tailscale0`, zone `tailscale`, forward `tailscale <-> lan` (qua `uci`) |
| Tailscale | Bật tự chạy, `tailscale up --auth-key ... --advertise-routes <4via6> --advertise-tags tag:site-router --hostname ad-<mã>` |
| Tự hồi phục | Watchdog + hotplug + cron (bên dưới) |
| Agent | Heartbeat HTTPS mỗi phút (chỉ chạy nếu có URL) |
| Telegram (tuỳ chọn) | Nhập bot token + chat ID nhóm: lưu ở `/etc/autodoor/telegram.conf` (quyền 600); watchdog tự báo vào nhóm khi Tailscale chết/phải reboot (cần Internet). Bỏ trống = không báo từ router; cảnh báo mất kết nối vẫn do web gửi |
| Router cha | Đọc MAC gateway, ghi vào JSON; agent gửi lại mỗi lần heartbeat để web phát hiện router bị chuyển chỗ |
| Mật khẩu | Đổi cuối cùng (nếu nhập), rồi đăng nhập lại để chắc chắn |

Auth key chỉ dùng một lần: ghi tạm vào `/tmp` (quyền 600) rồi xoá, **không** nằm trong JSON và không lưu trên router.

## Route 4via6

```
fd7a:115c:a1e0:b1a:0:<Site ID>:<IP LAN dạng hex>/<96 + độ dài prefix>
Ví dụ Site 7 + 192.168.1.0/24  ->  fd7a:115c:a1e0:b1a:0:7:c0a8:100/120
```

Site ID (1–65535) **không được trùng** giữa các router. Công thức khớp với parser trong backend (`app/adapters/tailscale.py`), có test đối chiếu.

## Router tự hồi phục thế nào

| Thành phần | File trên router | Hành vi |
|---|---|---|
| Hotplug | `/etc/hotplug.d/iface/99-autodoor` | WAN lên → 8 giây sau restart Tailscale (xử lý "Tailscale chạy trước khi có mạng") |
| Watchdog | `/usr/bin/autodoor-watchdog` (cron mỗi phút) | Internet ổn mà Tailscale không `Running` → restart; lặp lại thì `ifup wan`; 10 phút vẫn hỏng **và** router đã chạy ≥ 1 giờ → reboot. Mất Internet 5 phút → `ifup wan`. Trạng thái `NeedsLogin`/`NeedsMachineAuth` chỉ ghi log, không restart/reboot (restart không sửa được) |
| Agent | `/usr/bin/autodoor-agent` (cron mỗi phút) | POST heartbeat; chỉ chạy 3 lệnh: `restart_tailscale`, `ifup_wan`, `reboot`; lệnh khác bị bỏ qua |
| Cấu hình | `/etc/autodoor/agent.conf` (quyền 600) | Mã thiết bị, token, URL heartbeat |

Log trên router: `logread | grep -E "tailscale|autodoor"`.

### Giao thức agent (để làm API phía web)

```
POST <heartbeatUrl>
Authorization: Bearer <token>        X-Device-Id: <mã thiết bị>
{"deviceId","uptime","tailscaleState","tailscaleIp","tailscaleVersion","openwrt","ack"}

Phản hồi (text, mỗi lệnh một dòng):   CMD <id> <restart_tailscale|ifup_wan|reboot>
Lần gọi sau, "ack" = "id:mã,id:mã"   (mã 0 = OK, "unknown" = lệnh bị bỏ qua)
```

IP public của router lấy từ địa chỉ nguồn của request. Web chỉ nên lưu **hash** của token.

## File JSON xuất ra (`autodoor-router/1`)

`device` (mã, tên, Site ID, model, OpenWrt) · `network` (IP LAN, CIDR, route 4via6, IP public lúc cài) · `tailscale` (phiên bản, trạng thái, IP, tag, hạn key, route) · `security` (WireGuard, SSH, LuCI HTTPS, fingerprint host key) · `agent` (token, URL, danh sách lệnh).

⚠ Chứa **token agent**: import xong thì xoá/cất kỹ, đừng gửi lên chat hay commit vào git. Không chứa auth key.

## Đưa vào web

Admin AutoDoor → **Quản lý router** → **Nhập JSON router**, dán nội dung file `output\<mã>.json`, rồi bấm lên bản đồ để đặt vị trí. Chấm xanh = kết nối được, đỏ = không kết nối được, xám = đang trong danh sách bỏ qua ping. Bấm vào chấm để restart Tailscale, quét/đếm camera, xem chi tiết camera, bật/tắt ping định kỳ. Cài bot Telegram ở nút **Cảnh báo Telegram**.

Để lệnh từ xa chạy được cả khi Tailscale ngắt, điền **URL heartbeat** (`https://<máy chủ>/api/v1/router-agent/heartbeat`) lúc chạy tool, hoặc sửa `URL=` trong `/etc/autodoor/agent.conf` trên router. Chưa có URL thì web dùng SSH qua Tailscale (nhập mật khẩu router, không lưu).

## Cấu trúc thư mục

| File | Vai trò |
|---|---|
| `provision.py` | Giao diện Tkinter |
| `core.py` | Dò, nạp, thu thập, dựng JSON (không phụ thuộc GUI) |
| `remote.py` | Script shell chép lên router |
| `tests/test_core.py` | Test: công thức via, đối chiếu parser backend, JSON không lộ key, cú pháp shell |
| `output/` | JSON kết quả (tự tạo) |

Chạy test: `python -m pytest -q tests`.

## Đã kiểm chứng và chưa kiểm chứng

- ✔ Đọc router thật `192.168.3.1` (OpenWrt 25.12.6, `apk`): dò thông tin chạy đúng.
- ✔ Test tự động đạt; watchdog (8 kịch bản) và agent đã chạy thử với lệnh giả lập trên máy tính.
- ✘ **Chưa nạp lên router thật**: chưa kiểm chứng `apk add`, cấu hình `uci`, `tailscale up`, tự lên lại sau mất điện.
- ✘ Dòng `autoApprovers` cho dải 4via6 chưa kiểm chứng; nếu route vẫn "Pending" thì duyệt tay trong admin Tailscale.
- ✘ Phía web (tab Quản lý router, API heartbeat, bản đồ) **chưa làm**; để trống URL heartbeat cho đến khi có API.

## Gỡ cài đặt trên router

```sh
tailscale logout; /etc/init.d/tailscale stop
sed -i '/autodoor-/d' /etc/crontabs/root; /etc/init.d/cron restart
rm -f /usr/bin/autodoor-watchdog /usr/bin/autodoor-agent /etc/hotplug.d/iface/99-autodoor
rm -rf /etc/autodoor
# Mạng/tường lửa: xoá network.tailscale, firewall.tailscale, firewall.ad_ts_lan, firewall.ad_lan_ts rồi uci commit
```
Nhớ xoá máy tương ứng trong admin Tailscale.
