#!/usr/bin/env python3
"""
DZI SMS TOOL SERVER
pip install fastapi uvicorn
"""

import os, uuid, threading, subprocess, time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def root():
    return FileResponse("index.html")

# ── Session store ──
sessions: dict = {}

class StartReq(BaseModel):
    phone: str
    times: int

@app.post("/api/sms/start")
def sms_start(req: StartReq):
    if not req.phone.isdigit() or len(req.phone) != 10:
        raise HTTPException(400, "Số điện thoại không hợp lệ")
    if req.times < 1 or req.times > 9999:
        raise HTTPException(400, "Số lần không hợp lệ")

    sid = str(uuid.uuid4())[:8]
    stop_event = threading.Event()
    session = {
        "id": sid,
        "phone": req.phone,
        "total": req.times,
        "done": 0,
        "running": True,
        "stop_event": stop_event,
        "last_msg": "",
        "last_ok": True,
    }
    sessions[sid] = session

    threading.Thread(target=_spam_loop, args=(session,), daemon=True).start()
    return JSONResponse({"ok": True, "session_id": sid})

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

def _run_sms_script(script, phone):
    """Chạy smsv2.py hoặc smsfull.py theo kiểu subprocess như sms.py"""
    path = os.path.join(os.getcwd(), script)
    if not os.path.exists(path):
        raise FileNotFoundError(f"{script} không tồn tại")
    subprocess.Popen(["python", path, phone, "1"]).wait()

def _spam_loop(session: dict):
    phone = session["phone"]
    total = session["total"]
    stop  = session["stop_event"]

    scripts = []
    for s in ["smsv2.py", "smsfull.py"]:
        if os.path.exists(os.path.join(os.getcwd(), s)):
            scripts.append(s)

    if not scripts:
        session["last_msg"] = "❌ Không tìm thấy smsv2.py hoặc smsfull.py!"
        session["last_ok"]  = False
        session["running"]  = False
        return

    for i in range(total):
        if stop.is_set():
            break
        try:
            # Gọi tất cả scripts có sẵn mỗi lần
            for script in scripts:
                if stop.is_set():
                    break
                _run_sms_script(script, phone)

            session["done"] += 1
            session["last_msg"] = f"✅ Lần {i+1}/{total} — {phone[:3]}***{phone[-3:]}"
            session["last_ok"]  = True

        except Exception as e:
            session["last_msg"] = f"❌ Lần {i+1} lỗi: {str(e)}"
            session["last_ok"]  = False

        if stop.is_set():
            break

    session["running"] = False

if __name__ == "__main__":
    import uvicorn
    PORT = int(os.environ.get("PORT", 8000))
    print(f"🚀 DZI SMS Server — http://localhost:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
