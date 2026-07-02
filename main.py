from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import logging, re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Syrian Rates API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

cached = {
    "buy": None, "sell": None, "mid": None,
    "date": None, "updated_at": None,
    "source": "مصرف سوريا المركزي", "status": "initializing"
}

CB_URL = "https://cb.gov.sy/index.php?page=list&ex=2&dir=exchangerate&lang=1&service=4&act=1207"

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar-SY,ar;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://cb.gov.sy/",
    })
    return s

def fetch_rates():
    global cached
    logger.info("🔄 جاري جلب أسعار المركزي...")
    try:
        s = make_session()
        s.get("https://cb.gov.sy/", timeout=10)   # أول زيارة تجيب الكوكيز
        resp = s.get(CB_URL, timeout=15)
        resp.encoding = "utf-8"

        if resp.status_code != 200:
            logger.warning(f"⚠️ HTTP {resp.status_code}")
            cached["status"] = f"http_{resp.status_code}"
            return

        soup = BeautifulSoup(resp.text, "lxml")

        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            texts = [c.get_text(strip=True) for c in cells]

            # ابحث عن تاريخ
            date_val = next(
                (t for t in texts if re.search(r'\d{4}[-/]\d{2}[-/]\d{2}', t)
                 or re.search(r'\d{2}[-/]\d{2}[-/]\d{4}', t)), None
            )

            # ابحث عن أرقام > 50
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
                    "status": "ok"
                })
                logger.info(f"✅ شراء={buy} مبيع={sell} تاريخ={date_val}")
                return

        logger.warning("⚠️ لم يُعثر على بيانات")
        cached["status"] = "parse_error"

    except Exception as e:
        logger.error(f"❌ {e}")
        cached["status"] = f"error: {str(e)}"


@app.get("/")
def root():
    return {"message": "Syrian Rates API ✅", "docs": "/docs"}

@app.get("/api/rates")
def get_rates():
    return cached

@app.get("/api/health")
def health():
    return {"status": "ok", "cached_status": cached["status"], "last_updated": cached["updated_at"]}


scheduler = BackgroundScheduler()
scheduler.add_job(fetch_rates, "interval", hours=1)
scheduler.start()
fetch_rates()
