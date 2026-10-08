# Historical sentiment repair. Reads the sanitized output table and re-scores
# rows whose quoted text may have been attributed to the wrong brand.
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
  KEYWORDS_SPREADSHEET_ID,
  BRAND_SHEET_NAME,
  IFT_SHEET_NAME,
  SUPABASE_DB_CONFIG,
  SUPABASE_FULL_TABLE,
)
from google_auth import get_google_clients
import openai
import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor, execute_values
import requests

CODE_TO_COLUMN_MAP = {
  ("abbott", "master"): "Abbott Master Brand",
  ("abbott", "similac_hmo"): "Similac HMO",
  ("abbott", "similac_comfort"): "Similac Comfort",
  ("abbott", "pediasure"): "Abbott PediaSure",
  ("aptamil", "master"): "Aptamil Master Brand",
  ("aptamil", "apf"): "Apta APF",
  ("aptamil", "neo"): "Apta NEO",
  ("aptamil", "php"): "Apta PHP",
  ("aptamil", "essensis"): "Aptamil Master Brand",
  ("cow & gate", "master"): "C&G Master Brand",
  ("cow & gate", "a2_beta_casein"): "C&G Master Brand",
  ("friso", "master"): "Friso Master Brand",
  ("friso", "gold"): "Friso Gold",
  ("friso", "prestige"): "Friso Prestige",
  ("friso", "bio"): "Friso Bio",
  ("friso", "signature"): "Friso Signature",
  ("friso", "kids"): "Friso Kids",
  ("hipp", "master"): "HiPP Master Brand",
  ("hipp", "hmp"): "HiPP Master Brand",
  ("illuma", "master"): "Illuma Master Brand",
  ("illuma", "luxa"): "Illuma Luxa",
  ("illuma", "organic"): "Illuma Organic",
  ("illuma", "a2"): "Illuma A2",
  ("illuma", "xtracare"): "Illuma XtraCare",
  ("mjn", "master"): "MJ Master Brand",
  ("mjn", "enfinitas"): "Enfinitas",
  ("mjn", "a_plus"): "MJ A+",
  ("mjn", "neuropro"): "MJ NeuroPro",
  ("mjn", "gentle_care"): "MJ Gentle Care",
  ("mjn", "nutripower"): "MJ NutriPower",
  ("nestle", "master"): "Nestle Master Brand",
  ("nestle", "nan_ha"): "NAN HA",
  ("nestle", "nan_infini_pro"): "NAN Infini Pro",
  ("nestle", "nan_a2"): "NAN A2",
  ("nestle", "nan_legacy"): "NAN HA",
  ("wyeth", "master"): "Wyeth Master Brand",
  ("wyeth", "s26_master"): "Wyeth Master Brand",
  ("wyeth", "s26_ultima"): "S26 Ultima",
  ("wyeth", "s26_gold"): "S26 Gold",
  ("wyeth", "ascenda"): "Wyeth Ascenda",
  ("wyeth", "s26_legacy"): "S26 Gold",
}

ENV_TARGET_DATE = os.environ.get("TARGET_DATE", "").strip()
ENV_REPAIR_MODE = os.environ.get("REPAIR_MODE", "QUOTED_MISATTRIBUTION").strip().upper()
ENV_TARGET_GIDS = os.environ.get("TARGET_GROUP_IDS", "").strip()
TARGET_GIDS_LIST = [g.strip() for g in ENV_TARGET_GIDS.split(",") if g.strip()]
def repair_should_write(dry_raw, confirm_raw):
  # 缺省演練。空字串、未設定、true、1、yes 都不寫入。
  # 只有明確關掉演練（false / 0 / no）且 confirm_run 正好是 yes 才寫入。
  if dry_raw is None:
    dry_off = False
  else:
    dry_off = str(dry_raw).strip().lower() in ("false", "0", "no")
  confirm_yes = str(confirm_raw or "").strip() == "yes"
  return bool(dry_off and confirm_yes)

IS_DRY_RUN = not repair_should_write(os.environ.get("DRY_RUN"), os.environ.get("CONFIRM_RUN"))

COMMIT_EVERY_N_ROWS = 1000
MAX_CONSECUTIVE_FAILURES = 5
POE_API_KEY = os.environ.get("POE_API_KEY", "")
POE_MODEL = "gemini-3.1-flash-lite"

