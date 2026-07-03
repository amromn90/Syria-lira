from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from apscheduler.schedulers.background import BackgroundScheduler
from pydantic import BaseModel
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import logging, re, hashlib, secrets, json, os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Syrian Rates API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── كلمة السر (مشفرة SHA256) ───────────────────
ADMIN_PASSWORD_HASH = "04067de8cf70fc76077836b9f28020fc7214e866437ed7071a66bd9efb450d17"
# token ثابت (مش عشوائي) — يضل شغال حتى لو السيرفر أعاد التشغيل
ADMIN_TOKEN = hashlib.sha256((ADMIN_PASSWORD_HASH + "syprate-fixed-salt").encode()).hexdigest()

# ─── الحفظ الدائم بملف ───────────────────────────
DATA_FILE = "rates_data.json"

def load_cached():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                logger.info(f"✅ تم استرجاع البيانات المحفوظة: {data}")
                return data
        except Exception as e:
            logger.warning(f"⚠️ فشل قراءة الملف المحفوظ: {e}")
    return {
        "buy": None, "sell": None, "mid": None,
        "date": None, "updated_at": None,
        "source": "مصرف سوريا المركزي",
        "status": "initializing",
        "manual": False,
        "bulletin_no": None,
        "bulletin_url": None,
        "currencies": {}
    }

def save_cached():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(cached, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"❌ فشل حفظ البيانات: {e}")

# ─── البيانات (تُحمّل من الملف إذا موجود) ─────────
cached = load_cached()

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
            cached["status"] = f"http_{resp.status_code}"
            return
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
                save_cached()
                logger.info(f"✅ شراء={buy} مبيع={sell}")
                return
        logger.warning("⚠️ لم يُعثر على بيانات")
        cached["status"] = "parse_error"
    except Exception as e:
        logger.error(f"❌ {e}")
        cached["status"] = f"error: {str(e)}"


# ─── AUTH ─────────────────────────────────────────
security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="غير مصرح")
    return True


# ─── MODELS ──────────────────────────────────────
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


# ─── PUBLIC ENDPOINTS ─────────────────────────────
@app.get("/")
def root():
    return {"message": "Syrian Rates API ✅"}

@app.get("/api/rates")
def get_rates():
    return cached

@app.get("/api/health")
def health():
    return {"status": "ok", "cached_status": cached["status"], "last_updated": cached["updated_at"]}


# ─── ADMIN ENDPOINTS ──────────────────────────────
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
    save_cached()
    logger.info(f"✅ تحديث يدوي: شراء={data.buy} مبيع={data.sell} نشرة={data.bulletin_no} عملات={len(currencies_dict)}")
    return {"success": True, "data": cached}

