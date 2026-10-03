import concurrent.futures
from datetime import datetime, timedelta
import json
import os
import re
import sys
import threading
import time

from config import (
  PRODUCT_SHORT_BRANDS as STANDARD_BRANDS,
  MASTER_BRAND_ROLLUP,
  FINAL_HEADERS_48,
  ENABLE_CONTEXTUAL_ALIAS,
  CONTEXT_HISTORY_MAX_MINUTES,
  TARGET_GROUP_IDS,
  CONTEXT_ONLY_TAGGING,
  KEYWORDS_SPREADSHEET_ID,
  BRAND_SHEET_NAME,
  IFT_SHEET_NAME,
  GROUPINFO_SHEET_URL,
  MASTER_WORKSHEET_NAME,
  TEST_TARGET_SHEET_URL,
  TEST_WORKSHEET_TAB,
  DB_CONFIG,
  SOURCE_VIEW,
  SUPABASE_DB_CONFIG,
  SUPABASE_FULL_TABLE,
  INTERNAL_PHONES_JSON,
  INTERNAL_PHONES_FILE
)
from google_auth import get_google_clients, with_retry
import gspread
import openai
import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor, execute_values
import pytz
import requests

# ==========================================
# ⚙️ 1. 核心參數與多模式重跑解析
# ==========================================
ENV_MANUAL_INPUT = os.environ.get("MANUAL_DATE", "").strip()

INCREMENTAL_WINDOW_MINUTES = 65
CONTEXT_HISTORY_LIMIT = 5
TIME_TOLERANCE_SECONDS = 60

POE_API_KEY = os.environ.get("POE_API_KEY", "")
POE_MODEL = "gemini-3.1-flash-lite"

poe_client = openai.OpenAI(
  api_key=POE_API_KEY,
  base_url="https://api.poe.com/v1",
)

# 內部號碼清單 (從環境變數或本地 gitignored 檔案載入，公開 repo 不含任何真實號碼)
# 格式: {"Apta": ["9xxxxxxx", ...], "Admin": [...], "Friso": [...]}
# 優先順序依 JSON 內 key 的順序 (第一個命中的標籤會寫入 Internal 欄位)
def load_internal_phones():
  raw = INTERNAL_PHONES_JSON
  source = "INTERNAL_PHONES_JSON"
  if not raw and INTERNAL_PHONES_FILE and os.path.exists(INTERNAL_PHONES_FILE):
    with open(INTERNAL_PHONES_FILE, "r", encoding="utf-8") as f:
      raw = f.read()
    source = INTERNAL_PHONES_FILE
  if not raw:
    print("ℹ️ 未設定內部號碼清單 (INTERNAL_PHONES_JSON / internal_phones.json)，Internal 欄位將留空。")
    return {}
  try:
    data = json.loads(raw)
    groups = {
      str(label): {re.sub(r"\D", "", str(p)) for p in phones if str(p).strip()}
      for label, phones in data.items()
    }
    print("✅ 已從 %s 載入內部號碼清單: %s" % (
      source, ", ".join("%s(%d)" % (k, len(v)) for k, v in groups.items())))
    return groups
  except Exception as e:
    print("⚠️ 內部號碼清單格式錯誤，已略過:", str(e))
    return {}

INTERNAL_PHONE_GROUPS = load_internal_phones()

def get_internal_status(phone_digits):
  for label, phones in INTERNAL_PHONE_GROUPS.items():
    if phone_digits in phones:
      return label
  return ""

stats_lock = threading.Lock()
abort_event = threading.Event()
consecutive_ai_failures = 0
MAX_CONSECUTIVE_FAILURES = 5

stats = {
  "total_db_rows": 0,
  "total_deduped_rows": 0,
  "need_ai_processing": 0,
  "ai_actually_processed": 0,
  "contextual_alias_triggered": 0,
  "hard_fact_guaranteed_count": 0,
  "spam_detected": 0,
  "generic_no_brand": 0,
  "brand_identified_count": 0,
  "context_attributed_count": 0
}

def print_stage_dashboard(stage_name, metrics):
  print("\n" + "=" * 55)
  print("📊 " + str(stage_name) + " - 統計看板")
  print("=" * 55)
  for key, value in metrics.items():
    print("%-26s : %s" % (str(key), str(value)))
  print("=" * 55 + "\n")


# ==========================================
# 🔐 2. 授權 Google API
# ==========================================
print("🔐 正在使用服務帳戶授權 Google Sheets API...")
try:
  gc, _, _ = get_google_clients()
  print("✅ Google Sheets 服務帳戶授權成功！")
except Exception as e:
  print("❌ 授權失敗:", str(e))
  sys.exit(1)


_missing_cfg = [name for name, val in [
  ("KEYWORDS_SPREADSHEET_ID / KEYWORDS_SHEET_URL", KEYWORDS_SPREADSHEET_ID),
  ("GROUPINFO_SHEET_URL", GROUPINFO_SHEET_URL),
  ("DB_HOST", DB_CONFIG.get("host")),
  ("DB_NAME", DB_CONFIG.get("database")),
  ("DB_USER", DB_CONFIG.get("user")),
  ("POE_API_KEY", POE_API_KEY),
] if not val]
if _missing_cfg:
  print("❌ 缺少必要環境變數:", ", ".join(_missing_cfg), "(請參考 .env.example)")
  sys.exit(1)


# ==========================================
# 🌐 3. 動態元數據編譯器與詞庫健康自檢
# ==========================================
print("\n🌐 正在從指定 Google Sheet 讀取雙配置檔 (brand_keywords & ift_keywords)...")
try:
  sh_obj = gc.open_by_key(KEYWORDS_SPREADSHEET_ID)
   
  try:
    b_sheet = sh_obj.worksheet(BRAND_SHEET_NAME)
  except Exception:
    b_sheet = sh_obj.sheet1
  b_data = b_sheet.get_all_values()
  brand_df = pd.DataFrame(b_data[1:], columns=b_data[0]) if b_data else pd.DataFrame()
  brand_df.columns = brand_df.columns.str.strip()

  try:
    ift_sheet = sh_obj.worksheet(IFT_SHEET_NAME)
    ift_data = ift_sheet.get_all_values()
    ift_df = pd.DataFrame(ift_data[1:], columns=ift_data[0]) if ift_data else pd.DataFrame()
    ift_df.columns = ift_df.columns.str.strip()
  except Exception:
    ift_df = pd.DataFrame()

  gi_sheet = gc.open_by_url(GROUPINFO_SHEET_URL).worksheet("groups")
  gi_data = gi_sheet.get_all_values()
  groupinfo_df = pd.DataFrame(gi_data[1:], columns=gi_data[0]) if gi_data else pd.DataFrame()
  groupinfo_df.columns = groupinfo_df.columns.str.strip()
  print("✅ 雙關鍵詞表與群組資訊讀取成功！")
except Exception as e:
  print("❌ 讀取配置檔失敗:", str(e))
  sys.exit(1)

group_map = {}
for _, row in groupinfo_df.iterrows():
  gid = str(row.get("gus_id", "")).strip()
  if gid.endswith(".0"):
    gid = gid[:-2]
  gname = str(row.get("subject", "")).strip()
  if gid and gid.lower() not in ["nan", "none"]:
    group_map[gid] = gname

