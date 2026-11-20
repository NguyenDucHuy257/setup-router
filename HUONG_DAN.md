# Hướng dẫn: cài router OpenWrt vào hệ thống AutoDoor

Gồm 2 phần: **A. làm tay một lần** (tài khoản, ACL, auth key) và **B. làm cho từng router** (chạy `provision.py`).

---

## A. Làm tay một lần

### A1. Tạo tài khoản Tailscale
1. Vào https://login.tailscale.com/start.
2. Tailscale **không có đăng ký bằng email + mật khẩu riêng**. Bạn đăng nhập bằng một tài khoản có sẵn: Google, Microsoft, GitHub, Apple (hoặc passkey). Dùng email bạn định dùng để quản lý (ví dụ Gmail) rồi chọn "Sign in with Google".
3. Lần đầu đăng nhập, Tailscale tự tạo một *tailnet* cho bạn. Tài khoản này là **admin** của tailnet.

### A2. Cài Tailscale trên máy chủ web (nơi chạy backend AutoDoor)
1. Tải bản Windows ở https://tailscale.com/download, cài và đăng nhập **cùng tài khoản** ở A1.
2. Bật nhận route của router con (bắt buộc để thấy các route 4via6):
   - Biểu tượng Tailscale ở khay hệ thống → **Preferences** → tick **Use Tailscale subnets**, hoặc chạy PowerShell: `tailscale set --accept-routes`.
3. Kiểm tra: `tailscale status` chạy được. Backend đọc lệnh này nên máy chủ phải cài `tailscale.exe`.

### A3. Khai báo tag và tự duyệt route trong ACL
Tag giúp router **không bao giờ hết hạn key** và route được duyệt tự động.
1. Vào https://login.tailscale.com/admin/acls → tab **File** (policy file).
2. Thêm (hoặc gộp vào) hai khối sau, giữ nguyên các khối đã có:

```json
"tagOwners": {
  "tag:site-router": ["autogroup:admin"]
},
"autoApprovers": {
  "routes": {
    "fd7a:115c:a1e0:b1a::/64": ["tag:site-router"]
  }
}
```
3. Bấm **Save**. Nếu có lỗi cú pháp, Tailscale báo ngay.
4. Policy mặc định cho phép mọi thiết bị nói chuyện với nhau. Nếu bạn đã siết ACL, hãy thêm quyền cho máy chủ web truy cập các route của `tag:site-router`.

> Lưu ý: dòng `autoApprovers` cho dải 4via6 chưa được kiểm chứng trên tailnet của bạn. Nếu sau khi cài router, route vẫn ở trạng thái **Pending approval**, hãy duyệt tay ở A5 rồi báo lại để chỉnh.

### A4. Tạo auth key
1. Vào https://login.tailscale.com/admin/settings/keys → **Generate auth key…**
2. Chọn:
   | Tuỳ chọn | Giá trị |
   |---|---|
   | Reusable | **Bật** (dùng cho nhiều router) |
   | Expiration | 90 ngày (tối đa) |
   | Ephemeral | **Tắt** (nếu bật, router sẽ bị xoá khi offline) |
   | Pre-approved | **Bật** (nếu có mục này) |
   | Tags | **Add tags → `tag:site-router`** (phải khai ở A3 trước, nếu không sẽ không chọn được) |
3. Bấm **Generate key**, **sao chép ngay** chuỗi `tskey-auth-...` (chỉ hiện một lần).
4. Key chỉ cần lúc cài. Hết hạn thì router đã cài **vẫn chạy bình thường**; chỉ cần tạo key mới khi cài router mới.
5. Key là bí mật: đừng gửi qua chat/email, đừng commit vào git. Tool không ghi key ra JSON hay lên router.

### A5. (Sau khi cài router) kiểm tra trong admin Tailscale
Vào https://login.tailscale.com/admin/machines, tìm máy tên `ad-<mã thiết bị>`:
- Có nhãn **tag:site-router**.
- Không có dòng "Key expires in…" (hoặc ghi *Key expiry disabled*).
- Mục **Subnets**: nếu route 4via6 ở trạng thái chờ → `⋯` → **Edit route settings…** → tick route → Save.

---

## B. Cài cho từng router

### Chuẩn bị
- Cắm máy tính vào mạng LAN của router (hoặc cùng mạng), router **ra được Internet** (cần tải gói Tailscale).
- Python 3.11+ và `pip install -r requirements.txt` (cần paramiko). Tkinter có sẵn cùng Python trên Windows.
- Flash trống tối thiểu ~20 MB (`df -h /overlay`). Router 16 MB flash thường không đủ.
- Có **Site ID** chưa dùng. Web sẽ cấp số này; trước mắt bạn tự ghi sổ các số đã dùng (1–65535, **không trùng**).

