import os, uuid, threading, time, subprocess, sys
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@app.get("/")
def root():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

# ── Giới hạn theo số điện thoại ──
PHONE_LIMIT    = 20     # tối đa 20 lần / lượt
PHONE_COOLDOWN = 120    # cooldown 2 phút
phone_usage: dict = {}  # { phone: {"count": int, "reset_at": float} }
phone_lock = threading.Lock()

def check_and_consume(phone: str, requested: int):
    """
    Trả về (allowed, error_msg).
    allowed = số lần thực tế được spam (0 nếu đang cooldown).
    """
    now = time.time()
    with phone_lock:
        info = phone_usage.get(phone, {"count": 0, "reset_at": 0})
        if now >= info["reset_at"]:
            info = {"count": 0, "reset_at": now + PHONE_COOLDOWN}
        
        remaining_quota = PHONE_LIMIT - info["count"]
        if remaining_quota <= 0:
            wait = int(info["reset_at"] - now)
            m, s = wait // 60, wait % 60
            return 0, f"⏳ Số {phone[:3]}***{phone[-3:]} đang cooldown! Thử lại sau {m}p{s:02d}s"
        
        allowed = min(requested, remaining_quota)
        info["count"] += allowed
        phone_usage[phone] = info
        return allowed, ""

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

    allowed, err = check_and_consume(req.phone, req.times)
    if allowed == 0:
        raise HTTPException(429, err)

    sid = str(uuid.uuid4())[:8]
    stop_event = threading.Event()
    session = {
        "id": sid,
        "phone": req.phone,
        "total": allowed,
        "done": 0,
        "running": True,
        "stop_event": stop_event,
        "last_msg": "",
        "last_ok": True,
    }
    sessions[sid] = session

    threading.Thread(target=_spam_loop, args=(session,), daemon=True).start()
    note = f"Giới hạn {PHONE_LIMIT} lần/2 phút — sẽ spam {allowed} lần." if allowed < req.times else ""
    return JSONResponse({"ok": True, "session_id": sid, "allowed": allowed, "note": note})

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

def _run_script(script_name: str, phone: str):
    path = os.path.join(BASE_DIR, script_name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"{script_name} không tìm thấy")
    result = subprocess.run(
        [sys.executable, path, phone, "1"],
        timeout=60,
        capture_output=True,
        text=True
    )
    return result

def _spam_loop(session: dict):
    phone  = session["phone"]
    total  = session["total"]
    stop   = session["stop_event"]
    scripts = [s for s in ["smsv2.py", "smsfull.py"] if os.path.exists(os.path.join(BASE_DIR, s))]

    if not scripts:
        session["last_msg"] = "❌ Không tìm thấy smsv2.py hoặc smsfull.py!"
        session["last_ok"]  = False
        session["running"]  = False
        return

    for i in range(total):
        if stop.is_set():
            break
        try:
            for script in scripts:
                if stop.is_set():
                    break
                _run_script(script, phone)

            session["done"] += 1
            session["last_msg"] = f"✅ Lần {session['done']}/{total} — {phone[:3]}***{phone[-3:]}"
            session["last_ok"]  = True
        except Exception as e:
            session["last_msg"] = f"❌ Lần {i+1} lỗi: {str(e)[:80]}"
            session["last_ok"]  = False

        if not stop.is_set():
            time.sleep(0.3)

    session["running"] = False

if __name__ == "__main__":
    import uvicorn
    PORT = int(os.environ.get("PORT", 8000))
    print(f"🚀 DZI SMS Server — http://localhost:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)