CODE_TO_COLUMN_MAP = {
  ("abbott", "master"): "Abbott",
  ("abbott", "similac_hmo"): "Similac HMO",
  ("abbott", "similac_comfort"): "Similac Comfort",
  ("abbott", "pediasure"): "Abbott PediaSure",
  ("aptamil", "master"): "Aptamil",
  ("aptamil", "apf"): "Apta APF",
  ("aptamil", "neo"): "Apta NEO",
  ("aptamil", "php"): "Apta PHP",
  ("aptamil", "essensis"): "Aptamil",
  ("cow & gate", "master"): "C&G",
  ("cow & gate", "a2_beta_casein"): "C&G",
  ("friso", "master"): "Friso",
  ("friso", "gold"): "Friso Gold",
  ("friso", "prestige"): "Friso Prestige",
  ("friso", "bio"): "Friso Bio",
  ("friso", "signature"): "Friso Signature",
  ("friso", "kids"): "Friso Kids",
  ("hipp", "master"): "HiPP",
  ("hipp", "hmp"): "HiPP",
  ("illuma", "master"): "Illuma",
  ("illuma", "luxa"): "Illuma Luxa",
  ("illuma", "organic"): "Illuma Organic",
  ("illuma", "a2"): "Illuma A2",
  ("illuma", "xtracare"): "Illuma XtraCare",
  ("mjn", "master"): "MJ",
  ("mjn", "enfinitas"): "Enfinitas",
  ("mjn", "a_plus"): "MJ A+",
  ("mjn", "neuropro"): "MJ NeuroPro",
  ("mjn", "gentle_care"): "MJ Gentle Care",
  ("mjn", "nutripower"): "MJ NutriPower",
  ("nestle", "master"): "Nestle",
  ("nestle", "nan_ha"): "NAN HA",
  ("nestle", "nan_infini_pro"): "NAN Infini Pro",
  ("nestle", "nan_a2"): "NAN A2",
  ("nestle", "nan_legacy"): "NAN HA",
  ("wyeth", "master"): "Wyeth",
  ("wyeth", "s26_master"): "Wyeth",
  ("wyeth", "s26_ultima"): "S26 Ultima",
  ("wyeth", "s26_gold"): "S26 Gold",
  ("wyeth", "ascenda"): "Wyeth Ascenda",
  ("wyeth", "s26_legacy"): "S26 Gold"
}

def resolve_target_column(brand_val, sub_brand_val):
  b_key = str(brand_val).strip().lower()
  s_key = str(sub_brand_val).strip().lower()
  return CODE_TO_COLUMN_MAP.get((b_key, s_key))

brand_text_rules = []
brand_regex_rules = []
all_brand_keywords_set = set()

for _, row in brand_df.iterrows():
  kw = str(row.get("Keyword", "") or row.get("keyword", "")).strip()
  m_type = str(row.get("Match_Type", "") or row.get("match_type", "")).strip().upper()
  c_with = str(row.get("Combo_With", "") or row.get("combo_with", "")).strip().lower()
  b_parent = str(row.get("Brand", "") or row.get("brand", "")).strip()
  sub_b = str(row.get("Sub_Brand", "") or row.get("sub_brand", "")).strip()
   
  if not kw or kw.lower() in ["nan", "none"]:
    continue
   
  target_col = resolve_target_column(b_parent, sub_b)

  if m_type == "REGEX":
    try:
      brand_regex_rules.append((re.compile(kw, re.IGNORECASE), kw, b_parent, sub_b, target_col))
    except Exception as e:
      print("⚠️ 正則語法錯誤跳過 [" + kw + "]:", str(e))
  else:
    brand_text_rules.append({
      "kw": kw,
      "match_type": m_type if m_type in ["COMBO", "CONTAINS"] else "CONTAINS",
      "combo_with": c_with,
      "brand": b_parent,
      "sub_brand": sub_b,
      "target_col": target_col
    })
    all_brand_keywords_set.add(kw.lower())

# ── 💡 詞庫健康自檢 (Health Check)：主動發現漏詞 (同時檢索字面詞與正則表達式) ──
CORE_CHECK_LIST = ["美素", "愛他美", "牛欄", "心美力", "雅培", "能恩", "雀巢", "啟賦", "美贊臣", "惠氏"]
all_rules_text_corpus = " ".join(all_brand_keywords_set) + " " + " ".join([r[1] for r in brand_regex_rules])
for core_term in CORE_CHECK_LIST:
  if core_term not in all_rules_text_corpus:
    print(f"⚠️ [詞庫告警] 檢測到品牌核心詞 [{core_term}] 未在 brand_keywords 中定義！請至 Google Sheet 檢查！")

formula_feature_keywords = []
general_keywords = []
exclude_mask_words = []

for _, row in ift_df.iterrows():
  k_type = str(row.get("type", "")).strip().lower()
  kw = str(row.get("keyword", "")).strip()
  if not kw or kw.lower() in ["nan", "none"]:
    continue
   
  if k_type == "exclude":
    exclude_mask_words.append(kw)
  elif k_type in ["formula_feature", "feature"]:
    formula_feature_keywords.append(kw)
  else:
    general_keywords.append(kw)

exclude_mask_words = sorted(list(set(exclude_mask_words)), key=len, reverse=True)
brand_text_rules = sorted(brand_text_rules, key=lambda x: len(x["kw"]), reverse=True)
formula_feature_keywords = sorted(list(set(formula_feature_keywords)), key=len, reverse=True)
general_keywords = sorted(list(set(general_keywords)), key=len, reverse=True)


# ==========================================
# 🐘 4. 連接 PostgreSQL 查詢對話數據
# ==========================================
hk_tz = pytz.timezone("Asia/Hong_Kong")
now_hk = datetime.now(hk_tz)

sql_where_clause = ""
sql_params = ()

def parse_any_date(date_text):
  date_text = date_text.strip()
  for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%y%m%d", "%Y%m%d", "%d-%m-%Y"]:
    try:
      return datetime.strptime(date_text, fmt)
    except ValueError:
      pass
  return None

if ENV_MANUAL_INPUT:
  print("\n🐘 檢測到手動輸入參數: [%s]" % ENV_MANUAL_INPUT)
  
  # 模式 1：精確時段 (例 "260928 14:00-16:00")
  time_range_match = re.search(r"^(\d{6}|\d{8}|\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2}(?::\d{2})?)-(\d{1,2}:\d{2}(?::\d{2})?)$", ENV_MANUAL_INPUT)
  
  # 模式 2：跨天/跨月範圍 (例 "2026-07-01 to 2026-09-30" 或 "260701-260930" 或 "01/07/2026-30/09/2026")
  date_span_match = re.search(r"^(.*?)(?:\s*(?:to|至|-|~)\s*)([0-9/.\-]+)$", ENV_MANUAL_INPUT, re.IGNORECASE)

  if time_range_match:
    raw_d = time_range_match.group(1).strip()
    t_start = time_range_match.group(2).strip()
    t_end = time_range_match.group(3).strip()
    if len(t_start.split(":")) == 2: t_start += ":00"
    if len(t_end.split(":")) == 2: t_end += ":59"

    parsed_dt = parse_any_date(raw_d)
    d_candidates = [raw_d]
    if parsed_dt:
      d_candidates.extend([
        parsed_dt.strftime("%d/%m/%Y"),
        f"{parsed_dt.day}/{parsed_dt.month}/{parsed_dt.year}",
        parsed_dt.strftime("%Y-%m-%d")
      ])
    d_candidates = list(dict.fromkeys(d_candidates))

    print("🎯 【精準時段重跑模式】:")
    print("  📅 日期標識:", d_candidates)
    print("  ⏰ 時間區間: %s ~ %s" % (t_start, t_end))
    sql_where_clause = "sentdate = ANY(%s) AND senttime >= %s AND senttime <= %s"
    sql_params = (d_candidates, t_start, t_end)

  elif date_span_match and parse_any_date(date_span_match.group(1)) and parse_any_date(date_span_match.group(2)):
    dt_start = parse_any_date(date_span_match.group(1))
    dt_end = parse_any_date(date_span_match.group(2))
    
    if dt_start > dt_end:
      dt_start, dt_end = dt_end, dt_start

    print(f"🎯 【跨日期長範圍批次模式】: {dt_start.strftime('%Y-%m-%d')} 至 {dt_end.strftime('%Y-%m-%d')}")
    d_candidates = []
    curr = dt_start
    while curr <= dt_end:
      d_candidates.extend([
        curr.strftime("%d/%m/%Y"),
        f"{curr.day}/{curr.month}/{curr.year}",
        curr.strftime("%Y-%m-%d")
      ])
      curr += timedelta(days=1)
    d_candidates = list(dict.fromkeys(d_candidates))
    print(f"  📅 已動態生成 {len(d_candidates)} 個日期字串候選索引！")
    sql_where_clause = "sentdate = ANY(%s)"
    sql_params = (d_candidates,)

  else:
    # 模式 3：單日全天
    raw_d = ENV_MANUAL_INPUT.strip()
    parsed_dt = parse_any_date(raw_d)
    d_candidates = [raw_d]
    if parsed_dt:
      d_candidates.extend([
        parsed_dt.strftime("%d/%m/%Y"),
        f"{parsed_dt.day}/{parsed_dt.month}/{parsed_dt.year}",
        parsed_dt.strftime("%Y-%m-%d")
      ])
    d_candidates = list(dict.fromkeys(d_candidates))
    print("🎯 【單日全天批次重跑模式】:")
    print("  📅 目標全天:", d_candidates)
    sql_where_clause = "sentdate = ANY(%s)"
    sql_params = (d_candidates,)