poe_client = openai.OpenAI(api_key=POE_API_KEY, base_url="https://api.poe.com/v1")
http_session = requests.Session()
stats_lock = threading.Lock()
abort_event = threading.Event()
consecutive_ai_failures = 0
stats = {
  "scanned_db_records": 0,
  "suspect_filtered_records": 0,
  "ai_processed_records": 0,
  "updated_db_records": 0,
}

def parse_any_date(date_text):
  if not date_text:
    return None
  date_text = date_text.strip()
  for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%y%m%d", "%Y%m%d", "%d-%m-%Y", "%Y/%m/%d"]:
    try:
      return datetime.strptime(date_text, fmt)
    except ValueError:
      pass
  return None

def build_date_candidates(date_expr):
  if not date_expr:
    return []
  span_match = re.search(r"^(.*?)(?:\s*(?:to|至|-|~)\s*)([0-9/.\-]+)$", date_expr, re.IGNORECASE)
  if span_match and parse_any_date(span_match.group(1)) and parse_any_date(span_match.group(2)):
    d_start, d_end = parse_any_date(span_match.group(1)), parse_any_date(span_match.group(2))
    if d_start > d_end:
      d_start, d_end = d_end, d_start
    cands = []
    curr = d_start
    while curr <= d_end:
      cands.extend([
        curr.strftime("%Y-%m-%d"),
        curr.strftime("%d/%m/%Y"),
        "%02d/%02d/%d" % (curr.day, curr.month, curr.year),
        "%d/%d/%d" % (curr.day, curr.month, curr.year),
      ])
      curr += timedelta(days=1)
    return list(dict.fromkeys(cands))
  dt = parse_any_date(date_expr)
  if not dt:
    return [date_expr]
  return list(dict.fromkeys([
    dt.strftime("%Y-%m-%d"),
    dt.strftime("%d/%m/%Y"),
    "%02d/%02d/%d" % (dt.day, dt.month, dt.year),
    "%d/%d/%d" % (dt.day, dt.month, dt.year),
  ]))

def request_poe_api(prompt_text):
  if hasattr(poe_client, "responses") and hasattr(poe_client.responses, "create"):
    try:
      resp = poe_client.responses.create(model=POE_MODEL, input=prompt_text)
      if hasattr(resp, "output_text"):
        return resp.output_text
    except Exception:
      pass
  headers = {"Authorization": "Bearer " + str(POE_API_KEY), "Content-Type": "application/json"}
  http_resp = http_session.post(
    "https://api.poe.com/v1/responses",
    json={"model": POE_MODEL, "input": prompt_text},
    headers=headers,
    timeout=60,
  )
  if http_resp.status_code == 200:
    data = http_resp.json()
    return data.get("output_text") or data.get("text", "")
  raise Exception("HTTP %s" % http_resp.status_code)

def check_ai_service_health():
  print("🩺 正在檢查 LLM 連線...")
  try:
    res = request_poe_api('Hello, please reply with JSON: {"status": "ok"}')
    if "ok" in res.lower() or "{" in res:
      print("✅ LLM 連線正常。\n")
      return True
    raise ValueError("unexpected response")
  except Exception as e:
    print("🚨 LLM 無法使用，已停止:", str(e))
    return False

if not KEYWORDS_SPREADSHEET_ID:
  print("❌ 未設定關鍵詞表。")
  sys.exit(1)

print("🌐 正在讀取關鍵詞表...")
try:
  gc, _, _ = get_google_clients()
  sh_obj = gc.open_by_key(KEYWORDS_SPREADSHEET_ID)
  b_sheet = sh_obj.worksheet(BRAND_SHEET_NAME)
  b_data = b_sheet.get_all_values()
  brand_df = pd.DataFrame(b_data[1:], columns=b_data[0]) if b_data else pd.DataFrame()
  brand_df.columns = brand_df.columns.str.strip()
  ift_sheet = sh_obj.worksheet(IFT_SHEET_NAME)
  ift_data = ift_sheet.get_all_values()
  ift_df = pd.DataFrame(ift_data[1:], columns=ift_data[0]) if ift_data else pd.DataFrame()
  ift_df.columns = ift_df.columns.str.strip()
except Exception as e:
  print("❌ 讀取關鍵詞表失敗:", str(e))
  sys.exit(1)

exclude_mask_words = []
for _, row in ift_df.iterrows():
  k_type = str(row.get("type", "")).strip().lower()
  kw = str(row.get("keyword", "")).strip()
  if kw and kw.lower() not in ["nan", "none"] and k_type == "exclude":
    exclude_mask_words.append(kw)