@app.get("/admin", response_class=HTMLResponse)
def admin_panel():
    return """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>لوحة التحكم - سعر الصرف السوري</title>
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Cairo',sans-serif;background:#0a0f0d;color:#f0fdf4;min-height:100vh;padding:2rem 1rem}
.card{background:#111f16;border:1px solid #1a3a25;border-radius:20px;padding:2rem;width:100%;max-width:620px;margin:0 auto;box-shadow:0 8px 32px rgba(0,0,0,0.5)}
h1{font-size:1.3rem;font-weight:800;color:#22c55e;margin-bottom:0.3rem;text-align:center}
.sub{font-size:0.75rem;color:#4ade80;text-align:center;margin-bottom:1.5rem}
label{font-size:0.8rem;color:#86efac;font-weight:600;display:block;margin-bottom:0.4rem}
input{width:100%;padding:0.8rem 1rem;background:#0d1a12;border:1.5px solid #1a3a25;border-radius:10px;color:#f0fdf4;font-family:'Cairo',sans-serif;font-size:1rem;font-weight:700;outline:none;margin-bottom:1rem;transition:border-color 0.2s}
input:focus{border-color:#22c55e}
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
.section-h{font-size:0.9rem;font-weight:800;color:#C9A84C;margin:1.2rem 0 0.8rem;display:flex;align-items:center;gap:0.4rem}
.curr-grid{display:grid;grid-template-columns:auto 1fr 1fr;gap:0.5rem 0.6rem;align-items:center;margin-bottom:0.5rem}
.curr-code{font-size:0.8rem;font-weight:800;color:#86efac;background:#0d1a12;border:1px solid #1a3a25;border-radius:8px;padding:0.6rem 0.5rem;text-align:center}
.curr-grid input{margin-bottom:0;padding:0.55rem 0.6rem;font-size:0.85rem}
.curr-header{display:grid;grid-template-columns:auto 1fr 1fr;gap:0.5rem 0.6rem;margin-bottom:0.5rem}
.curr-header span{font-size:0.68rem;color:#4ade80;text-align:center;font-weight:700}</style>
</head>
<body>
<div class="card">
  <h1>🛡️ لوحة التحكم</h1>
  <div class="sub">سعر الصرف السوري — Admin Only</div>

  <!-- LOGIN -->
  <div id="loginSection">
    <label>كلمة السر</label>
    <input type="password" id="passInput" placeholder="••••••••" onkeydown="if(event.key==='Enter')login()"/>
    <button class="btn" onclick="login()">دخول</button>
    <div class="msg" id="loginMsg"></div>
  </div>

  <!-- RATES PANEL -->
  <div id="ratesSection">
    <div class="current-rates" id="currentRates">
      <div class="cr-row"><span class="cr-label">آخر تحديث</span><span class="cr-value" id="crDate">—</span></div>
      <div class="cr-row"><span class="cr-label">رقم النشرة</span><span class="cr-value" id="crBulletinNo">—</span></div>
      <div class="cr-row"><span class="cr-label">سعر الدولار (شراء/مبيع)</span><span class="cr-value" id="crBuy">—</span></div>
      <div class="cr-row"><span class="cr-label"></span><span class="cr-value" id="crSell">—</span></div>
      <div class="cr-row"><span class="cr-label">المصدر</span><span id="crManual">—</span></div>
    </div>

    <div class="section-h">💵 الدولار الأمريكي (العملة الأساسية)</div>
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

    <div class="section-h">📋 لصق سريع من النشرة</div>
    <label style="font-weight:400;color:#6b8f7a">
      انسخ سطر كل عملة من النشرة (6 أرقام: قديمة وجديدة) — بياخد تلقائياً عمود <b>الليرة الجديدة</b> فقط (آخر رقمين)
    </label>
    <textarea id="pasteArea" rows="6" placeholder="USD 12200 12250 12150 122.00 122.50 121.50
EUR 13928.74 13998.04 13859.44 139.29 139.98 138.59
...(أو 3 أرقام فقط لو نسخت عمود الجديدة وحده)"
      style="width:100%;padding:0.8rem 1rem;background:#0d1a12;border:1.5px solid #1a3a25;border-radius:10px;color:#f0fdf4;font-family:'Cairo',sans-serif;font-size:0.85rem;outline:none;margin-bottom:0.6rem;resize:vertical"></textarea>
    <button class="btn" style="background:#1e3a5f" onclick="parsePaste()">⚡ تحليل ولصق بالحقول</button>
    <div class="msg" id="pasteMsg"></div>

    <div class="section-h">🌍 باقي العملات (حسب النشرة الرسمية)</div>
    <div class="curr-header">
      <span></span><span>شراء</span><span>مبيع</span>
    </div>
    <div id="currenciesGrid"></div>

    <button class="btn" onclick="updateRates()">💾 حفظ وتحديث الموقع</button>
    <div class="msg" id="ratesMsg"></div>

    <hr class="divider"/>
    <button class="btn" style="background:#1a3a25;color:#86efac" onclick="logout()">تسجيل الخروج</button>
  </div>
</div>

<script>
let TOKEN = localStorage.getItem('adminToken') || '';
const API = '';

// العملات الأساسية من النشرة الرسمية (بالإضافة للدولار)
const BULLETIN_CURRENCIES = [
  {code:'EUR', name:'يورو'},
  {code:'GBP', name:'جنيه إسترليني'},
  {code:'CHF', name:'فرنك سويسري'},
  {code:'JPY', name:'ين ياباني'},
  {code:'CNY', name:'يوان صيني'},
  {code:'TRY', name:'ليرة تركية'},
  {code:'SAR', name:'ريال سعودي'},
  {code:'QAR', name:'ريال قطري'},
  {code:'AED', name:'درهم إماراتي'},
  {code:'KWD', name:'دينار كويتي'},
  {code:'BHD', name:'دينار بحريني'},
  {code:'OMR', name:'ريال عماني'},
  {code:'JOD', name:'دينار أردني'},
  {code:'EGP', name:'جنيه مصري'},
  {code:'CAD', name:'دولار كندي'},
  {code:'DKK', name:'كرونة دنماركية'},
  {code:'SEK', name:'كرونة سويدية'},
  {code:'NOK', name:'كرونة نرويجية'},
  {code:'AUD', name:'دولار أسترالي'},
  {code:'RUB', name:'روبل روسي'},
];

function buildCurrenciesGrid() {
  const grid = document.getElementById('currenciesGrid');
  grid.innerHTML = '';
  BULLETIN_CURRENCIES.forEach(c => {
    const row = document.createElement('div');
    row.className = 'curr-grid';
    row.innerHTML = `
      <div class="curr-code">${c.code}</div>
      <input type="number" step="0.01" id="cur_${c.code}_buy" placeholder="شراء" title="${c.name} - شراء"/>
      <input type="number" step="0.01" id="cur_${c.code}_sell" placeholder="مبيع" title="${c.name} - مبيع"/>
    `;
    grid.appendChild(row);
  });
}
buildCurrenciesGrid();

// معروفة رموز العملات المسموحة (تشمل USD كمان)
const KNOWN_CODES = ['USD', ...BULLETIN_CURRENCIES.map(c => c.code)];

function parsePaste() {
  const text = document.getElementById('pasteArea').value;
  const msg  = document.getElementById('pasteMsg');
  if (!text.trim()) {
    showMsg(msg, 'الصق النص أول', 'err');
    return;
  }

  const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
  let filled = 0;
  let skipped = [];

  lines.forEach(line => {
    // ابحث عن رمز عملة معروف بالسطر (3 حروف كبيرة)
    const codeMatch = line.match(/\\b([A-Z]{3})\\b/);
    if (!codeMatch) return;
    const code = codeMatch[1];
    if (!KNOWN_CODES.includes(code)) return;

    // استخرج كل الأرقام بالسطر (يدعم الفاصلة العشرية والألوف)
    const nums = (line.match(/[\\d,]+\\.\\d+|\\d+/g) || [])
      .map(n => parseFloat(n.replace(/,/g, '')))
      .filter(n => !isNaN(n) && n > 0);

    if (nums.length === 0) return;

    let buy = null, sell = null;

    if (nums.length >= 6) {
      // 6 أرقام = [وسطي قديمة, مبيع قديمة, شراء قديمة, وسطي جديدة, مبيع جديدة, شراء جديدة]
      // ناخد فقط عمود الليرة الجديدة (آخر 3 أرقام)
      sell = nums[nums.length - 2];
      buy  = nums[nums.length - 1];
    } else if (nums.length === 3) {
      // 3 أرقام فقط = افتراضياً عمود واحد (وسطي، مبيع، شراء)
      sell = nums[1];
      buy  = nums[2];
    } else if (nums.length === 2) {
      sell = nums[0];
      buy  = nums[1];
    } else {
      return; // رقم واحد بس، مش كافي
    }

    // فحص أمان: تأكد إن الأرقام منطقية (مو أرقام الليرة القديمة الكبيرة بالغلط)
    // لو الرقم أكبر من 5000 لعملة غير SYP الأصلية، على الأغلب هي القديمة بالغلط
    if (code !== 'USD' && code !== 'EGP' && code !== 'JOD' && buy > 5000) {
      skipped.push(code + ' (رقم كبير مشبوه)');
      return;
    }

    if (code === 'USD') {
      document.getElementById('buyInput').value  = buy;
      document.getElementById('sellInput').value = sell;
      filled++;
    } else {
      const buyEl  = document.getElementById(`cur_${code}_buy`);
      const sellEl = document.getElementById(`cur_${code}_sell`);
      if (buyEl && sellEl) {
        buyEl.value  = buy;
        sellEl.value = sell;
        filled++;
      }
    }
  });

  if (filled > 0) {
    let m = `✅ تم تعبئة ${filled} عملة تلقائياً (عمود الليرة الجديدة فقط)`;
    if (skipped.length) m += ` — تم تجاوز: ${skipped.join(', ')}`;
    showMsg(msg, m, 'ok');
  } else {
    showMsg(msg, 'لم يتم التعرف على أي عملة — تأكد من الصيغة', 'err');
  }
}

async function login() {
  const pass = document.getElementById('passInput').value;
  const msg  = document.getElementById('loginMsg');
  try {
    const r = await fetch(API + '/admin/login', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({password: pass})
    });
    const d = await r.json();
    if (r.ok) {
      TOKEN = d.token;
      localStorage.setItem('adminToken', TOKEN);
      showPanel();
    } else {
      showMsg(msg, 'كلمة السر غلط ❌', 'err');
    }
  } catch(e) {
    showMsg(msg, 'خطأ بالاتصال', 'err');
  }
}

async function loadCurrentRates() {
  try {
    const r = await fetch(API + '/api/rates');
    const d = await r.json();
    document.getElementById('crDate').textContent  = d.date || '—';
    document.getElementById('crBulletinNo').textContent = d.bulletin_no || '—';
    document.getElementById('crBuy').textContent   = d.buy  ? ('شراء ' + d.buy) : '—';
    document.getElementById('crSell').textContent  = d.sell ? ('مبيع ' + d.sell) : '—';
    document.getElementById('crManual').innerHTML  =
      d.manual
        ? '<span class="badge manual">يدوي</span>'
        : '<span class="badge auto">تلقائي</span>';
    // ملء الحقول بالقيم الحالية
    if (d.buy)  document.getElementById('buyInput').value  = d.buy;
    if (d.sell) document.getElementById('sellInput').value = d.sell;
    if (d.date) document.getElementById('dateInput').value = d.date;
    if (d.bulletin_no)  document.getElementById('bulletinNoInput').value  = d.bulletin_no;
    if (d.bulletin_url) document.getElementById('bulletinUrlInput').value = d.bulletin_url;
    // ملء أسعار العملات المحفوظة
    if (d.currencies) {
      Object.keys(d.currencies).forEach(code => {
        const buyEl  = document.getElementById(`cur_${code}_buy`);
        const sellEl = document.getElementById(`cur_${code}_sell`);
        if (buyEl)  buyEl.value  = d.currencies[code].buy;
        if (sellEl) sellEl.value = d.currencies[code].sell;
      });
    }
  } catch(e) {}
}

async function updateRates() {
  const buy  = parseFloat(document.getElementById('buyInput').value);
  const sell = parseFloat(document.getElementById('sellInput').value);
  const date = document.getElementById('dateInput').value;
  const bulletin_no  = document.getElementById('bulletinNoInput').value;
  const bulletin_url = document.getElementById('bulletinUrlInput').value;
  const msg  = document.getElementById('ratesMsg');

  if (!buy || !sell || !date) {
    showMsg(msg, 'يرجى ملء حقول الدولار والتاريخ على الأقل', 'err');
    return;
  }

  // تجميع بيانات باقي العملات (فقط اللي تم تعبئتها)
  const currencies = {};
  BULLETIN_CURRENCIES.forEach(c => {
    const b = parseFloat(document.getElementById(`cur_${c.code}_buy`).value);
    const s = parseFloat(document.getElementById(`cur_${c.code}_sell`).value);
    if (b && s) currencies[c.code] = {buy: b, sell: s};
  });

  try {
    const r = await fetch(API + '/admin/rates', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + TOKEN
      },
      body: JSON.stringify({buy, sell, date, bulletin_no, bulletin_url, currencies})
    });
    const d = await r.json();
    if (r.ok) {
      showMsg(msg, '✅ تم التحديث بنجاح!', 'ok');
      loadCurrentRates();
    } else {
      showMsg(msg, 'غير مصرح ❌', 'err');
    }
  } catch(e) {
    showMsg(msg, 'خطأ بالاتصال', 'err');
  }
}

function showPanel() {
  document.getElementById('loginSection').style.display = 'none';
  document.getElementById('ratesSection').style.display = 'block';
  loadCurrentRates();
  // تحديث تلقائي كل دقيقة
  setInterval(loadCurrentRates, 60000);
}

function logout() {
  TOKEN = '';
  localStorage.removeItem('adminToken');
  document.getElementById('loginSection').style.display = 'block';
  document.getElementById('ratesSection').style.display = 'none';
}

function showMsg(el, text, type) {
  el.textContent = text;
  el.className = 'msg ' + type;
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 4000);
}

// تحقق لو في token محفوظ
if (TOKEN) showPanel();

// ضع تاريخ اليوم افتراضياً
document.getElementById('dateInput').value = new Date().toISOString().split('T')[0];
</script>
</body>
</html>"""


# ─── SCHEDULER ───────────────────────────────────
scheduler = BackgroundScheduler()
scheduler.add_job(fetch_rates, "interval", hours=1)
scheduler.start()
fetch_rates()