else:
  start_time_hk = now_hk - timedelta(minutes=INCREMENTAL_WINDOW_MINUTES)
  distinct_dates = list(set([
    start_time_hk.strftime("%d/%m/%Y"),
    now_hk.strftime("%d/%m/%Y")
  ]))
  start_time_str = start_time_hk.strftime("%H:%M:%S")
  end_time_str = now_hk.strftime("%H:%M:%S")

  print("\n🐘 啟動【增量模式（最近 %d 分鐘）】 (香港時區):" % INCREMENTAL_WINDOW_MINUTES)
  print("  📅 目標日期:", distinct_dates)
  print("  ⏰ 時間窗口: %s ~ %s (前 %d 分鐘)" % (start_time_str, end_time_str, INCREMENTAL_WINDOW_MINUTES))

  if len(distinct_dates) == 1:
    sql_where_clause = "sentdate = %s AND senttime >= %s AND senttime <= %s"
    sql_params = (distinct_dates[0], start_time_str, end_time_str)
  else:
    sql_where_clause = "((sentdate = %s AND senttime >= %s) OR (sentdate = %s AND senttime <= %s))"
    sql_params = (distinct_dates[0], start_time_str, distinct_dates[1], end_time_str)

# 🎯 若配置了指定 TARGET_GROUP_IDS，動態追加 GroupID 門禁
gid_where_clause = ""
if TARGET_GROUP_IDS:
  gid_where_clause = " AND gusid = ANY(%s)"
  sql_params = sql_params + (TARGET_GROUP_IDS,)
  print("🎯 【指定群組過濾啟用】僅查詢目標 Group IDs:", TARGET_GROUP_IDS)

raw_db_rows = []
try:
  conn = psycopg2.connect(**DB_CONFIG)
  with conn.cursor(cursor_factory=RealDictCursor) as cur:
    # 來源 view 名以 sql.Identifier 引用 (config.py 已做白名單驗證)；WHERE 子句為代碼內固定片段，值一律走參數
    query = sql.SQL("""
      SELECT 
        groupname,
        gusid,
        sentdate,
        senttime,
        userphone,
        messagebody,
        mediacaption,
        quotedmessage
      FROM {source_view}
      WHERE {where_clause} {gid_clause}
       AND messagebody IS NOT NULL 
       AND TRIM(messagebody) != ''
       AND messagebody != '[empty]'
      ORDER BY gusid, sentdate, senttime ASC;
    """).format(
      source_view=sql.Identifier(*SOURCE_VIEW.split(".")),
      where_clause=sql.SQL(sql_where_clause),
      gid_clause=sql.SQL(gid_where_clause),
    )
    cur.execute(query, sql_params)
    raw_db_rows = cur.fetchall()
  conn.close()
  stats["total_db_rows"] = len(raw_db_rows)
  print("✅ 成功從 PostgreSQL 抽取 %d 筆有效對話！" % stats["total_db_rows"])
except Exception as e:
  print("❌ PostgreSQL 連接或查詢失敗:", str(e))
  sys.exit(1)


# ==========================================
# 🧹 5. 層級匹配與前置去重
# ==========================================
def analyze_phone_quality(phone):
  phone_str = str(phone).strip()
  clean_digits = re.sub(r"\D", "", phone_str)
  if "@" in phone_str:
    if clean_digits.startswith("852") and len(clean_digits) == 11:
      return 4, clean_digits[3:]
    elif len(clean_digits) == 8 and clean_digits[0] in "456789":
      return 4, clean_digits
    return 1, clean_digits
     
  if re.match(r"^\d+$", phone_str):
    if phone_str.startswith("852") and len(phone_str) == 11:
      return 4, phone_str[3:]
    elif len(phone_str) == 8 and phone_str[0] in "456789":
      return 4, phone_str
    elif len(phone_str) >= 12:
      return 2, phone_str
    else:
      return 3, phone_str
       
  return 1, clean_digits

def extract_unique_kws_longest_match(text, target_kws, occupied_mask):
  if not text:
    return []
  matched = []
  text_lower = text.lower()
  
  for kw in target_kws:
    kw_l = kw.lower()
    # 判斷是否為純英文/字母縮寫 (如 OPO, RTF, HMO)
    is_pure_ascii = kw.isascii() and kw.isalpha()
    start = 0
    
    while True:
      idx = text_lower.find(kw_l, start)
      if idx == -1:
        break
      end = idx + len(kw_l)
      
      # 💡 ASCII 字母邊界保護：純英文詞前後不能緊接 [A-Za-z] (通殺 popo 誤中 OPO、portfolio 誤中 RTF)
      boundary_ok = True
      if is_pure_ascii:
        has_left_alpha = (idx > 0) and ('a' <= text[idx - 1].lower() <= 'z')
        has_right_alpha = (end < len(text)) and ('a' <= text[end].lower() <= 'z')
        if has_left_alpha or has_right_alpha:
          boundary_ok = False

      if boundary_ok and not any(occupied_mask[idx:end]):
        matched.append(kw)
        for i in range(idx, end):
          occupied_mask[i] = True
          
      start = idx + 1
  return matched

