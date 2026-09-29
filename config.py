# ==========================================
# ⚙️ 全局系統設定檔 (config.py)
# ------------------------------------------
# All environment-specific values (sheet URLs, DB hosts, credentials) are
# read from environment variables. No real defaults are committed.
# See .env.example for the full list of variables.
# ==========================================
import os


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _env_int(name, default):
    val = _env(name)
    return int(val) if val else default


# 🍼 18 個核心標準奶粉品牌
MILK_POWDER_BRANDS = [
    "雅培心美力", "Apta Platinum", "Apta Essensis", "Apta Neo", "牛欄牌",
    "美素", "美素金裝", "美素皇家", "美素有機", "美素Kids", "美素Signature",
    "Hipp", "Illuma", "Illuma 有機", "美贊臣 A+", "美贊臣 Enfinitas",
    "雀巢能恩", "雀巢全護"
]

# 📝 31 個全量欄位標準格式 (reply 移至 messageBody 後、brand 前)
FINAL_HEADERS_31 = (
    ["Group", "GroupID", "Date", "Time", "userPhone", "Internal",
     "quotedMessage", "messageBody", "reply", "brand", "keywords", "warning"]
    + MILK_POWDER_BRANDS
    + ["Other_Brands"]
)

# 🌐 Google Sheets 相關設定 (URLs supplied via env / GitHub Secrets)
KEYWORDS_SHEET_URL = _env("KEYWORDS_SHEET_URL")
BRAND_SHEET_NAME = "brand_keywords"
IFT_SHEET_NAME = "ift_keywords"

GROUPINFO_SHEET_URL = _env("GROUPINFO_SHEET_URL")
MASTER_WORKSHEET_NAME = "Sheet1"

# 🧪 測試表格覆蓋設定
# Set TEST_TARGET_SHEET_URL to write all output into a single test sheet.
# Leave empty for production mode (writes to yymm_DailyData_PartN sheets).
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

# 📊 Dashboard 設定 (used by dashboard.py)
BRAND_MAPPING = {
    "Abbott": ["雅培心美力"],
    "Apta": ["Apta Platinum", "Apta Essensis", "Apta Neo"],
    "Cow & Gate": ["牛欄牌"],
    "HiPP": ["Hipp"],
    "Mead Johnson": ["美贊臣 A+", "美贊臣 Enfinitas"],
    "Nestle": ["雀巢能恩", "雀巢全護"],
    "Wyeth / illuma": ["Illuma", "Illuma 有機"]
}
FRISO_SUB_BRANDS = ["美素金裝", "美素皇家", "美素有機", "美素Kids", "美素Signature"]
FRISO_MAIN = "美素"
