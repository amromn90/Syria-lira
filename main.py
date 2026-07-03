from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from apscheduler.schedulers.background import BackgroundScheduler
from pydantic import BaseModel
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import logging, re, hashlib, json, os
import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Syrian Rates API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ADMIN_PASSWORD_HASH = "04067de8cf70fc76077836b9f28020fc7214e866437ed7071a66bd9efb450d17"
ADMIN_TOKEN = hashlib.sha256((ADMIN_PASSWORD_HASH + "syprate-fixed-salt").encode()).hexdigest()
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ─── PostgreSQL ───────────────────────────────────
def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    if not DATABASE_URL:
        logger.warning("⚠️ DATABASE_URL غير موجود")
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS rates (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                # جدول الزيارات بعمود TEXT واحد للقيمة
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS visits (
                        key TEXT PRIMARY KEY,
                        value TEXT DEFAULT '0'
                    )
                """)
                cur.execute("INSERT INTO visits (key,value) VALUES ('total','0') ON CONFLICT DO NOTHING")
                cur.execute("INSERT INTO visits (key,value) VALUES ('today','0') ON CONFLICT DO NOTHING")
                cur.execute("INSERT INTO visits (key,value) VALUES ('today_date','') ON CONFLICT DO NOTHING")
        logger.info("✅ قاعدة البيانات جاهزة")
    except Exception as e:
        logger.error(f"❌ فشل تهيئة قاعدة البيانات: {e}")

def db_save(data: dict):
    if not DATABASE_URL:
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO rates (key, value, updated_at)
                    VALUES ('cached', %s, NOW())
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = NOW()
                """, (json.dumps(data, ensure_ascii=False),))
        logger.info("✅ تم الحفظ بقاعدة البيانات")
    except Exception as e:
        logger.error(f"❌ فشل الحفظ: {e}")

def db_load() -> dict:
    if not DATABASE_URL:
        return {}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM rates WHERE key = 'cached'")
                row = cur.fetchone()
                if row:
                    data = json.loads(row[0])
                    logger.info("✅ تم تحميل البيانات من قاعدة البيانات")
                    return data
    except Exception as e:
        logger.error(f"❌ فشل التحميل: {e}")
    return {}

def db_visit():
    if not DATABASE_URL:
        return {"total": 0, "today": 0}
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        with get_conn() as conn:
            with conn.cursor() as cur:
                # جيب تاريخ اليوم المحفوظ
                cur.execute("SELECT value FROM visits WHERE key='today_date'")
                row = cur.fetchone()
                stored_date = row[0] if row else ""
                # لو تغير اليوم صفّر عداد اليوم
                if stored_date != today:
                    cur.execute("UPDATE visits SET value='0' WHERE key='today'")
                    cur.execute("UPDATE visits SET value=%s WHERE key='today_date'", (today,))
                # زود العدادين
                cur.execute("UPDATE visits SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='total' RETURNING value")
                total = int(cur.fetchone()[0])
                cur.execute("UPDATE visits SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='today' RETURNING value")
                today_count = int(cur.fetchone()[0])
        return {"total": total, "today": today_count}
    except Exception as e:
        logger.error(f"❌ فشل تسجيل الزيارة: {e}")
        return {"total": 0, "today": 0}

# ─── الحالة الافتراضية ───────────────────────────
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

init_db()
loaded = db_load()
cached = loaded if loaded.get("status") == "ok" else DEFAULT_CACHED.copy()

# ─── SCRAPER ─────────────────────────────────────
CB_URL = "https://cb.gov.sy/index.php?page=list&ex=2&dir=exchangerate&lang=1&service=4&act=1207"