def parse_message_layers(text):
  """
  執行分層詞庫匹配：
  1. Exclude 佔據區間
  2. 正則 Brand 匹配
  3. 句內 Hard Brand 匹配 (含口語助詞穿透)
  4. 收集 COMBO 短稱候選 (供後續跨上下文解析)
  5. 提取 Formula_Feature 與 General
  """
  if not text:
    return [], [], [], []
   
  occupied = [False] * len(text)
  text_lower = text.lower()
   
  # 步驟 1：Exclude 佔據區間
  for ex in exclude_mask_words:
    ex_l = ex.lower()
    start = 0
    while True:
      idx = text_lower.find(ex_l, start)
      if idx == -1:
        break
      for i in range(idx, idx + len(ex_l)):
        occupied[i] = True
      start = idx + 1

  matched_hard_brand = []
  unresolved_combo_candidates = []
   
  # 步驟 2：提取 Brand 正則規則 (收集所有匹配項並按長度降序佔位，確保最長完整片段優先)
  regex_matches = []
  for reg, raw_kw, b_p, s_b, t_col in brand_regex_rules:
    for m in reg.finditer(text):
      regex_matches.append({
        "start": m.start(),
        "end": m.end(),
        "text": m.group(0),
        "target_col": t_col,
        "brand": b_p,
        "sub_brand": s_b
      })
  # 按匹配片段長度從長到短排序佔位
  regex_matches = sorted(regex_matches, key=lambda x: len(x["text"]), reverse=True)
  for rm in regex_matches:
    s, e = rm["start"], rm["end"]
    if not any(occupied[s:e]):
      # 💡 規範化 keywords 輸出：去除口語助詞（如「嘅、嗰、呢、個、出、隻」及空白），輸出標準名（如「皇家有機」）
      clean_kw = re.sub(r"[嘅嗰呢個出隻的之\s]+", "", rm["text"])
      matched_hard_brand.append({
        "matched_kw": clean_kw,
        "target_col": rm["target_col"],
        "brand": rm["brand"],
        "sub_brand": rm["sub_brand"],
        "confidence": "hard"
      })
      for i in range(s, e):
        occupied[i] = True

  # 步驟 3：提取 Brand 文字規則 (含同句 COMBO 判定與口語連接詞插花穿透)
  for rule in brand_text_rules:
    kw = rule["kw"]
    kw_l = kw.lower()
    m_type = rule["match_type"]
    c_with = rule["combo_with"]
     
    # ── COMBO 模式判定 ──
    if m_type == "COMBO" and c_with:
      combo_pattern = rf"({re.escape(kw)}[\w\W]{{0,6}}?{re.escape(c_with)}|{re.escape(c_with)}[\w\W]{{0,6}}?{re.escape(kw)})"
      combo_match = re.search(combo_pattern, text, re.IGNORECASE)

      if combo_match:
        s, e = combo_match.start(), combo_match.end()
        if not any(occupied[s:e]):
          # 💡 規範化：COMBO 穿透命中時，統一輸出規範組合詞（如「皇家有機」）
          clean_kw = f"{c_with}{kw}" if c_with in text_lower[:s+len(c_with)] else f"{kw}{c_with}"
          matched_hard_brand.append({
            "matched_kw": clean_kw,
            "target_col": rule["target_col"],
            "brand": rule["brand"],
            "sub_brand": rule["sub_brand"],
            "confidence": "hard"
          })
          for i in range(s, e):
            occupied[i] = True
      elif c_with in text_lower and kw_l in text_lower:
        # 距離大於 6 字符但同一發言內仍同時出現兩詞
        start = 0
        while True:
          idx = text_lower.find(kw_l, start)
          if idx == -1:
            break
          end = idx + len(kw_l)
          if not any(occupied[idx:end]):
            matched_hard_brand.append({
              "matched_kw": kw,
              "target_col": rule["target_col"],
              "brand": rule["brand"],
              "sub_brand": rule["sub_brand"],
              "confidence": "hard"
            })
            for i in range(idx, end):
              occupied[i] = True
          start = idx + 1
      else:
        # 單句不包含 combo_with -> 存入未決短稱清單 (不佔據 occupied，供跨上下文解析)
        if kw_l in text_lower:
          unresolved_combo_candidates.append({
            "matched_kw": kw,
            "combo_with": c_with,
            "target_col": rule["target_col"],
            "brand": rule["brand"],
            "sub_brand": rule["sub_brand"]
          })
    else:
      # ── CONTAINS 模式 ──
      start = 0
      while True:
        idx = text_lower.find(kw_l, start)
        if idx == -1:
          break
        end = idx + len(kw_l)
        if not any(occupied[idx:end]):
          matched_hard_brand.append({
            "matched_kw": kw,
            "target_col": rule["target_col"],
            "brand": rule["brand"],
            "sub_brand": rule["sub_brand"],
            "confidence": "hard"
          })
          for i in range(idx, end):
            occupied[i] = True
        start = idx + 1

  matched_formula = extract_unique_kws_longest_match(text, formula_feature_keywords, occupied)
  matched_general = extract_unique_kws_longest_match(text, general_keywords, occupied)

  return matched_hard_brand, unresolved_combo_candidates, matched_formula, matched_general


print("\n🧹 正在進行前置智能去重與特徵抽取...")
deduped_records_dict = {}
last_seen_tracker = {}

for row in raw_db_rows:
  body = str(row.get("messagebody") or "").strip()
  if body.lower() in ["nan", "null", "none", ""]:
    continue

  caption = str(row.get("mediacaption") or "").strip()
  if caption and caption.lower() not in ["nan", "null", "none"]:
    body = caption

  quoted = str(row.get("quotedmessage") or "").strip()
  if quoted.lower() in ["nan", "null", "none"]:
    quoted = ""

  gusid = str(row.get("gusid") or "").strip()
  if gusid.endswith(".0"):
    gusid = gusid[:-2]
  group_name = group_map.get(gusid, str(row.get("groupname") or "").strip())

  phone_raw = str(row.get("userphone") or "").strip()
  curr_score, curr_clean_digits = analyze_phone_quality(phone_raw)
  internal_flag = get_internal_status(curr_clean_digits)

  date_val = str(row.get("sentdate") or "").strip()
  time_val = str(row.get("senttime") or "").strip()

  cleaned_body_fp = re.sub(r"\s+", "", body)
  base_fingerprint = str(gusid) + "___" + str(cleaned_body_fp)

  current_time_obj = pd.to_datetime(str(date_val) + " " + str(time_val), errors="coerce", dayfirst=True)

  if base_fingerprint not in last_seen_tracker:
    last_seen_tracker[base_fingerprint] = []

  is_merged = False
  for old_info in last_seen_tracker[base_fingerprint]:
    old_time_obj = old_info["time_obj"]
    old_key = old_info["key"]
    old_time_str = old_info["time_str"]

    if pd.notna(current_time_obj) and pd.notna(old_time_obj):
      time_diff = abs((current_time_obj - old_time_obj).total_seconds())
    else:
      time_diff = 999999

    is_exact_same_time = (time_val == old_time_str) and bool(time_val)

    if is_exact_same_time or time_diff <= TIME_TOLERANCE_SECONDS:
      should_merge = True
      old_phone = str(deduped_records_dict[old_key]["userPhone"]).strip()
      old_score, old_clean_digits = analyze_phone_quality(old_phone)

      if old_score >= 4 and curr_score >= 4:
        if old_clean_digits != curr_clean_digits:
          should_merge = False

      if should_merge:
        if curr_score > old_score:
          deduped_records_dict[old_key]["userPhone"] = phone_raw
          deduped_records_dict[old_key]["phone_score"] = curr_score
          deduped_records_dict[old_key]["phone_clean"] = curr_clean_digits
        if internal_flag and not deduped_records_dict[old_key]["Internal"]:
          deduped_records_dict[old_key]["Internal"] = internal_flag
        is_merged = True
        break

  if not is_merged:
    b_hard_ev, b_unres_combo, b_form, b_gen = parse_message_layers(body)
    q_hard_ev, q_unres_combo, q_form, q_gen = parse_message_layers(quoted)

    # 嚴格隔離：keywords 欄位只填 messageBody 自身命中的詞彙，quoted 絕不污染！
    body_brand_kws = [item["matched_kw"] for item in b_hard_ev]
    body_kws_only = list(dict.fromkeys(body_brand_kws + b_form + b_gen))

    # 門禁：正文或引用命中品牌證據或奶粉強特徵
    base_should_ai = bool(b_hard_ev or b_form or q_hard_ev or q_form)

    record = {
      "Group": group_name,
      "GroupID": gusid,
      "Date": date_val,
      "Time": time_val,
      "time_obj": current_time_obj,
      "userPhone": phone_raw,
      "phone_score": curr_score,
      "phone_clean": curr_clean_digits,
      "Internal": internal_flag,
      "quotedMessage": quoted,
      "messageBody": body,
      "reply": "",
      "brand": "",
      "keywords": ", ".join(body_kws_only),
      "warning": "",
      "Other_Brands": "",
      "should_ai": base_should_ai,
      "direct_brand_evidence": b_hard_ev,
      "quoted_brand_evidence": q_hard_ev,
      "unresolved_combos": b_unres_combo,
      "has_body_formula": bool(b_form),
      "has_quoted_formula": bool(q_form),
      "body_formula_kws": b_form,
      "contextual_alias_evidence": [],
      "context_history_msgs": [],
      "context_brand_evidence": [],
      "context_formula_kws": []
    }
    for b in STANDARD_BRANDS:
      record[b] = ""

    unique_key = "rec_" + str(len(deduped_records_dict))
    deduped_records_dict[unique_key] = record

    last_seen_tracker[base_fingerprint].append({
      "time_obj": current_time_obj,
      "time_str": time_val,
      "key": unique_key
    })