exclude_mask_words = sorted(set(exclude_mask_words), key=len, reverse=True)

brand_text_rules = []
brand_regex_rules = []
for _, row in brand_df.iterrows():
  kw = str(row.get("Keyword", "") or row.get("keyword", "")).strip()
  m_type = str(row.get("Match_Type", "") or row.get("match_type", "")).strip().upper()
  c_with = str(row.get("Combo_With", "") or row.get("combo_with", "")).strip().lower()
  b_parent = str(row.get("Brand", "") or row.get("brand", "")).strip()
  sub_b = str(row.get("Sub_Brand", "") or row.get("sub_brand", "")).strip()
  if not kw or kw.lower() in ["nan", "none"]:
    continue
  target_col = CODE_TO_COLUMN_MAP.get((b_parent.lower(), sub_b.lower()))
  if m_type == "REGEX":
    try:
      brand_regex_rules.append((re.compile(kw, re.IGNORECASE), kw, b_parent, sub_b, target_col))
    except Exception:
      pass
  else:
    brand_text_rules.append({
      "kw": kw,
      "match_type": m_type if m_type in ["COMBO", "CONTAINS"] else "CONTAINS",
      "combo_with": c_with,
      "brand": b_parent,
      "sub_brand": sub_b,
      "target_col": target_col,
    })
brand_text_rules = sorted(brand_text_rules, key=lambda x: len(x["kw"]), reverse=True)

def parse_brands_from_text(text):
  if not text:
    return []
  occupied = [False] * len(text)
  text_lower = text.lower()
  for ex in exclude_mask_words:
    ex_l = ex.lower()
    start = 0
    while True:
      idx = text_lower.find(ex_l, start)
      if idx == -1:
        break
      for i in range(idx, min(len(occupied), idx + len(ex_l))):
        occupied[i] = True
      start = idx + 1
  matched = []
  for reg, raw_kw, b_p, s_b, t_col in brand_regex_rules:
    for m in reg.finditer(text):
      s, e = m.start(), m.end()
      if not any(occupied[s:e]):
        matched.append({"target_col": t_col, "matched_kw": m.group(0), "brand": b_p})
        for i in range(s, e):
          occupied[i] = True
  for rule in brand_text_rules:
    kw = rule["kw"]
    kw_l = kw.lower()
    is_pure_ascii = kw.isascii() and kw.isalpha()
    start = 0
    while True:
      idx = text_lower.find(kw_l, start)
      if idx == -1:
        break
      end = idx + len(kw_l)
      boundary_ok = True
      if is_pure_ascii:
        left = (idx > 0) and text[idx - 1].isalpha()
        right = (end < len(text)) and text[end].isalpha()
        if left or right:
          boundary_ok = False
      if boundary_ok and not any(occupied[idx:end]):
        matched.append({"target_col": rule["target_col"], "matched_kw": kw, "brand": rule["brand"]})
        for i in range(idx, end):
          occupied[i] = True
      start = idx + 1
  return matched

def call_llm_fix_analysis(body_text, quoted_text, eligible_desc, max_retries=3):
  global consecutive_ai_failures
  if abort_event.is_set():
    return False, False, []
  quoted_str = quoted_text if quoted_text else "無引用消息"
  prompt = (
    "判斷這則群組留言是否為無效訊息 (isSpam)，並抽出品牌立場 opinions。"
    "二手轉讓、促銷轉發一律 isSpam=true 且 opinions 為空。"
    "只能輸出候選清單內的品牌，或正文白紙黑字寫出的品牌。"
    "正文有品牌時只評正文；正文沒有品牌而引用有品牌時，情緒必須依據正文自己的態度，不可複製引用的情緒。"
    "母品牌與子系列分開輸出。sentiment 只能是 P、N 或 I。"
    "只輸出 JSON：{\"isSpam\": false, \"opinions\": [{\"brand_name\": \"\", \"sentiment\": \"I\"}]}\n"
    "候選：\n%s\n引用：%s\n正文：%s" % (eligible_desc, quoted_str, body_text)
  )
  attempt = 0
  while attempt < max_retries:
    try:
      res_text = request_poe_api(prompt)
      json_match = re.search(r"\{.*\}", res_text, re.DOTALL)
      if not json_match:
        raise ValueError("No JSON found")
      result = json.loads(json_match.group(0))
      with stats_lock:
        consecutive_ai_failures = 0
      return True, result.get("isSpam", False), result.get("opinions", [])
    except Exception:
      attempt += 1
      if attempt < max_retries:
        time.sleep(2)
      else:
        with stats_lock:
          consecutive_ai_failures += 1
          if consecutive_ai_failures >= MAX_CONSECUTIVE_FAILURES:
            abort_event.set()
            print("\n🚨 連續多次 AI 失敗，停止後續任務。")
        return False, False, []

