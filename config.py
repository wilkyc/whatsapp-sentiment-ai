# ==========================================
# ⚙️ 全局系統設定檔 (config.py)
# ------------------------------------------
# All environment-specific values (sheet IDs/URLs, DB hosts, credentials) are
# read from environment variables. No real defaults are committed.
# See .env.example for the full list of variables.
# ==========================================
import os
import re


def _env(name, default=""):
  return os.environ.get(name, default).strip()


def _env_int(name, default):
  val = _env(name)
  return int(val) if val else default


def _sheet_id_from_url(url):
  m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url or "")
  return m.group(1) if m else ""


# 🍼 35 個核心標準監控項 (Product Short 表頭標準)
PRODUCT_SHORT_BRANDS = [
  # 1. Abbott 體系
  "Abbott", "Similac HMO", "Similac Comfort", "Abbott PediaSure",
  # 2. Aptamil 體系
  "Aptamil", "Apta APF", "Apta NEO", "Apta PHP",
  # 3. Cow & Gate
  "C&G",
  # 4. Friso 體系
  "Friso", "Friso Gold", "Friso Prestige", "Friso Bio", "Friso Signature", "Friso Kids",
  # 5. HiPP
  "HiPP",
  # 6. Illuma 體系
  "Illuma", "Illuma Luxa", "Illuma Organic", "Illuma A2", "Illuma XtraCare",
  # 7. Mead Johnson (MJ) 體系
  "MJ", "Enfinitas", "MJ A+", "MJ NeuroPro", "MJ Gentle Care", "MJ NutriPower",
  # 8. Nestlé 體系
  "Nestle", "NAN HA", "NAN Infini Pro", "NAN A2",
  # 9. Wyeth 體系
  "Wyeth", "S26 Ultima", "S26 Gold", "Wyeth Ascenda"
]

# 🌳 母子品牌自動向上匯總 (Rollup) 映射字典
MASTER_BRAND_ROLLUP = {
  "Abbott": ["Similac HMO", "Similac Comfort", "Abbott PediaSure"],
  "Aptamil": ["Apta APF", "Apta NEO", "Apta PHP"],
  "Friso": ["Friso Gold", "Friso Prestige", "Friso Bio", "Friso Signature", "Friso Kids"],
  "Illuma": ["Illuma Luxa", "Illuma Organic", "Illuma A2", "Illuma XtraCare"],
  "MJ": ["Enfinitas", "MJ A+", "MJ NeuroPro", "MJ Gentle Care", "MJ NutriPower"],
  "Nestle": ["NAN HA", "NAN Infini Pro", "NAN A2"],
  "Wyeth": ["S26 Ultima", "S26 Gold", "Wyeth Ascenda"]
}

# 📝 48 個全量欄位標準格式 (reply 在 messageBody 後，brand 之前)
FINAL_HEADERS_48 = (
  ["Group", "GroupID", "Date", "Time", "userPhone", "Internal",
   "quotedMessage", "messageBody", "reply", "brand", "keywords", "warning"]
  + PRODUCT_SHORT_BRANDS
  + ["Other_Brands"]
)
FINAL_HEADERS_31 = FINAL_HEADERS_48  # 向下相容歷史別名

# 🌐 上下文推斷與風控參數
ENABLE_CONTEXTUAL_ALIAS = True          # 跨引用短稱解析開關
CONTEXT_HISTORY_MAX_MINUTES = 30       # 前文對話衰減時間窗口 (分鐘)

# 🌐 Google Sheets 雙表設定 (IDs / URLs supplied via env / GitHub Secrets)
# KEYWORDS_SPREADSHEET_ID takes priority; KEYWORDS_SHEET_URL is still accepted.
KEYWORDS_SPREADSHEET_ID = _env("KEYWORDS_SPREADSHEET_ID") or _sheet_id_from_url(_env("KEYWORDS_SHEET_URL"))
BRAND_SHEET_NAME = _env("BRAND_SHEET_NAME") or "brand_keywords"
IFT_SHEET_NAME = _env("IFT_SHEET_NAME", "ift_keywords")

GROUPINFO_SHEET_URL = _env("GROUPINFO_SHEET_URL")
MASTER_WORKSHEET_NAME = "Sheet1"

# 🧪 測試表格覆蓋設定
# Set TEST_TARGET_SHEET_URL to write all output into a single test sheet.
# Leave empty for production mode.
TEST_TARGET_SHEET_URL = _env("TEST_TARGET_SHEET_URL")
TEST_WORKSHEET_TAB = "Sheet1"

# 👥 內部號碼清單 (never committed)
# Either INTERNAL_PHONES_JSON (JSON string, e.g. a GitHub Secret) or a local
# gitignored file at INTERNAL_PHONES_FILE. See internal_phones.example.json.
INTERNAL_PHONES_JSON = _env("INTERNAL_PHONES_JSON")
INTERNAL_PHONES_FILE = _env("INTERNAL_PHONES_FILE", "internal_phones.json")

# 🐘 1. 原讀取對話資料庫 (PostgreSQL, WhatsApp message store)
DB_CONFIG = {
  "host": _env("DB_HOST"),
  "port": _env_int("DB_PORT", 5432),
  "database": _env("DB_NAME"),
  "user": _env("DB_USER"),
  "password": _env("DB_PASSWORD"),
}

# ⚡ 2. Supabase 全量寫入資料庫配置 (Session Mode - IPv4 Pooler)
SUPABASE_DB_CONFIG = {
  "host": _env("SUPABASE_DB_HOST"),
  "port": _env_int("SUPABASE_DB_PORT", 5432),
  "database": _env("SUPABASE_DB_NAME", "postgres"),
  "user": _env("SUPABASE_DB_USER"),
  "password": _env("SUPABASE_DB_PASSWORD"),
}
SUPABASE_FULL_TABLE = "message_full"

# 📊 Dashboard 設定 (used by dashboard.py, derived from the 35 standard columns)
FRISO_MAIN = "Friso"
FRISO_SUB_BRANDS = MASTER_BRAND_ROLLUP["Friso"]
BRAND_MAPPING = {
  "Abbott": ["Abbott"] + MASTER_BRAND_ROLLUP["Abbott"],
  "Aptamil": ["Aptamil"] + MASTER_BRAND_ROLLUP["Aptamil"],
  "Cow & Gate": ["C&G"],
  "HiPP": ["HiPP"],
  "Illuma": ["Illuma"] + MASTER_BRAND_ROLLUP["Illuma"],
  "Mead Johnson": ["MJ"] + MASTER_BRAND_ROLLUP["MJ"],
  "Nestle": ["Nestle"] + MASTER_BRAND_ROLLUP["Nestle"],
  "Wyeth": ["Wyeth"] + MASTER_BRAND_ROLLUP["Wyeth"],
}