cleaned_records = list(deduped_records_dict.values())
stats["total_deduped_rows"] = len(cleaned_records)


# ==========================================
# ⏱️ 5.1 上下文回溯組裝與【時間衰減短稱解析】
# ==========================================
records_by_group = {}
for r in cleaned_records:
  gid = r["GroupID"]
  if gid not in records_by_group:
    records_by_group[gid] = []
   
  curr_time = r["time_obj"]
  recent_history = records_by_group[gid][-CONTEXT_HISTORY_LIMIT:]
   
  # ⏱️ 時間維度門禁：前文發言必須在 CONTEXT_HISTORY_MAX_MINUTES (30分鐘) 內
  valid_time_history = []
  for prev in recent_history:
    prev_time = prev.get("time_obj")
    if pd.notna(curr_time) and pd.notna(prev_time):
      diff_mins = (curr_time - prev_time).total_seconds() / 60.0
      if 0 <= diff_mins <= CONTEXT_HISTORY_MAX_MINUTES:
        valid_time_history.append(prev)
    else:
      valid_time_history.append(prev)

  history_msgs = []
  collected_ctx_evidence = []
  collected_ctx_formula = []

  for prev in valid_time_history:
    if prev["messageBody"] and prev["messageBody"] not in ["[empty]", "image", "video"]:
      history_msgs.append(str(prev["messageBody"]))
      if prev.get("direct_brand_evidence"):
        collected_ctx_evidence.extend(prev["direct_brand_evidence"])
      if prev.get("contextual_alias_evidence"):
        collected_ctx_evidence.extend(prev["contextual_alias_evidence"])
      if prev.get("body_formula_kws"):
        collected_ctx_formula.extend(prev["body_formula_kws"])

  r["context_history_msgs"] = history_msgs
  r["context_brand_evidence"] = collected_ctx_evidence
  r["context_formula_kws"] = list(dict.fromkeys(collected_ctx_formula))

  # 🚀 跨引用產品短稱解析 (Contextual Alias Resolution)
  if ENABLE_CONTEXTUAL_ALIAS and r["unresolved_combos"]:
    quoted_lower = r["quotedMessage"].lower()
    for combo_item in r["unresolved_combos"]:
      c_with = combo_item["combo_with"]
      alias_kw = combo_item["matched_kw"]
      is_unlocked = False
      unlock_reason = ""

      # 門禁 A: 主動引用 (QuotedMessage) 強關係解鎖
      if r["quotedMessage"]:
        if c_with in quoted_lower:
          is_unlocked = True
          unlock_reason = f"Quoted 包含母品牌詞 '{c_with}'"
        elif r["has_quoted_formula"] or r["has_body_formula"]:
          is_unlocked = True
          unlock_reason = f"Quoted/Body 具備強配方奶特徵，並延續短稱 '{alias_kw}'"

      # 門禁 B: 前文隊列 (History) 雙重風控解鎖 (僅在 30 分鐘有效窗口內)
      if not is_unlocked and valid_time_history:
        prev_has_combo_with = any(c_with in str(p["messageBody"]).lower() for p in valid_time_history)
        if prev_has_combo_with:
          is_unlocked = True
          unlock_reason = f"30分鐘內前文曾提及母品牌詞 '{c_with}'"
        else:
          # 僅前文有配方奶特徵 -> 嚴格限制緊鄰上一條 (Distance=1)
          last_prev = valid_time_history[-1]
          if last_prev.get("has_body_formula"):
            is_unlocked = True
            unlock_reason = f"緊鄰上一條發言包含配方奶特徵，並接續短稱 '{alias_kw}'"

      if is_unlocked:
        r["contextual_alias_evidence"].append({
          "matched_kw": alias_kw,
          "target_col": combo_item["target_col"],
          "brand": combo_item["brand"],
          "sub_brand": combo_item["sub_brand"],
          "confidence": "contextual_inferred",
          "reason": unlock_reason
        })
        r["should_ai"] = True
        stats["contextual_alias_triggered"] += 1

  records_by_group[gid].append(r)

stats["need_ai_processing"] = sum(1 for r in cleaned_records if r["should_ai"])

print_stage_dashboard(
  "前置去重與候選分析完成",
  {
    "💬 資料庫原始筆數": "%d 行" % stats["total_db_rows"],
    "📝 去重後保留唯一數": "%d 行" % stats["total_deduped_rows"],
    "🎯 觸發需 AI 分析數": "%d 行" % stats["need_ai_processing"],
    "🔍 跨引用短稱捕獲數": "%d 次 (Contextual Alias)" % stats["contextual_alias_triggered"],
    "⏱️ 前文時間衰減上限": "%d 分鐘" % CONTEXT_HISTORY_MAX_MINUTES,
  }
)


# ==========================================
# 🤖 6. 核心機制 2：候選閉環約束 (Candidate-Constrained Extraction)
# ==========================================
def request_poe_api(prompt_text):
  if hasattr(poe_client, "responses") and hasattr(poe_client.responses, "create"):
    try:
      resp = poe_client.responses.create(model=POE_MODEL, input=prompt_text)
      if hasattr(resp, "output_text"):
        return resp.output_text
    except Exception:
      pass

  headers = {"Authorization": "Bearer " + str(POE_API_KEY), "Content-Type": "application/json"}
  payload = {"model": POE_MODEL, "input": prompt_text}
  http_resp = requests.post("https://api.poe.com/v1/responses", json=payload, headers=headers, timeout=60)
  if http_resp.status_code == 200:
    data = http_resp.json()
    return data.get("output_text") or data.get("text", "")
  else:
    raise Exception("HTTP " + str(http_resp.status_code) + " - " + str(http_resp.text[:200]))

def check_ai_service_health():
  print("\n🩺 正在嗅探 Poe AI 服務連通性與帳號餘額...")
  test_prompt = 'Hello, please reply with JSON: {"status": "ok"}'
  try:
    res = request_poe_api(test_prompt)
    if "ok" in res.lower() or "{" in res:
      print("✅ Poe AI 服務檢測正常，帳號餘額充足，準備開始批量分析！\n")
      return True
    else:
      raise ValueError("AI 回應格式異常: " + str(res[:100]))
  except Exception as e:
    print("\n" + "!" * 65)
    print("🚨 【緊急熔斷觸發】Poe AI 服務無法正常工作！")
    print("👉 錯誤詳情:", str(e))
    print("🛑 系統已自動中止所有後續寫入流程！")
    print("!" * 65 + "\n")
    return False