host = (SUPABASE_DB_CONFIG.get("host") or "").strip()
password = (SUPABASE_DB_CONFIG.get("password") or "").strip()
if not host or not password:
  print("❌ 未設定寫入資料庫連線，已停止。")
  sys.exit(1)

print("🐘 正在連接寫入資料庫...")
try:
  conn = psycopg2.connect(**SUPABASE_DB_CONFIG)
except Exception as e:
  print("❌ 連接失敗:", str(e))
  sys.exit(1)

where_sql = [
  sql.SQL('"messageBody" IS NOT NULL AND TRIM("messageBody") <> %s'),
]
params = [""]
date_cands = build_date_candidates(ENV_TARGET_DATE)
if date_cands:
  where_sql.append(sql.SQL('"Date" = ANY(%s)'))
  params.append(date_cands)
if TARGET_GIDS_LIST:
  where_sql.append(sql.SQL('"GroupID" = ANY(%s)'))
  params.append(TARGET_GIDS_LIST)
if ENV_REPAIR_MODE == "QUOTED_MISATTRIBUTION":
  where_sql.append(sql.SQL(
    '"quotedMessage" IS NOT NULL AND TRIM("quotedMessage") <> %s AND TRIM("quotedMessage") <> %s AND "brand" = %s'
  ))
  params.extend(["", "[empty]", "1"])

select_cols = [
  sql.Identifier("message_id"),
  sql.Identifier("GroupID"),
  sql.Identifier("quotedMessage"),
  sql.Identifier("messageBody"),
  sql.Identifier("brand"),
  sql.Identifier("Other_Brands"),
] + [sql.Identifier(b) for b in STANDARD_BRANDS]

query = sql.SQL("SELECT {cols} FROM {table} WHERE {where}").format(
  cols=sql.SQL(", ").join(select_cols),
  table=sql.Identifier("public", SUPABASE_FULL_TABLE),
  where=sql.SQL(" AND ").join(where_sql),
)
with conn.cursor(cursor_factory=RealDictCursor) as cur:
  cur.execute(query, tuple(params))
  fetched_rows = cur.fetchall()

stats["scanned_db_records"] = len(fetched_rows)
print("📊 讀到 %d 筆。" % stats["scanned_db_records"])

suspect_records = []
for row in fetched_rows:
  body = str(row.get("messageBody") or "").strip()
  quoted = str(row.get("quotedMessage") or "").strip()
  b_hits = parse_brands_from_text(body)
  q_hits = parse_brands_from_text(quoted)
  b_cols = {h["target_col"] for h in b_hits if h.get("target_col")}
  q_cols = {h["target_col"] for h in q_hits if h.get("target_col")}
  is_suspect = False
  if ENV_REPAIR_MODE == "FULL_RESCORE":
    is_suspect = bool(b_hits or q_hits)
  else:
    if b_cols and q_cols and b_cols != q_cols:
      is_suspect = True
    elif not b_cols and q_cols:
      if any(row.get(col) == "N" for col in STANDARD_BRANDS):
        is_suspect = True
    elif b_cols:
      is_suspect = any(bc in STANDARD_BRANDS and not row.get(bc) for bc in b_cols)
  if is_suspect:
    row["_b_cols"] = list(b_cols)
    row["_q_cols"] = list(q_cols)
    row["_b_hits"] = b_hits
    suspect_records.append(row)

stats["suspect_filtered_records"] = len(suspect_records)
print("🎯 需重算 %d 筆。" % stats["suspect_filtered_records"])
if not suspect_records:
  conn.close()
  sys.exit(0)
if not check_ai_service_health():
  conn.close()
  sys.exit(1)