### Chạy
```
cd D:\PM\router-provisioner
python provision.py
```
1. **Mục 1, nhập thông tin** rồi nhấn **Enter**:
   - *IP OpenWrt*, *Tài khoản SSH* (thường là `root`), *Mật khẩu hiện tại* (để trống nếu router mới chưa đặt).
   - *Mật khẩu mới*: để trống nếu không đổi. Tool đổi ở bước cuối rồi đăng nhập thử lại để chắc chắn.
   - *Tailscale auth key*: dán `tskey-auth-...` ở A4.
   - *Site ID* và *Tên thiết bị*.
2. Tool dò router (đọc-only) và hiện thông tin: bản OpenWrt, model, flash trống, LAN, Internet/DNS.
3. **Mục 2, điều chỉnh** rồi nhấn **Enter** lần nữa:
   - *Mã thiết bị*: 8 ký tự ngẫu nhiên (sửa được).
   - *LAN cần advertise*: tool đọc từ router (ví dụ router `192.168.0.1` → `192.168.0.0/24`).
   - *Route 4via6*: hiện trực tiếp khi bạn đổi Site ID/CIDR.
   - *URL heartbeat*: để trống nếu web chưa có API (watchdog Tailscale vẫn chạy).
4. Hộp thoại **xác nhận** liệt kê mọi thứ sắp làm → **Yes** để nạp.
5. Tool làm lần lượt: chỉnh giờ (nếu lệch) → cài `tailscale curl ca-bundle` → cấu hình mạng/tường lửa → bật Tailscale tự chạy → `tailscale up` bằng auth key → cài watchdog/hotplug/agent/cron → đổi mật khẩu.
6. Kết quả lưu ở `output\<mã thiết bị>.json`.

### Router sẽ tự làm gì sau khi cài
- Tailscale bật cùng hệ thống; khi WAN lên thì **hotplug** khởi động lại Tailscale sau 8 giây.
- **Watchdog** (mỗi phút): Internet ổn mà Tailscale không Running thì restart; lặp lại thì `ifup wan`; 10 phút vẫn hỏng thì reboot (tối đa 1 lần/giờ). Mất Internet 5 phút thì `ifup wan`.
- **Agent** (mỗi phút): gọi HTTPS về server nếu có URL heartbeat; chỉ chạy 3 lệnh trong danh sách trắng: `restart_tailscale`, `ifup_wan`, `reboot`.

### File JSON
- Chứa: mã thiết bị, tên, Site ID, model, IP public, IP LAN/CIDR/route 4via6, thông tin Tailscale (IP, tag, hạn key), thông tin bảo mật (WireGuard, SSH, LuCI HTTPS, fingerprint host key) và **token agent**.
- **Không chứa** auth key. Token là bí mật: import xong thì xoá/cất kỹ; web chỉ nên lưu hash của token.
- IP public chỉ đúng tại thời điểm cài; trạng thái thật lấy từ heartbeat/Tailscale.

### Kiểm tra sau khi cài
Trên máy chủ web:
```
tailscale status                      # thấy ad-<mã> online
tailscale ping ad-<mã>
ping fd7a:115c:a1e0:b1a:0:<SiteID>:<IP LAN hex>   # ví dụ camera 192.168.1.10 ở Site 7: ...:0:7:c0a8:10a
```
Trên router: `logread | grep -E "tailscale|autodoor"`, `crontab -l`.

### Lỗi thường gặp
| Hiện tượng | Cách xử lý |
|---|---|
| "Sai tài khoản/mật khẩu SSH" | OpenWrt thường dùng tài khoản `root`. Router mới chưa đặt mật khẩu: để trống ô mật khẩu. |
| "Router chưa ra được Internet/DNS" | Kiểm tra cáp WAN, DNS; chạy `ping 1.1.1.1` trên router. |
| Flash trống không đủ | Dùng router lớn hơn, hoặc extroot (USB), hoặc tick bỏ qua kiểm tra nếu chắc chắn. |
| `tailscale up` báo lỗi tag/key | Tag chưa khai ở A3, key hết hạn, hoặc key không gắn tag. Tạo key mới. |
| Route ở trạng thái Pending | Duyệt tay ở A5 hoặc sửa `autoApprovers` ở A3. |
| Tailscale "not running" sau khi khởi động | Watchdog/hotplug tự xử lý trong ~1–2 phút; xem `logread`. |