def call_llm_analysis(body_text, quoted_text, context_list, eligible_candidates_desc, max_retries=3):
  global consecutive_ai_failures
   
  if abort_event.is_set():
    return False, False, [], ""

  context_str = "\n".join(["[前文發言 " + str(idx+1) + "]: " + str(c) for idx, c in enumerate(context_list)]) if context_list else "無前文記錄"
  quoted_str = quoted_text if quoted_text else "無引用消息"

  prompt = """# Role
你是一位具備 15 年育兒經驗的香港母親，同時擔任頂級母嬰品牌公關與客服風控專家。你精通香港/廣東話社群俚語、常見錯別字及指代習慣。

# Task
解構香港媽媽群聊留言，判斷「是否為無效訊息 (isSpam)」並提取「討論的奶粉品牌/系列與評價立場 (opinions)」。

# 垃圾/商業過濾 (isSpam 判定 - 極度嚴格)：
凡符合以下任一特徵，一律標記為 isSpam: true，且 opinions 強制為空 []，reply_origin 強制為空字串 ""：
1. 純二手交易/收購/轉讓/交換：徵收、二手買賣、平放、全新未開、有意pm、出讓奶粉券、交換禮品。
2. 促銷轉發/代購報價/積分轉讓：
  - 出現「幫忙儲分」、「代儲分」、「yuu」、「萬寧88折」、「折後$XXX」、「指定門市地址」等純藥房超市格價或折扣券轉發。

# 【候選閉環與防腦補鐵律】(極度重要！違反將直接判定為重大事故)：
1. 💡 本次對話經規則引擎字面檢索到的【合法候選實體清單】：
""" + eligible_candidates_desc + """
  - ⚠️ 只有在上述候選清單內出現的品牌，或者發言原文白紙黑字寫出的真實競品，才允許輸出！
  - ⚠️ 若候選標註為【上下文推斷短稱】（如『皇家』、『白金』）：
      * 必須結合引用 (Quoted) 或前文語境，確認其確實指代嬰幼兒配方奶粉，才可確認該候選！
      * 若原文為非奶粉領域（如皇家馬德里、皇家酒店、泛稱），opinions 必須強制為空 []！
  - ⚠️ 嚴禁任何脫離字面證據的聯想！「水解」、「鐵質」、「益生菌」、「乳鐵蛋白」是通用成分，若無具體品牌字眼，絕對禁止判定為任何品牌！
  - ⚠️ 香港公立醫院名（「威記」、「威院」、「瑪麗」）及通用名詞（「屋仔奶」、「鮮奶」），嚴禁腦補為奶粉品牌！

2. 嚴格指代溯源與 reply_origin 規範：
  - 當本句沒有直接提品牌名，而是在接續、回覆、評論前文所討論的奶粉話題時（例如：「因為有乳鐵蛋白喎」、「我都係咁」），【必須】填入它所承接的那一條前文留言的原話全文（原封不動複製，保留 Emoji 與口語詞）！
  - 若前文完全沒有提及奶粉或品牌話題，或者本句自成獨立話題，reply_origin 必須強制為空字串 ""！

# 立場情感 (sentiment) 評判標準：
- "P" (正面): 讚賞、推介、成分好、長肉長磅、便便順暢、整體優點大於缺點。
- "N" (負面): 針對產品本身的嚴重指控、不良反應（便秘、羊咩屎、嚴重肚脹、腹瀉、起濕疹紅點、極難溶結塊）或重大危機。轉奶因負面原因轉走算 N。
- "I" (中立/客觀): 純詢問、客觀陳述、單純轉奶意向但無評價。提及贈品、活動登記但未評價產品質量本身，判 I。BB 挑食不肯喝只判 I。

# 輸出 JSON 格式 (嚴禁輸出 Markdown 或其他文字)：
{
 "isSpam": false,
 "reply_origin": "",
 "opinions": [
  {
   "brand_name": "候選清單中的標準名稱或真實競品名",
   "sentiment": "P/N/I",
   "raw_mention": "原文詞彙或推斷依據"
  }
 ]
}

# 當前對話語境：
【同一群組前文對話】：
""" + context_str + """

【本則引用的消息 (Quoted)】：
""" + quoted_str + """

【目標分析留言】：
""" + body_text

  attempt = 0
  while attempt < max_retries:
    try:
      res_text = request_poe_api(prompt)
      json_match = re.search(r"\{.*\}", res_text, re.DOTALL)
      if json_match:
        result = json.loads(json_match.group(0))
        with stats_lock:
          consecutive_ai_failures = 0
         
        raw_origin = str(result.get("reply_origin", "") or "").strip()
        cleaned_origin = re.sub(r"^(\[前文發言\s*\d+\]:\s*|發言:\s*|\d{8,15}\s*:\s*)", "", raw_origin).strip()
        if any(bad in cleaned_origin for bad in ["放棄關聯", "純交易", "直接提及", "無具體品牌", "轉讓"]):
          cleaned_origin = ""

        return True, result.get("isSpam", False), result.get("opinions", []), cleaned_origin
      else:
        raise ValueError("No JSON found in LLM response")
    except Exception:
      attempt += 1
      if attempt < max_retries:
        time.sleep(2)
      else:
        with stats_lock:
          consecutive_ai_failures += 1
          if consecutive_ai_failures >= MAX_CONSECUTIVE_FAILURES:
            abort_event.set()
            print("\n🚨 【運行中熔斷】已連續 %d 次 AI 調用失敗，終止後續任務！" % consecutive_ai_failures)
        return False, False, [], ""

def process_record_ai(record):
  if abort_event.is_set():
    return record, False, False, [], ""

  all_evidences = (
    record["direct_brand_evidence"] 
    + record["quoted_brand_evidence"] 
    + record["context_brand_evidence"]
    + record["contextual_alias_evidence"]
  )
   
  if not all_evidences:
    if record["context_formula_kws"] or record["has_body_formula"]:
      formula_hints = ", ".join(record["context_formula_kws"] + record["body_formula_kws"])
      eligible_desc = f"【話題包含配方特徵但無具體品牌】: (提及: {formula_hints})。若前文未明確提及品牌，嚴禁腦補品牌，opinions 必須為空 []；但若本句是在接續前文討論，可正常溯源 reply_origin。"
    else:
      eligible_desc = "【無任何字面品牌證據】：本對話鏈完全未出現品牌，嚴禁輸出任何品牌，opinions 必須為空 []。"
  else:
    distinct_candidates = []
    for ev in all_evidences:
      t_col = ev.get("target_col") or ev.get("brand")
      matched_k = ev.get("matched_kw")
      conf = ev.get("confidence", "hard")
       
      if conf == "contextual_inferred":
        desc = f"- 標準名稱: [{t_col}] (⚠️ 上下文推斷詞: '{matched_k}', 依據: {ev.get('reason')})"
      else:
        desc = f"- 標準名稱: [{t_col}] (原文命中: '{matched_k}')"
         
      if desc not in distinct_candidates:
        distinct_candidates.append(desc)
    eligible_desc = (
      "⚠️ 請注意：輸出 opinions 時，brand_name 必須嚴格填寫括號 [] 內的【標準名稱】，嚴禁輸出拼音或自創代碼！\n"
      + "\n".join(distinct_candidates)
    )

  success, is_spam, opinions, reply_origin = call_llm_analysis(
    record["messageBody"],
    record["quotedMessage"],
    record["context_history_msgs"],
    eligible_desc
  )
  return record, success, is_spam, opinions, reply_origin

print("\n🤖 開始進行第二階段：AI 語義識別與【閉環候選審查】...")
records_to_process = [r for r in cleaned_records if r["should_ai"]]
total_ai_tasks = len(records_to_process)
processed_counter = 0

