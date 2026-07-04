from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
import asyncpg
import os
import hashlib
import httpx
from datetime import datetime, date

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_HASH = "04067de8cf70fc76077836b9f28020fc7214e866437ed7071a66bd9efb450d17"
SALT = "syprate-fixed-salt"
FIXED_TOKEN = hashlib.sha256(f"{ADMIN_HASH}{SALT}".encode()).hexdigest()

# ─── DB Connection ───────────────────────────────────────
async def get_db():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        await conn.close()

# ─── Init Tables ─────────────────────────────────────────
@app.on_event("startup")
async def startup():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # جدول العملات
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rates (
                id SERIAL PRIMARY KEY,
                currency TEXT NOT NULL,
                buy NUMERIC,
                sell NUMERIC,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # جدول الزيارات
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS visits (
                id SERIAL PRIMARY KEY,
                visit_date TEXT NOT NULL UNIQUE,
                count INTEGER DEFAULT 0
            )
        """)
        # جدول الذهب الرسمي
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gold_official (
                id SERIAL PRIMARY KEY,
                bulletin_date DATE NOT NULL,
                bulletin_time TEXT,
                karat_24 NUMERIC,
                karat_21 NUMERIC,
                karat_18 NUMERIC,
                ounce_price NUMERIC,
                lira_gold NUMERIC,
                note TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # جدول الطاقة
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS energy_prices (
                id SERIAL PRIMARY KEY,
                fuel_type TEXT NOT NULL UNIQUE,
                price NUMERIC NOT NULL,
                unit TEXT DEFAULT 'لتر',
                effective_date DATE,
                note TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # إضافة البيانات الأولية للطاقة إذا ما في
        await conn.execute("""
            INSERT INTO energy_prices (fuel_type, price, unit, effective_date)
            VALUES 
                ('بنزين 95', 0, 'لتر', CURRENT_DATE),
                ('بنزين 90', 0, 'لتر', CURRENT_DATE),
                ('مازوت',    0, 'لتر', CURRENT_DATE),
                ('غاز منزلي', 0, 'اسطوانة', CURRENT_DATE),
                ('غاز صناعي', 0, 'اسطوانة', CURRENT_DATE)
            ON CONFLICT (fuel_type) DO NOTHING
        """)
    finally:
        await conn.close()

# ═══════════════════════════════════════════════════════
# ─── PUBLIC APIs ────────────────────────────────────────
# ═══════════════════════════════════════════════════════

# العملات
@app.get("/api/rates")
async def get_rates(db=Depends(get_db)):
    rows = await db.fetch("SELECT currency, buy, sell, updated_at FROM rates ORDER BY id")
    return [dict(r) for r in rows]

# الذهب الرسمي
@app.get("/api/gold/official")
async def get_gold_official(db=Depends(get_db)):
    row = await db.fetchrow("""
        SELECT * FROM gold_official ORDER BY bulletin_date DESC, updated_at DESC LIMIT 1
    """)
    if not row:
        return {"status": "no_data"}
    return dict(row)

# الذهب العالمي (من API مجاني)
@app.get("/api/gold/world")
async def get_gold_world():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://metals.live/api/spot/gold")
            data = r.json()
            price_usd = float(data[0]["price"])  # سعر الأونصة بالدولار
            gram_24 = price_usd / 31.1035
            return {
                "ounce_usd": round(price_usd, 2),
                "karat_24": round(gram_24, 2),
                "karat_22": round(gram_24 * (22/24), 2),
                "karat_21": round(gram_24 * (21/24), 2),
                "karat_18": round(gram_24 * (18/24), 2),
                "updated_at": datetime.now().isoformat(),
                "status": "live"
            }
    except Exception as e:
        return {"status": "error", "message": str(e)}

# الفضة العالمية
@app.get("/api/silver/world")
async def get_silver_world():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://metals.live/api/spot/silver")
            data = r.json()
            price_usd = float(data[0]["price"])
            gram = price_usd / 31.1035
            return {
                "ounce_usd": round(price_usd, 2),
                "silver_999": round(gram, 2),
                "silver_925": round(gram * 0.925, 2),
                "silver_900": round(gram * 0.900, 2),
                "updated_at": datetime.now().isoformat(),
                "status": "live"
            }
    except Exception as e:
        return {"status": "error", "message": str(e)}

# الطاقة
@app.get("/api/energy")
async def get_energy(db=Depends(get_db)):
    rows = await db.fetch("SELECT * FROM energy_prices ORDER BY id")
    return [dict(r) for r in rows]

# الزيارات
@app.post("/api/visit")
async def record_visit(db=Depends(get_db)):
    today = date.today().isoformat()
    await db.execute("""
        INSERT INTO visits (visit_date, count) VALUES ($1, 1)
        ON CONFLICT (visit_date) DO UPDATE SET count = visits.count + 1
    """, today)
    row = await db.fetchrow("SELECT SUM(count) as total FROM visits")
    return {"total": int(row["total"] or 0), "today": today}

# ═══════════════════════════════════════════════════════
# ─── ADMIN APIs ─────────────────────────────────────────
# ═══════════════════════════════════════════════════════

def verify_token(request: Request):
    token = request.headers.get("X-Admin-Token", "")
    if token != FIXED_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

class LoginRequest(BaseModel):
    password: str

@app.post("/admin/login")
async def login(req: LoginRequest):
    h = hashlib.sha256(req.password.encode()).hexdigest()
    if h != ADMIN_HASH:
        raise HTTPException(status_code=401, detail="كلمة مرور خاطئة")
    return {"token": FIXED_TOKEN}

# تحديث العملات
class RatesUpdate(BaseModel):
    rates: list

@app.post("/admin/rates")
async def update_rates(req: RatesUpdate, request: Request, db=Depends(get_db)):
    verify_token(request)
    await db.execute("DELETE FROM rates")
    for r in req.rates:
        await db.execute(
            "INSERT INTO rates (currency, buy, sell) VALUES ($1, $2, $3)",
            r["currency"], r.get("buy"), r.get("sell")
        )
    return {"status": "ok"}

# تحديث الذهب الرسمي
class GoldOfficialUpdate(BaseModel):
    bulletin_date: str
    bulletin_time: Optional[str] = None
    karat_24: Optional[float] = None
    karat_21: Optional[float] = None
    karat_18: Optional[float] = None
    ounce_price: Optional[float] = None
    lira_gold: Optional[float] = None
    note: Optional[str] = None

@app.post("/admin/gold/official")
async def update_gold_official(req: GoldOfficialUpdate, request: Request, db=Depends(get_db)):
    verify_token(request)
    await db.execute("""
        INSERT INTO gold_official 
            (bulletin_date, bulletin_time, karat_24, karat_21, karat_18, ounce_price, lira_gold, note)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
    """, req.bulletin_date, req.bulletin_time, req.karat_24, req.karat_21,
         req.karat_18, req.ounce_price, req.lira_gold, req.note)
    return {"status": "ok"}

# تحديث أسعار الطاقة
class EnergyUpdate(BaseModel):
    fuel_type: str
    price: float
    unit: Optional[str] = "لتر"
    effective_date: Optional[str] = None
    note: Optional[str] = None

@app.post("/admin/energy")
async def update_energy(req: EnergyUpdate, request: Request, db=Depends(get_db)):
    verify_token(request)
    await db.execute("""
        INSERT INTO energy_prices (fuel_type, price, unit, effective_date, note)
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (fuel_type) DO UPDATE SET
            price = $2, unit = $3, effective_date = $4, note = $5, updated_at = NOW()
    """, req.fuel_type, req.price, req.unit, req.effective_date, req.note)
    return {"status": "ok"}

# ─── Admin Panel HTML ────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
async def admin_panel():
    return open("admin.html").read()
