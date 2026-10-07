# ==========================================
# ⚙️ 全局系統設定檔 (config.py)
# ------------------------------------------
# All environment-specific values (sheet IDs/URLs, DB hosts, credentials) are
# read from environment variables. No real defaults are committed.
# See .env.example for the full list of variables.
# ==========================================
import json
import os
import re


def _env(name, default=""):
  # 空字串 (例如 GitHub Actions 中未設定的 secret) 視同未設定，回退到預設值
  return os.environ.get(name, "").strip() or default


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
# 來源 view / 表名由環境變數 SOURCE_VIEW 提供 (schema.name 或 name)；預設為中性佔位名。
# 只接受合法識別符，並在 SQL 中以 psycopg2.sql.Identifier 引用，避免注入。
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
SOURCE_VIEW = _env("SOURCE_VIEW") or "public.messages_view"
if not _IDENT_RE.match(SOURCE_VIEW):
  raise ValueError("SOURCE_VIEW must look like 'schema.name' or 'name' (letters, digits, underscore)")

# 來源欄位映射：內部中性 key -> 來源 view 的實際欄位名。預設值即中性 key 本身；
# 可用環境變數 SOURCE_COLUMN_MAP (JSON，只需列出要覆蓋的 key) 覆蓋。
# 欄位名只接受合法識別符，SQL 中以 psycopg2.sql.Identifier 引用並以 AS 別名統一成內部 key。
SOURCE_COLUMN_KEYS = [
  "message_id", "instance_id", "message_ts", "message_type", "sender_lid",
  "group_name", "group_id", "sent_date", "sent_time", "user_phone",
  "message_body", "media_caption", "quoted_message",
]
_COL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _load_source_column_map():
  mapping = {k: k for k in SOURCE_COLUMN_KEYS}
  raw = _env("SOURCE_COLUMN_MAP")
  if raw:
    try:
      override = json.loads(raw)
    except ValueError as e:
      raise ValueError("SOURCE_COLUMN_MAP must be a JSON object: %s" % e)
    if not isinstance(override, dict):
      raise ValueError("SOURCE_COLUMN_MAP must be a JSON object")
    for k, v in override.items():
      if k not in mapping:
        raise ValueError("SOURCE_COLUMN_MAP: unknown key '%s'" % k)
      mapping[k] = str(v)
  for k, v in mapping.items():
    if not _COL_RE.match(v):
      raise ValueError("SOURCE_COLUMN_MAP: invalid column name for '%s'" % k)
  return mapping


SOURCE_COLUMN_MAP = _load_source_column_map()

# 可自訂哪些 13 位號碼視為真實電話 (正則，對純數字做 fullmatch)。
# 預設為空：所有 13 位純數字一律視為 WhatsApp 設備 LID。
VALID_PHONE_13_REGEX = _env("VALID_PHONE_13_REGEX")
try:
  VALID_PHONE_13_RE = re.compile(VALID_PHONE_13_REGEX) if VALID_PHONE_13_REGEX else None
except re.error as e:
  raise ValueError("VALID_PHONE_13_REGEX is not a valid regular expression: %s" % e)
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

# ==========================================
# 🎯 特定 Group ID 過濾與打標設定
# ==========================================
# 1. 指定 Group ID 清單 (留空為處理所有群組；若填寫則只跑指定群組，支援逗號分隔)
# 支援環境變數傳入，例如: TARGET_GROUP_IDS="<group_id_1>@g.us,<group_id_2>@g.us"
ENV_TARGET_GIDS = os.environ.get("TARGET_GROUP_IDS", "").strip()
TARGET_GROUP_IDS = [gid.strip() for gid in ENV_TARGET_GIDS.split(",") if gid.strip()] if ENV_TARGET_GIDS else []

# 2. 品牌打標證據範圍
# False: 只有發言自身(含 Quoted)有品牌證據才打標
# True:  前文上下文推斷的品牌亦可打標
CONTEXT_ONLY_TAGGING = False
