from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional, Dict
import asyncpg
import os
import hashlib
import httpx
import json
from datetime import datetime, date

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_PASSWORD_HASH = "04067de8cf70fc76077836b9f28020fc7214e866437ed7071a66bd9efb450d17"
ADMIN_TOKEN = hashlib.sha256((ADMIN_PASSWORD_HASH + "syprate-fixed-salt").encode()).hexdigest()

async def get_db():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        await conn.close()

DEFAULT_CACHED = {
    "buy": None, "sell": None, "mid": None,
    "date": None, "updated_at": None,
    "source": "مصرف سوريا المركزي",
    "status": "initializing",
    "manual": False,
    "bulletin_no": None,
    "bulletin_url": None,
    "currencies": {}
}

@app.on_event("startup")
async def startup():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # ═══ جدول العملات — نفس الهيكل الأصلي (key/value JSON blob) ═══
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rates (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # ═══ جدول أرشيف النشرات — يحفظ كل نشرة نُدخلها بدون استبدال القديمة ═══
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rates_history (
                id SERIAL PRIMARY KEY,
                bulletin_no TEXT,
                bulletin_date TEXT,
                bulletin_url TEXT,
                snapshot TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # ═══ جدول الزيارات — نفس الهيكل الأصلي ═══
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS visits (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT '0'
            )
        """)
        await conn.execute("INSERT INTO visits (key,value) VALUES ('total','0') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO visits (key,value) VALUES ('today','0') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO visits (key,value) VALUES ('today_date','') ON CONFLICT DO NOTHING")

        # ═══ جداول جديدة — الطاقة (لا تؤثر على العملات) ═══
        # ملاحظة: الذهب والفضة والبلاتين الآن تلقائية 100% من PMA.sy - لا حاجة لجدول محلي
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS energy_prices (
                id SERIAL PRIMARY KEY,
                fuel_type TEXT NOT NULL UNIQUE,
                price NUMERIC NOT NULL,
                unit TEXT DEFAULT 'لتر',
                effective_date TEXT,
                note TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # إصلاح دفاعي: لو العمود كان موجود من قبل بنوع DATE، حوّله TEXT
        try:
            await conn.execute("ALTER TABLE energy_prices ALTER COLUMN effective_date TYPE TEXT")
        except Exception:
            pass
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS energy_price_history (
                id SERIAL PRIMARY KEY,
                fuel_type TEXT NOT NULL,
                price NUMERIC NOT NULL,
                unit TEXT DEFAULT 'لتر',
                effective_date TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        try:
            await conn.execute("ALTER TABLE energy_price_history ALTER COLUMN effective_date TYPE TEXT")
        except Exception:
            pass
        await conn.execute("""
            INSERT INTO energy_prices (fuel_type, price, unit)
            VALUES
                ('بنزين 95', 0, 'لتر'),
                ('بنزين 90', 0, 'لتر'),
                ('مازوت', 0, 'لتر'),
                ('غاز منزلي', 0, 'اسطوانة'),
                ('غاز صناعي', 0, 'اسطوانة')
            ON CONFLICT (fuel_type) DO NOTHING
        """)
    finally:
        await conn.close()

# ═══════════════════════════════════════════
# العملات — نفس الـ API الأصلي بالضبط
# ═══════════════════════════════════════════

@app.get("/")
def root():
    return {"message": "Syrian Rates API ✅"}

@app.get("/api/rates")
async def get_rates(db=Depends(get_db)):
    row = await db.fetchrow("SELECT value FROM rates WHERE key = 'cached'")
    if not row:
        return DEFAULT_CACHED
    try:
        return json.loads(row["value"])
    except Exception:
        return DEFAULT_CACHED

@app.get("/api/health")
async def health(db=Depends(get_db)):
    row = await db.fetchrow("SELECT value, updated_at FROM rates WHERE key = 'cached'")
    if not row:
        return {"status": "no_data"}
    data = json.loads(row["value"])
    return {"status": data.get("status"), "last_updated": str(row["updated_at"])}

class CurrencyRate(BaseModel):
    buy: float
    sell: float

class RatesUpdate(BaseModel):
    buy: float
    sell: float
    date: str
    bulletin_no: str = ""
    bulletin_url: str = ""
    currencies: Dict[str, CurrencyRate] = {}

def verify_token(request: Request):
    token = request.headers.get("X-Admin-Token", "")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

class LoginRequest(BaseModel):
    password: str

@app.post("/admin/login")
async def login(req: LoginRequest):
    h = hashlib.sha256(req.password.encode()).hexdigest()
    if h != ADMIN_PASSWORD_HASH:
        raise HTTPException(status_code=401, detail="كلمة مرور خاطئة")
    return {"token": ADMIN_TOKEN}

@app.post("/admin/rates")
async def update_rates(req: RatesUpdate, request: Request, db=Depends(get_db)):
    verify_token(request)
    mid = round((req.buy + req.sell) / 2, 4)
    payload = {
        "buy": req.buy,
        "sell": req.sell,
        "mid": mid,
        "date": req.date,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "source": "مصرف سوريا المركزي",
        "status": "ok",
        "manual": True,
        "bulletin_no": req.bulletin_no or None,
        "bulletin_url": req.bulletin_url or None,
        "currencies": {k: {"buy": v.buy, "sell": v.sell} for k, v in req.currencies.items()}
    }
    value_json = json.dumps(payload, ensure_ascii=False)
    await db.execute("""
        INSERT INTO rates (key, value, updated_at)
        VALUES ('cached', $1, NOW())
        ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()
    """, value_json)
    # حفظ نسخة بالأرشيف - لا تُستبدل، تتراكم بمرور الوقت
    await db.execute("""
        INSERT INTO rates_history (bulletin_no, bulletin_date, bulletin_url, snapshot)
        VALUES ($1, $2, $3, $4)
    """, req.bulletin_no or None, req.date, req.bulletin_url or None, value_json)
    return {"status": "ok"}

@app.get("/api/rates/history")
async def get_rates_history(db=Depends(get_db)):
    rows = await db.fetch("""
        SELECT bulletin_no, bulletin_date, bulletin_url, snapshot, created_at
        FROM rates_history ORDER BY created_at ASC
    """)
    result = []
    for r in rows:
        try:
            snap = json.loads(r["snapshot"])
        except Exception:
            snap = {}
        result.append({
            "bulletin_no": r["bulletin_no"],
            "date": r["bulletin_date"],
            "bulletin_url": r["bulletin_url"],
            "buy": snap.get("buy"),
            "sell": snap.get("sell"),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return result

# ═══════════════════════════════════════════
# الزيارات — نفس الأصلي
# ═══════════════════════════════════════════

@app.get("/api/visit")
async def record_visit(db=Depends(get_db)):
    today = date.today().isoformat()
    row = await db.fetchrow("SELECT value FROM visits WHERE key='today_date'")
    stored_date = row["value"] if row else ""

    if stored_date != today:
        await db.execute("UPDATE visits SET value = $1 WHERE key = 'today_date'", today)
        await db.execute("UPDATE visits SET value = '0' WHERE key = 'today'")

    total_row = await db.fetchrow("SELECT value FROM visits WHERE key='total'")
    today_row = await db.fetchrow("SELECT value FROM visits WHERE key='today'")
    total = int(total_row["value"] or 0) + 1
    today_count = int(today_row["value"] or 0) + 1

    await db.execute("UPDATE visits SET value = $1 WHERE key = 'total'", str(total))
    await db.execute("UPDATE visits SET value = $1 WHERE key = 'today'", str(today_count))

    return {"total": total, "today": today_count}

# ═══════════════════════════════════════════
# الذهب — ميزات جديدة (منفصلة تماماً عن العملات)
# ═══════════════════════════════════════════

PMA_ITEM_URL = "https://admin.pma.sy/pma_project/public/api/item?perPage=100"
PMA_WORLD_URL = "https://admin.pma.sy/pma_project/public/api/gold-prices"
PMA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://pma.sy/",
    "Origin": "https://pma.sy",
    "Accept": "application/json, text/plain, */*",
}

async def fetch_pma_items():
    async with httpx.AsyncClient(timeout=15, headers=PMA_HEADERS) as client:
        r = await client.get(PMA_ITEM_URL)
        r.raise_for_status()
        return r.json().get("data", [])

def find_pma_item(items, name_contains):
    for it in items:
        if name_contains in it.get("name", ""):
            return it
    return None

def shape_local_item(it):
    if not it:
        return None
    syp = it["pricing"].get("syp", {})
    usd = it["pricing"].get("usd", {})
    return {
        "name": it["name"],
        "buy_syp": float(syp.get("buy")) if syp.get("buy") else None,
        "sell_syp": float(syp.get("sale")) if syp.get("sale") else None,
        "buy_usd": float(usd.get("buy")) if usd.get("buy") else None,
        "sell_usd": float(usd.get("sale")) if usd.get("sale") else None,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }

@app.get("/api/gold/official")
async def get_gold_official():
    try:
        items = await fetch_pma_items()
        k24 = shape_local_item(find_pma_item(items, "عيار 24"))
        k21 = shape_local_item(find_pma_item(items, "عيار 21"))
        k18 = shape_local_item(find_pma_item(items, "عيار 18"))
        if not (k21 or k24 or k18):
            return {"status": "no_data"}
        return {
            "status": "ok",
            "source": "الهيئة العامة لإدارة المعادن الثمينة (PMA.sy)",
            "source_url": "https://pma.sy",
            "karat_24": k24, "karat_21": k21, "karat_18": k18,
            "updated_at": (k21 or k24 or k18)["updated_at"],
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/silver/local")
async def get_silver_local():
    try:
        items = await fetch_pma_items()
        s = shape_local_item(find_pma_item(items, "فضة"))
        if not s:
            return {"status": "no_data"}
        return {"status": "ok", "source": "الهيئة العامة لإدارة المعادن الثمينة (PMA.sy)",
                "source_url": "https://pma.sy", **s}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/platinum/local")
async def get_platinum_local():
    try:
        items = await fetch_pma_items()
        p = shape_local_item(find_pma_item(items, "بلاتين"))
        if not p:
            return {"status": "no_data"}
        return {"status": "ok", "source": "الهيئة العامة لإدارة المعادن الثمينة (PMA.sy)",
                "source_url": "https://pma.sy", **p}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/gold/world")
async def get_gold_world():
    try:
        async with httpx.AsyncClient(timeout=15, headers=PMA_HEADERS) as client:
            r = await client.get(PMA_WORLD_URL)
            item = r.json()["items"][0]
            price_usd = float(item["xauPrice"])
            gram_24 = price_usd / 31.1035
            return {
                "ounce_usd": round(price_usd, 2),
                "karat_24": round(gram_24, 2),
                "karat_22": round(gram_24 * (22/24), 2),
                "karat_21": round(gram_24 * (21/24), 2),
                "karat_18": round(gram_24 * (18/24), 2),
                "change": round(float(item.get("chgXau",0)), 2),
                "change_pct": round(float(item.get("pcXau",0)), 3),
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "source": "Bullion Vault / PMA.sy",
                "source_url": "https://pma.sy",
                "status": "live"
            }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/silver/world")
async def get_silver_world():
    try:
        async with httpx.AsyncClient(timeout=15, headers=PMA_HEADERS) as client:
            r = await client.get(PMA_WORLD_URL)
            item = r.json()["items"][0]
            price_usd = float(item["xagPrice"])
            gram = price_usd / 31.1035
            return {
                "ounce_usd": round(price_usd, 2),
                "silver_999": round(gram, 2),
                "silver_925": round(gram * 0.925, 2),
                "silver_900": round(gram * 0.900, 2),
                "change": round(float(item.get("chgXag",0)), 4),
                "change_pct": round(float(item.get("pcXag",0)), 3),
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "source": "Bullion Vault / PMA.sy",
                "source_url": "https://pma.sy",
                "status": "live"
            }
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ملاحظة: تم إلغاء الإدخال اليدوي للذهب - كل البيانات تلقائية 100% من PMA.sy

# ═══════════════════════════════════════════
# الطاقة — ميزة جديدة
# ═══════════════════════════════════════════

@app.get("/api/energy")
async def get_energy(db=Depends(get_db)):
    rows = await db.fetch("SELECT * FROM energy_prices ORDER BY id")
    return [dict(r) for r in rows]

@app.get("/api/energy/history")
async def get_energy_history(db=Depends(get_db)):
    rows = await db.fetch("SELECT * FROM energy_price_history ORDER BY created_at ASC")
    return [dict(r) for r in rows]

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
            price=$2, unit=$3, effective_date=$4, note=$5, updated_at=NOW()
    """, req.fuel_type, req.price, req.unit, req.effective_date, req.note)
    await db.execute("""
        INSERT INTO energy_price_history (fuel_type, price, unit, effective_date)
        VALUES ($1,$2,$3,$4)
    """, req.fuel_type, req.price, req.unit, req.effective_date)
    return {"status": "ok"}

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel():
    return open("admin.html").read()