def process_fix_row(record):
  if abort_event.is_set():
    return None
  b_cols = record["_b_cols"]
  q_cols = record["_q_cols"]
  if b_cols:
    desc = "\n".join("- [%s] 正文命中" % c for c in b_cols)
  else:
    desc = "\n".join("- [%s] 只在引用出現" % c for c in q_cols)
  success, is_spam, opinions = call_llm_fix_analysis(record.get("messageBody") or "", record.get("quotedMessage") or "", desc)
  if not success:
    return None
  with stats_lock:
    stats["ai_processed_records"] += 1
  fixed = {b: None for b in STANDARD_BRANDS}
  mention = None
  others = []
  if not is_spam:
    standard_hit = False
    for op in opinions:
      if not isinstance(op, dict):
        continue
      raw_b = str(op.get("brand_name") or "").strip()
      s_val = str(op.get("sentiment") or "").strip().upper()
      if s_val not in ["P", "N", "I"] or not raw_b or raw_b.lower() in ["none", "null"]:
        continue
      resolved = next((b for b in STANDARD_BRANDS if raw_b.lower() == b.lower()), "")
      is_valid = (resolved in b_cols) if b_cols else bool(q_cols)
      if resolved and is_valid:
        fixed[resolved] = s_val
        standard_hit = True
      elif raw_b and is_valid:
        others.append("%s(%s)" % (raw_b, s_val))
    body = record.get("messageBody") or ""
    body_kws = [ev.get("matched_kw") or "" for ev in record.get("_b_hits", [])]
    pure_master_kws = ["美素", "愛他美", "雅培", "啟賦", "美贊臣", "雀巢", "惠氏"]
    for master_b, sub_list in MASTER_BRAND_ROLLUP.items():
      if any(fixed.get(sub) for sub in sub_list):
        independent = any(pm in body and len(body_kws) > 1 for pm in pure_master_kws)
        if not independent:
          fixed[master_b] = None
    if standard_hit and b_cols:
      mention = "1"
  other_str = "; ".join(others) if others else None
  return (mention, other_str, *[fixed[b] for b in STANDARD_BRANDS], record["message_id"])

def flush_batch_to_db(batch_data):
  if not batch_data:
    return
  if IS_DRY_RUN:
    with stats_lock:
      stats["updated_db_records"] += len(batch_data)
    return
  assignments = [
    sql.SQL("{c} = v.{c}").format(c=sql.Identifier("brand")),
    sql.SQL("{c} = v.{c}").format(c=sql.Identifier("Other_Brands")),
  ] + [sql.SQL("{c} = v.{c}").format(c=sql.Identifier(b)) for b in STANDARD_BRANDS]
  value_cols = [sql.Identifier("brand"), sql.Identifier("Other_Brands")] + [sql.Identifier(b) for b in STANDARD_BRANDS] + [sql.Identifier("message_id")]
  update_sql = sql.SQL(
    "UPDATE {table} AS t SET {sets} FROM (VALUES %s) AS v({vcols}) WHERE t.message_id = v.message_id"
  ).format(
    table=sql.Identifier("public", SUPABASE_FULL_TABLE),
    sets=sql.SQL(", ").join(assignments),
    vcols=sql.SQL(", ").join(value_cols),
  )
  with conn.cursor() as cur:
    execute_values(cur, update_sql.as_string(cur), batch_data, page_size=len(batch_data))
    conn.commit()
  with stats_lock:
    stats["updated_db_records"] += len(batch_data)
  print("\n💾 已提交 %d 筆（累計 %d）。" % (len(batch_data), stats["updated_db_records"]))

print("🤖 開始重算（每 %d 筆提交一次）..." % COMMIT_EVERY_N_ROWS)
buffer = []
done = 0
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
  futures = {executor.submit(process_fix_row, r): r for r in suspect_records}
  for future in concurrent.futures.as_completed(futures):
    if abort_event.is_set():
      print("\n🛑 已停止。")
      conn.close()
      sys.exit(1)
    res = future.result()
    done += 1
    print("\r 🔄 %d / %d" % (done, len(suspect_records)), end="", flush=True)
    if res:
      buffer.append(res)
      if len(buffer) >= COMMIT_EVERY_N_ROWS:
        flush_batch_to_db(buffer)
        buffer = []
if buffer:
  flush_batch_to_db(buffer)
conn.close()
if IS_DRY_RUN:
  print("\n🧪 演練完成，沒有寫入。")
else:
  print("\n✅ 修復寫入完成。")
print("掃描 %d，待修 %d，重算 %d，更新 %d。" % (
  stats["scanned_db_records"], stats["suspect_filtered_records"],
  stats["ai_processed_records"], stats["updated_db_records"]))
