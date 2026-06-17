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

PHONE_LIMIT    = 20
PHONE_COOLDOWN = 120
phone_usage: dict = {}
phone_lock = threading.Lock()

def check_and_consume(phone: str, requested: int):
    now = time.time()
    with phone_lock:
        info = phone_usage.get(phone, {"count": 0, "reset_at": now + PHONE_COOLDOWN})
        if now >= info["reset_at"]:
            info = {"count": 0, "reset_at": now + PHONE_COOLDOWN}
        remaining = PHONE_LIMIT - info["count"]
        if remaining <= 0:
            wait = int(info["reset_at"] - now)
            m, s = wait // 60, wait % 60
            return 0, f"⏳ Số {phone[:3]}***{phone[-3:]} đang cooldown! Thử lại sau {m}p{s:02d}s"
        allowed = min(requested, remaining)
        info["count"] += allowed
        phone_usage[phone] = info
        return allowed, ""

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

def _spam_loop(session: dict):
    phone  = session["phone"]
    total  = session["total"]
    stop   = session["stop_event"]

    # Tìm file smsv2.py và smsfull.py cùng thư mục server
    scripts = []
    for name in ["smsv2.py", "smsfull.py"]:
        path = os.path.join(BASE_DIR, name)
        if os.path.exists(path):
            scripts.append(path)

    if not scripts:
        session["last_msg"] = "❌ Không tìm thấy smsv2.py hoặc smsfull.py!"
        session["last_ok"]  = False
        session["running"]  = False
        return

    for i in range(total):
        if stop.is_set():
            break
        try:
            # Gọi đúng như sms.py: python smsv2.py <phone> 1
            for script in scripts:
                if stop.is_set():
                    break
                subprocess.Popen(
                    [sys.executable, script, phone, "1"]
                ).wait()

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
    uvicorn.run(app, host="0.0.0.0", port=PORT