def fetch_rates():
    global cached
    logger.info("🔄 جاري جلب أسعار المركزي...")
    try:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept-Language": "ar-SY,ar;q=0.9",
            "Referer": "https://cb.gov.sy/",
        })
        s.get("https://cb.gov.sy/", timeout=10)
        resp = s.get(CB_URL, timeout=15)
        resp.encoding = "utf-8"
        if resp.status_code != 200:
            logger.warning(f"⚠️ HTTP {resp.status_code} - محافظ على البيانات القديمة")
            return  # لا نغير شي، نحافظ على البيانات القديمة
        soup = BeautifulSoup(resp.text, "lxml")
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            texts = [c.get_text(strip=True) for c in cells]
            date_val = next(
                (t for t in texts if re.search(r'\d{4}[-/]\d{2}[-/]\d{2}', t)
                 or re.search(r'\d{2}[-/]\d{2}[-/]\d{4}', t)), None
            )
            nums = []
            for t in texts:
                clean = re.sub(r'[^\d.]', '', t.replace(',', '.'))
                if re.match(r'^\d+(\.\d+)?$', clean):
                    v = float(clean)
                    if v > 50:
                        nums.append(v)
            if date_val and len(nums) >= 2:
                buy, sell = nums[0], nums[1]
                cached.update({
                    "buy": buy, "sell": sell,
                    "mid": round((buy + sell) / 2, 4),
                    "date": date_val,
                    "updated_at": datetime.utcnow().isoformat() + "Z",
                    "status": "ok", "manual": False
                })
                db_save(cached)
                logger.info(f"✅ شراء={buy} مبيع={sell}")
                return
        logger.warning("⚠️ لم يُعثر على بيانات - محافظ على البيانات القديمة")
        # لا نغير cached إذا عندنا بيانات محفوظة
    except Exception as e:
        logger.error(f"❌ {e} - محافظ على البيانات القديمة")
        # لا نغير cached إذا عندنا بيانات محفوظة


security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="غير مصرح")
    return True


class LoginRequest(BaseModel):
    password: str

class CurrencyRate(BaseModel):
    buy: float
    sell: float

class RatesUpdate(BaseModel):
    buy: float
    sell: float
    date: str
    bulletin_no: str = ""
    bulletin_url: str = ""
    currencies: dict[str, CurrencyRate] = {}


@app.get("/")
def root():
    return {"message": "Syrian Rates API ✅"}

@app.get("/api/rates")
def get_rates():
    return cached

@app.get("/api/health")
def health():
    return {"status": "ok", "cached_status": cached["status"], "last_updated": cached["updated_at"], "db": bool(DATABASE_URL)}

@app.get("/api/visit")
def record_visit():
    return db_visit()

@app.post("/admin/login")
def admin_login(req: LoginRequest):
    hashed = hashlib.sha256(req.password.encode()).hexdigest()
    if hashed != ADMIN_PASSWORD_HASH:
        raise HTTPException(status_code=401, detail="كلمة السر غلط")
    return {"token": ADMIN_TOKEN}

@app.post("/admin/rates")
def update_rates(data: RatesUpdate, auth: bool = Depends(verify_token)):
    mid = round((data.buy + data.sell) / 2, 4)
    currencies_dict = {}
    for code, rate in data.currencies.items():
        c_mid = round((rate.buy + rate.sell) / 2, 4)
        currencies_dict[code] = {"buy": rate.buy, "sell": rate.sell, "mid": c_mid}

    cached.update({
        "buy": data.buy, "sell": data.sell, "mid": mid,
        "date": data.date,
        "bulletin_no": data.bulletin_no,
        "bulletin_url": data.bulletin_url,
        "currencies": currencies_dict,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "status": "ok", "manual": True
    })
    db_save(cached)
    logger.info(f"✅ تحديث يدوي: شراء={data.buy} مبيع={data.sell}")
    return {"success": True, "data": cached}