if total_ai_tasks > 0:
  if not check_ai_service_health():
    sys.exit(1)

  with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    futures = {executor.submit(process_record_ai, r): r for r in records_to_process}
    for future in concurrent.futures.as_completed(futures):
      if abort_event.is_set():
        print("\n🛑 熔斷器已開啟，放棄剩餘任務，退出程式！")
        sys.exit(1)

      record, success, is_spam, opinions, reply_origin = future.result()
      processed_counter += 1
      print("\r 🔄 AI 處理進度: %d / %d" % (processed_counter, total_ai_tasks), end="", flush=True)

      if success:
        with stats_lock:
          stats["ai_actually_processed"] += 1

        # 💡 原句 100% 還原邏輯
        final_reply = ""
        if reply_origin and not record["quotedMessage"]:
          cleaned_needle = reply_origin.strip()
          for ctx_msg in reversed(record["context_history_msgs"]):
            full_msg_text = str(ctx_msg).strip()
            if cleaned_needle in full_msg_text or full_msg_text in cleaned_needle:
              final_reply = full_msg_text
              break
          if not final_reply:
            final_reply = cleaned_needle
         
        if record["quotedMessage"]:
          record["reply"] = ""
        else:
          record["reply"] = final_reply

        if is_spam:
          with stats_lock:
            stats["spam_detected"] += 1
          record["brand"] = ""
          record["keywords"] = ""
          record["reply"] = ""
          for b in STANDARD_BRANDS:
            record[b] = ""
          record["Other_Brands"] = ""
          record["warning"] = ""
        else:
          other_brands_collected = []
          standard_brand_hit = False
          parent_rollup_collector = {p: [] for p in MASTER_BRAND_ROLLUP.keys()}

          all_evidences = (
            record["direct_brand_evidence"] 
            + record["quoted_brand_evidence"] 
            + record["context_brand_evidence"]
            + record["contextual_alias_evidence"]
          )
          valid_target_cols = set([ev["target_col"] for ev in all_evidences if ev.get("target_col")])

          # 1. 處理 AI 成功分析出的 opinions (具備證據動態歸一化能力)
          for op in opinions:
            raw_b_name = op.get("brand_name", "").strip()
            s_val = op.get("sentiment", "I").strip()
            resolved_col = ""

            # A. 若直接等於 35 個標準欄位名 (大小寫不敏感匹配)
            for std_b in STANDARD_BRANDS:
              if raw_b_name.lower() == std_b.lower():
                resolved_col = std_b
                break

            # B. 通用動態證據對齊：若 AI 輸出了 "Friso - bio", "皇家", "金裝", "bio" 等
            if not resolved_col:
              b_norm = re.sub(r"[\s\-_]+", "", raw_b_name.lower())
              for ev in all_evidences:
                t_col = ev.get("target_col")
                s_b = str(ev.get("sub_brand") or "").lower()
                b_p = str(ev.get("brand") or "").lower()
                m_k = str(ev.get("matched_kw") or "").lower()

                # 比對 sub_brand (如 bio), brand-sub (如 frisobio), 或 matched_kw (如 皇家, 金裝)
                if (
                  b_norm == s_b or 
                  b_norm == f"{b_p}{s_b}" or 
                  b_norm == m_k or 
                  m_k in b_norm or 
                  b_norm in m_k
                ):
                  resolved_col = t_col
                  break

            # C. S26 / Wyeth 特殊兜底
            if not resolved_col and ("s26" in raw_b_name.lower() or "wyeth" in raw_b_name.lower()):
              if "ultima" in raw_b_name.lower(): resolved_col = "S26 Ultima"
              elif "gold" in raw_b_name.lower(): resolved_col = "S26 Gold"
              elif "ascenda" in raw_b_name.lower(): resolved_col = "Wyeth Ascenda"
              else: resolved_col = "Wyeth"

            # ── 寫入判定 ──
            # 💡 預設要求當前句自己或 Quoted 有明確品牌證據才打標
            has_self_evidence = bool(record["direct_brand_evidence"] or record["quoted_brand_evidence"])
            allow_tag = CONTEXT_ONLY_TAGGING or has_self_evidence

            if resolved_col and resolved_col in STANDARD_BRANDS and allow_tag:
              record[resolved_col] = s_val
              standard_brand_hit = True

              for p_brand, sub_list in MASTER_BRAND_ROLLUP.items():
                if resolved_col in sub_list or resolved_col == p_brand:
                  parent_rollup_collector[p_brand].append(s_val)

            elif raw_b_name and allow_tag:
              # 只有真正次要競品才進 Other_Brands
              other_brands_collected.append(raw_b_name + "(" + s_val + ")")

          # 💡 硬事實字面保底機制 (Hard Fact Guarantee)
          # 非 Spam 且正文自身白紙黑字命中品牌實體，即使 AI 給出空 opinions，底層強制填入 "I" (中立/客觀)
          if not standard_brand_hit and record["direct_brand_evidence"]:
            for dir_ev in record["direct_brand_evidence"]:
              t_col = dir_ev.get("target_col")
              if t_col and t_col in STANDARD_BRANDS:
                record[t_col] = "I"
                standard_brand_hit = True
                for p_brand, sub_list in MASTER_BRAND_ROLLUP.items():
                  if t_col in sub_list or t_col == p_brand:
                    parent_rollup_collector[p_brand].append("I")
            if standard_brand_hit:
              with stats_lock:
                stats["hard_fact_guaranteed_count"] += 1

          # 母品牌向上匯總 Rollup (負面 N 優先)
          for p_brand, s_list in parent_rollup_collector.items():
            if s_list:
              if "N" in s_list:
                record[p_brand] = "N"
              elif "P" in s_list:
                record[p_brand] = "P"
              elif "I" in s_list:
                record[p_brand] = "I"

          # 只有 35 個標準項命中時 brand 置為 1
          if standard_brand_hit:
            record["brand"] = "1"
            with stats_lock:
              stats["brand_identified_count"] += 1
          else:
            record["brand"] = ""
            with stats_lock:
              stats["generic_no_brand"] += 1

          # 💡 優化一：【嚴格門禁：沒有 brand=1 絕對不顯示 reply】
          # 無論有無次要競品，只要 35 個標準項未命中 (brand != "1")，強制清空 reply，徹底凈化報表！
          if record["brand"] != "1":
            record["reply"] = ""
          elif record["reply"]:
            # 只有最終保留有效 reply 的記錄，才計入關聯統計看板
            with stats_lock:
              stats["context_attributed_count"] += 1

          # 美素負面預警
          if record.get("Friso") == "N":
            record["warning"] = "✓"

          if other_brands_collected:
            record["Other_Brands"] = "; ".join(other_brands_collected)
  print()


# ==========================================
# 💾 7. 雙軌寫入：Google Sheets (48欄) + Supabase (48欄)
# ==========================================
print("\n💾 正在整理資料並準備執行雙軌寫入 (48 欄位標準格式)...")
final_df = pd.DataFrame(cleaned_records)

def parse_hk_date_to_iso(date_str):
  """
  嚴格將香港常見的 DD/MM/YYYY 或其他格式安全轉換為 ISO 8601 (YYYY-MM-DD)
  徹底消除 02/10/2026 被誤認為 2月10日的倒置 Bug！
  """
  if not date_str:
    return None
  d_str = str(date_str).strip()
  
  # 優先按香港常用 DD/MM/YYYY 或 DD-MM-YYYY 解析
  for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%Y-%m-%d", "%Y/%m/%d"]:
    try:
      return datetime.strptime(d_str, fmt).strftime("%Y-%m-%d")
    except ValueError:
      pass
  
  # 若未精確匹配，使用 dayfirst=True 強制日優先解析
  dt = pd.to_datetime(d_str, dayfirst=True, errors="coerce")
  if pd.notna(dt):
    return dt.strftime("%Y-%m-%d")
  return d_str

def escape_sheet_formula(val):
  if not isinstance(val, str):
    return val
  stripped = val.lstrip()
  if stripped and stripped[0] in ("=", "+", "-", "@"):
    return "\u200b" + val
  return val

def get_target_sheet_name(date_str):
  try:
    iso_date = parse_hk_date_to_iso(date_str)
    dt = pd.to_datetime(iso_date)
    yy = dt.strftime("%y")
    mm = dt.strftime("%m")
    dd = dt.day
    part = "Part1" if dd <= 15 else "Part2"
    return "%s%s_DailyData_%s" % (yy, mm, part)
  except Exception:
    return "Unknown_Sheet"

@with_retry(max_retries=5, base_delay=3)
def append_to_google_sheet_safe(worksheet, rows_data):
  worksheet.append_rows(rows_data, value_input_option="USER_ENTERED")