@app.get("/admin", response_class=HTMLResponse)
def admin_panel():
    return """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>لوحة التحكم</title>
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Cairo',sans-serif;background:#0a0f0d;color:#f0fdf4;min-height:100vh;padding:2rem 1rem}
.card{background:#111f16;border:1px solid #1a3a25;border-radius:20px;padding:2rem;width:100%;max-width:620px;margin:0 auto;box-shadow:0 8px 32px rgba(0,0,0,0.5)}
h1{font-size:1.3rem;font-weight:800;color:#22c55e;margin-bottom:0.3rem;text-align:center}
.sub{font-size:0.75rem;color:#4ade80;text-align:center;margin-bottom:1.5rem}
label{font-size:0.8rem;color:#86efac;font-weight:600;display:block;margin-bottom:0.4rem}
input,textarea{width:100%;padding:0.8rem 1rem;background:#0d1a12;border:1.5px solid #1a3a25;border-radius:10px;color:#f0fdf4;font-family:'Cairo',sans-serif;font-size:1rem;font-weight:700;outline:none;margin-bottom:1rem;transition:border-color 0.2s}
input:focus,textarea:focus{border-color:#22c55e}
.btn{width:100%;padding:0.9rem;background:linear-gradient(135deg,#15803d,#22c55e);color:white;border:none;border-radius:10px;font-family:'Cairo',sans-serif;font-size:1rem;font-weight:700;cursor:pointer;transition:opacity 0.2s;margin-top:0.5rem}
.btn:hover{opacity:0.9}
.msg{padding:0.7rem 1rem;border-radius:8px;font-size:0.85rem;font-weight:600;margin-top:1rem;text-align:center;display:none}
.msg.ok{background:#052e16;border:1px solid #22c55e;color:#4ade80}
.msg.err{background:#1c0a0a;border:1px solid #ef4444;color:#f87171}
.divider{border:none;border-top:1px solid #1a3a25;margin:1.5rem 0}
.badge{display:inline-block;padding:0.2rem 0.6rem;border-radius:20px;font-size:0.7rem;font-weight:700}
.badge.manual{background:#1e3a5f;color:#60a5fa}
.badge.auto{background:#052e16;color:#4ade80}
#loginSection{max-width:420px;margin:0 auto}
#ratesSection{display:none}
.current-rates{background:#0d1a12;border:1px solid #1a3a25;border-radius:10px;padding:1rem;margin-bottom:1.5rem}
.cr-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:0.4rem}
.cr-label{font-size:0.75rem;color:#86efac}
.cr-value{font-size:1rem;font-weight:800;color:#22c55e}
.section-h{font-size:0.9rem;font-weight:800;color:#C9A84C;margin:1.2rem 0 0.8rem}
.curr-grid{display:grid;grid-template-columns:auto 1fr 1fr;gap:0.5rem 0.6rem;align-items:center;margin-bottom:0.5rem}
.curr-code{font-size:0.8rem;font-weight:800;color:#86efac;background:#0d1a12;border:1px solid #1a3a25;border-radius:8px;padding:0.6rem 0.5rem;text-align:center}
.curr-grid input{margin-bottom:0;padding:0.55rem 0.6rem;font-size:0.85rem}
.curr-header{display:grid;grid-template-columns:auto 1fr 1fr;gap:0.5rem 0.6rem;margin-bottom:0.5rem}
.curr-header span{font-size:0.68rem;color:#4ade80;text-align:center;font-weight:700}
</style>
</head>
<body>
<div class="card">
  <h1>🛡️ لوحة التحكم</h1>
  <div class="sub">محول العملة السورية — Admin Only</div>
  <div id="loginSection">
    <label>كلمة السر</label>
    <input type="password" id="passInput" placeholder="••••••••" onkeydown="if(event.key==='Enter')login()"/>
    <button class="btn" onclick="login()">دخول</button>
    <div class="msg" id="loginMsg"></div>
  </div>
  <div id="ratesSection">
    <div class="current-rates">
      <div class="cr-row"><span class="cr-label">آخر تحديث</span><span class="cr-value" id="crDate">—</span></div>
      <div class="cr-row"><span class="cr-label">رقم النشرة</span><span class="cr-value" id="crBulletinNo">—</span></div>
      <div class="cr-row"><span class="cr-label">شراء الدولار</span><span class="cr-value" id="crBuy">—</span></div>
      <div class="cr-row"><span class="cr-label">مبيع الدولار</span><span class="cr-value" id="crSell">—</span></div>
      <div class="cr-row"><span class="cr-label">المصدر</span><span id="crManual">—</span></div>
    </div>
    <div class="section-h">💵 الدولار الأمريكي</div>
    <label>سعر الشراء (Buy)</label>
    <input type="number" id="buyInput" placeholder="121.50" step="0.01"/>
    <label>سعر المبيع (Sell)</label>
    <input type="number" id="sellInput" placeholder="122.50" step="0.01"/>
    <div class="section-h">📋 معلومات النشرة</div>
    <label>تاريخ النشرة</label>
    <input type="date" id="dateInput"/>
    <label>رقم النشرة</label>
    <input type="text" id="bulletinNoInput" placeholder="119"/>
    <label>رابط النشرة (PDF)</label>
    <input type="text" id="bulletinUrlInput" placeholder="https://cb.gov.sy/downloads/files/xxxx.PDF"/>
    <div class="section-h">⚡ لصق سريع من النشرة</div>
    <label style="font-weight:400;color:#6b8f7a">انسخ سطر كل عملة (6 أرقام) — بياخد عمود الليرة الجديدة تلقائياً</label>
    <textarea id="pasteArea" rows="6" placeholder="USD 12200 12250 12150 122.00 122.50 121.50&#10;EUR 13928.74 13998.04 13859.44 139.29 139.98 138.59"></textarea>
    <button class="btn" style="background:#1e3a5f;margin-top:0" onclick="parsePaste()">⚡ تحليل ولصق بالحقول</button>
    <div class="msg" id="pasteMsg"></div>
    <div class="section-h">🌍 باقي العملات</div>
    <div class="curr-header"><span></span><span>شراء</span><span>مبيع</span></div>
    <div id="currenciesGrid"></div>
    <button class="btn" onclick="updateRates()">💾 حفظ وتحديث الموقع</button>
    <div class="msg" id="ratesMsg"></div>
    <hr class="divider"/>
    <button class="btn" style="background:#1a3a25;color:#86efac" onclick="logout()">تسجيل الخروج</button>
  </div>
</div>
<script>
let TOKEN=localStorage.getItem('adminToken')||'';
const API='';
const BULLETIN_CURRENCIES=[
  {code:'EUR',name:'يورو'},{code:'GBP',name:'جنيه إسترليني'},
  {code:'CHF',name:'فرنك سويسري'},{code:'JPY',name:'ين ياباني'},
  {code:'CNY',name:'يوان صيني'},{code:'TRY',name:'ليرة تركية'},
  {code:'SAR',name:'ريال سعودي'},{code:'QAR',name:'ريال قطري'},
  {code:'AED',name:'درهم إماراتي'},{code:'KWD',name:'دينار كويتي'},
  {code:'BHD',name:'دينار بحريني'},{code:'OMR',name:'ريال عماني'},
  {code:'JOD',name:'دينار أردني'},{code:'EGP',name:'جنيه مصري'},
  {code:'CAD',name:'دولار كندي'},{code:'DKK',name:'كرونة دنماركية'},
  {code:'SEK',name:'كرونة سويدية'},{code:'NOK',name:'كرونة نرويجية'},
  {code:'AUD',name:'دولار أسترالي'},{code:'RUB',name:'روبل روسي'},
];
const KNOWN_CODES=['USD',...BULLETIN_CURRENCIES.map(c=>c.code)];
function buildCurrenciesGrid(){
  const grid=document.getElementById('currenciesGrid');
  grid.innerHTML='';
  BULLETIN_CURRENCIES.forEach(c=>{
    const row=document.createElement('div');
    row.className='curr-grid';
    row.innerHTML=`<div class="curr-code">${c.code}</div><input type="number" step="0.01" id="cur_${c.code}_buy" placeholder="شراء"/><input type="number" step="0.01" id="cur_${c.code}_sell" placeholder="مبيع"/>`;
    grid.appendChild(row);
  });
}
buildCurrenciesGrid();
function parsePaste(){
  const text=document.getElementById('pasteArea').value;
  const msg=document.getElementById('pasteMsg');
  if(!text.trim()){showMsg(msg,'الصق النص أول','err');return;}
  const lines=text.split('\\n').map(l=>l.trim()).filter(Boolean);
  let filled=0,skipped=[];
  lines.forEach(line=>{
    const cm=line.match(/\\b([A-Z]{3})\\b/);
    if(!cm)return;
    const code=cm[1];
    if(!KNOWN_CODES.includes(code))return;
    const nums=(line.match(/[\\d,]+\\.\\d+|\\d+/g)||[]).map(n=>parseFloat(n.replace(/,/g,''))).filter(n=>!isNaN(n)&&n>0);
    if(!nums.length)return;
    let buy=null,sell=null;
    if(nums.length>=6){sell=nums[nums.length-2];buy=nums[nums.length-1];}
    else if(nums.length===3){sell=nums[1];buy=nums[2];}
    else if(nums.length===2){sell=nums[0];buy=nums[1];}
    else return;
    if(code!=='USD'&&code!=='EGP'&&code!=='KWD'&&buy>5000){skipped.push(code);return;}
    if(code==='USD'){document.getElementById('buyInput').value=buy;document.getElementById('sellInput').value=sell;filled++;}
    else{const b=document.getElementById(`cur_${code}_buy`);const s=document.getElementById(`cur_${code}_sell`);if(b&&s){b.value=buy;s.value=sell;filled++;}}
  });
  if(filled>0){let m=`✅ تم تعبئة ${filled} عملة`;if(skipped.length)m+=` — تجاوز: ${skipped.join(', ')}`;showMsg(msg,m,'ok');}
  else showMsg(msg,'لم يتعرف على أي عملة','err');
}
async function login(){
  const pass=document.getElementById('passInput').value;
  const msg=document.getElementById('loginMsg');
  try{const r=await fetch(API+'/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pass})});const d=await r.json();if(r.ok){TOKEN=d.token;localStorage.setItem('adminToken',TOKEN);showPanel();}else showMsg(msg,'كلمة السر غلط ❌','err');}
  catch(e){showMsg(msg,'خطأ بالاتصال','err');}
}
async function loadCurrentRates(){
  try{
    const r=await fetch(API+'/api/rates');const d=await r.json();
    document.getElementById('crDate').textContent=d.date||'—';
    document.getElementById('crBulletinNo').textContent=d.bulletin_no||'—';
    document.getElementById('crBuy').textContent=d.buy||'—';
    document.getElementById('crSell').textContent=d.sell||'—';
    document.getElementById('crManual').innerHTML=d.manual?'<span class="badge manual">يدوي</span>':'<span class="badge auto">تلقائي</span>';
    if(d.buy)document.getElementById('buyInput').value=d.buy;
    if(d.sell)document.getElementById('sellInput').value=d.sell;
    if(d.date)document.getElementById('dateInput').value=d.date;
    if(d.bulletin_no)document.getElementById('bulletinNoInput').value=d.bulletin_no;
    if(d.bulletin_url)document.getElementById('bulletinUrlInput').value=d.bulletin_url;
    if(d.currencies){Object.keys(d.currencies).forEach(code=>{const b=document.getElementById(`cur_${code}_buy`);const s=document.getElementById(`cur_${code}_sell`);if(b)b.value=d.currencies[code].buy;if(s)s.value=d.currencies[code].sell;});}
  }catch(e){}
}
async function updateRates(){
  const buy=parseFloat(document.getElementById('buyInput').value);
  const sell=parseFloat(document.getElementById('sellInput').value);
  const date=document.getElementById('dateInput').value;
  const bulletin_no=document.getElementById('bulletinNoInput').value;
  const bulletin_url=document.getElementById('bulletinUrlInput').value;
  const msg=document.getElementById('ratesMsg');
  if(!buy||!sell||!date){showMsg(msg,'يرجى ملء حقول الدولار والتاريخ','err');return;}
  const currencies={};
  BULLETIN_CURRENCIES.forEach(c=>{const b=parseFloat(document.getElementById(`cur_${c.code}_buy`).value);const s=parseFloat(document.getElementById(`cur_${c.code}_sell`).value);if(b&&s)currencies[c.code]={buy:b,sell:s};});
  try{
    const r=await fetch(API+'/admin/rates',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+TOKEN},body:JSON.stringify({buy,sell,date,bulletin_no,bulletin_url,currencies})});
    const d=await r.json();
    if(r.ok){showMsg(msg,'✅ تم التحديث بنجاح!','ok');loadCurrentRates();}
    else showMsg(msg,'غير مصرح ❌','err');
  }catch(e){showMsg(msg,'خطأ بالاتصال','err');}
}
function showPanel(){document.getElementById('loginSection').style.display='none';document.getElementById('ratesSection').style.display='block';loadCurrentRates();setInterval(loadCurrentRates,60000);}
function logout(){TOKEN='';localStorage.removeItem('adminToken');document.getElementById('loginSection').style.display='block';document.getElementById('ratesSection').style.display='none';}
function showMsg(el,text,type){el.textContent=text;el.className='msg '+type;el.style.display='block';setTimeout(()=>el.style.display='none',5000);}
if(TOKEN)showPanel();
document.getElementById('dateInput').value=new Date().toISOString().split('T')[0];
</script>
</body>
</html>"""

scheduler = BackgroundScheduler()
scheduler.add_job(fetch_rates, "interval", hours=1)
scheduler.start()
fetch_rates()