if not final_df.empty:
  for col in FINAL_HEADERS_48:
    if col not in final_df.columns:
      final_df[col] = ""
  full_48_df = final_df[FINAL_HEADERS_48].copy()

  # ── 7.1 寫入 Google Sheets ──
  sheets_df = full_48_df.copy()
  # 💡 強制統一為 YYYY-MM-DD，Google Sheets 100% 識別為日期，且永不混淆月份與日！
  sheets_df["Date"] = sheets_df["Date"].apply(parse_hk_date_to_iso)
  for text_col in ["messageBody", "quotedMessage", "reply"]:
    sheets_df[text_col] = sheets_df[text_col].apply(escape_sheet_formula)

  is_test_mode = bool(
    TEST_TARGET_SHEET_URL 
    and "your_test_sheet_id" not in TEST_TARGET_SHEET_URL 
    and TEST_TARGET_SHEET_URL.strip().startswith("https://docs.google.com")
  )

  if is_test_mode:
    print("\n🧪 【測試模式啟用】強制寫入指定測試 Sheet！")
    print("👉 測試目標: TEST_TARGET_SHEET_URL (已設定)")
    try:
      sh = gc.open_by_url(TEST_TARGET_SHEET_URL)
      worksheet = sh.worksheet(TEST_WORKSHEET_TAB)
      append_to_google_sheet_safe(worksheet, sheets_df.values.tolist())
      print("✅ [測試表] 成功寫入 %d 筆 48 欄測試數據到 【%s】！" % (len(sheets_df), TEST_WORKSHEET_TAB))
    except Exception as e:
      print("❌ [測試表] 寫入測試表格失敗:", str(e))
  else:
    print("\n🚀 【生產模式】按日期動態分表寫入...")
    sheets_df["TargetSheet"] = sheets_df["Date"].apply(get_target_sheet_name)

    for sheet_name, group_df in sheets_df.groupby("TargetSheet"):
      if sheet_name == "Unknown_Sheet":
        continue
      write_df = group_df.drop(columns=["TargetSheet"]).fillna("")
      print("🔄 [Google Sheets] 正在寫入工作表 【" + sheet_name + "】 (" + str(len(write_df)) + " 筆資料)...")
      try:
        sh = gc.open(sheet_name)
        worksheet = sh.worksheet(MASTER_WORKSHEET_NAME)
        append_to_google_sheet_safe(worksheet, write_df.values.tolist())
        print("✅ [Google Sheets] 成功寫入 " + str(len(write_df)) + " 筆資料到 【" + sheet_name + "】！")
      except gspread.exceptions.SpreadsheetNotFound:
        print("❌ [Google Sheets] 找不到名為【" + sheet_name + "】的表格，請確認已共用給服務帳戶！")
      except Exception as e:
        print("❌ [Google Sheets] 寫入【" + sheet_name + "】失敗:", str(e))

  # ── 7.2 寫入 Supabase 全量表 (48 欄) ──
  supabase_host = SUPABASE_DB_CONFIG.get("host", "").strip()
  supabase_pw = SUPABASE_DB_CONFIG.get("password", "").strip()

  if supabase_host and supabase_pw:
    print("\n⚡ [Supabase] 正在批次寫入全量表 【" + SUPABASE_FULL_TABLE + "】...")
    try:
      db_cols = [
        '"Group"', '"GroupID"', '"Date"', '"Time"', '"userPhone"', '"Internal"',
        '"quotedMessage"', '"messageBody"', '"reply"', '"brand"', '"keywords"', '"warning"',
        '"Abbott"', '"Similac HMO"', '"Similac Comfort"', '"Abbott PediaSure"',
        '"Aptamil"', '"Apta APF"', '"Apta NEO"', '"Apta PHP"',
        '"C&G"',
        '"Friso"', '"Friso Gold"', '"Friso Prestige"', '"Friso Bio"', '"Friso Signature"', '"Friso Kids"',
        '"HiPP"',
        '"Illuma"', '"Illuma Luxa"', '"Illuma Organic"', '"Illuma A2"', '"Illuma XtraCare"',
        '"MJ"', '"Enfinitas"', '"MJ A+"', '"MJ NeuroPro"', '"MJ Gentle Care"', '"MJ NutriPower"',
        '"Nestle"', '"NAN HA"', '"NAN Infini Pro"', '"NAN A2"',
        '"Wyeth"', '"S26 Ultima"', '"S26 Gold"', '"Wyeth Ascenda"',
        '"Other_Brands"'
      ]

      insert_rows = []
      for _, r in full_48_df.iterrows():
        d_val = str(r["Date"]).strip()
        date_str = parse_hk_date_to_iso(d_val)

        t_val = str(r["Time"]).strip()
        time_str = t_val if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", t_val) else None

        row_tuple = (
          r.get("Group") or None,
          r.get("GroupID") or None,
          date_str,
          time_str,
          r.get("userPhone") or None,
          r.get("Internal") or None,
          r.get("quotedMessage") or None,
          r.get("messageBody") or None,
          r.get("reply") or None,
          r.get("brand") or None,
          r.get("keywords") or None,
          r.get("warning") or None,
          r.get("Abbott") or None,
          r.get("Similac HMO") or None,
          r.get("Similac Comfort") or None,
          r.get("Abbott PediaSure") or None,
          r.get("Aptamil") or None,
          r.get("Apta APF") or None,
          r.get("Apta NEO") or None,
          r.get("Apta PHP") or None,
          r.get("C&G") or None,
          r.get("Friso") or None,
          r.get("Friso Gold") or None,
          r.get("Friso Prestige") or None,
          r.get("Friso Bio") or None,
          r.get("Friso Signature") or None,
          r.get("Friso Kids") or None,
          r.get("HiPP") or None,
          r.get("Illuma") or None,
          r.get("Illuma Luxa") or None,
          r.get("Illuma Organic") or None,
          r.get("Illuma A2") or None,
          r.get("Illuma XtraCare") or None,
          r.get("MJ") or None,
          r.get("Enfinitas") or None,
          r.get("MJ A+") or None,
          r.get("MJ NeuroPro") or None,
          r.get("MJ Gentle Care") or None,
          r.get("MJ NutriPower") or None,
          r.get("Nestle") or None,
          r.get("NAN HA") or None,
          r.get("NAN Infini Pro") or None,
          r.get("NAN A2") or None,
          r.get("Wyeth") or None,
          r.get("S26 Ultima") or None,
          r.get("S26 Gold") or None,
          r.get("Wyeth Ascenda") or None,
          r.get("Other_Brands") or None
        )
        insert_rows.append(row_tuple)

      if insert_rows:
        sp_conn = psycopg2.connect(**SUPABASE_DB_CONFIG)
        with sp_conn.cursor() as cur:
          insert_query = "INSERT INTO public." + SUPABASE_FULL_TABLE + " (" + ", ".join(db_cols) + ") VALUES %s;"
          execute_values(cur, insert_query, insert_rows, page_size=1000)
          sp_conn.commit()
        sp_conn.close()
        print("✅ [Supabase] 成功批次寫入 %d 筆 48 欄資料到 【%s】！" % (len(insert_rows), SUPABASE_FULL_TABLE))
    except Exception as e:
      print("❌ [Supabase] 寫入全量表失敗:", str(e))
  else:
    if not supabase_pw:
      print("\n⚠️ [Supabase] 檢測到 SUPABASE_DB_PASSWORD 為空，已略過 Supabase 寫入。")
    else:
      print("\nℹ️ [Supabase] 略過 Supabase 寫入。")
else:
  print("⚠️ 目標時間窗口無有效資料可寫入。")

# ==========================================
# 📊 8. 全景監控統計看板
# ==========================================
print_stage_dashboard(
  "自動化任務完成 (全景業務指標)",
  {
    "💬 資料庫原始總數": "%d 行" % stats["total_db_rows"],
    "📝 前置去重後唯一數": "%d 行" % stats["total_deduped_rows"],
    "🎯 AI 實際處理筆數": "%d 行" % stats["ai_actually_processed"],
    "🔍 短稱跨引用捕獲數": "%d 行 (Contextual Inferred)" % stats["contextual_alias_triggered"],
    "🛡️ 硬事實字面保底數": "%d 行 (防 AI 漏標兜底 I)" % stats["hard_fact_guaranteed_count"],
    "🏷️ 明確命中品牌數": "%d 行 (35項目有填入，brand=1)" % stats["brand_identified_count"],
    "🔗 成功關聯前文數": "%d 行 (有效 reply 溯源)" % stats["context_attributed_count"],
    "🍼 泛育兒(無品牌)數": "%d 行" % stats["generic_no_brand"],
    "🗑️ 標記為 Spam 垃圾數": "%d 行" % stats["spam_detected"],
    "📝 最終寫入總筆數": "%d 行" % len(final_df)
  }
)
