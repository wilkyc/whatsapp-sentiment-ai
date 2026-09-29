import concurrent.futures
from datetime import datetime, timedelta
import json
import os
import re
import sys
import threading
import time

from config import (
    MILK_POWDER_BRANDS as STANDARD_BRANDS,
    FINAL_HEADERS_31,
    KEYWORDS_SHEET_URL as SPREADSHEET_URL,
    BRAND_SHEET_NAME,
    IFT_SHEET_NAME,
    GROUPINFO_SHEET_URL,
    MASTER_WORKSHEET_NAME,
    TEST_TARGET_SHEET_URL,
    TEST_WORKSHEET_TAB,
    DB_CONFIG,
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
from psycopg2.extras import RealDictCursor, execute_values
import pytz
import requests

# ==========================================
# ⚙️ 1. 核心參數
# ==========================================
ENV_MANUAL_DATE = os.environ.get("MANUAL_DATE", "").strip()
if ENV_MANUAL_DATE:
    MANUAL_TARGET_DATES = [ENV_MANUAL_DATE]
else:
    MANUAL_TARGET_DATES = []

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

# 📊 全景監控統計
stats = {
    "total_db_rows": 0,
    "total_deduped_rows": 0,
    "need_ai_processing": 0,
    "ai_actually_processed": 0,
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


_missing_cfg = [name for name, val in [
    ("KEYWORDS_SHEET_URL", SPREADSHEET_URL),
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
# 🔐 2. 授權 Google API
# ==========================================
print("🔐 正在使用服務帳戶授權 Google Sheets API...")
try:
    gc, _, _ = get_google_clients()
    print("✅ Google Sheets 服務帳戶授權成功！")
except Exception as e:
    print("❌ 授權失敗:", str(e))
    sys.exit(1)


# ==========================================
# 🌐 3. 讀取 Google Sheets 雙表配置
# ==========================================
print("\n🌐 正在從 Google Sheets 讀取雙配置檔 (brand_keywords & ift_keywords)...")
try:
    sh_obj = gc.open_by_url(SPREADSHEET_URL)
    
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

brand_text_rules = []
brand_regex_rules = []

for _, row in brand_df.iterrows():
    kw = str(row.get("Keyword", "") or row.get("keyword", "")).strip()
    m_type = str(row.get("Match_Type", "") or row.get("match_type", "")).strip().upper()
    c_with = str(row.get("Combo_With", "") or row.get("combo_with", "")).strip().lower()
    
    if not kw or kw.lower() in ["nan", "none"]:
        continue
    
    if m_type == "REGEX":
        try:
            brand_regex_rules.append((re.compile(kw, re.IGNORECASE), kw))
        except Exception as e:
            print("⚠️ 正則語法錯誤跳過 [" + kw + "]:", str(e))
    else:
        brand_text_rules.append({
            "kw": kw,
            "match_type": m_type if m_type in ["COMBO", "CONTAINS"] else "CONTAINS",
            "combo_with": c_with
        })

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
if MANUAL_TARGET_DATES:
    target_dates = MANUAL_TARGET_DATES
else:
    target_dates = [(datetime.now(hk_tz) - timedelta(days=1)).strftime("%y%m%d")]

print("\n🐘 準備查詢 PostgreSQL 數據庫，原始目標代號:", target_dates)

date_query_candidates = []
for d_str in target_dates:
    d_clean = str(d_str).strip()
    date_query_candidates.append(d_clean)
    
    parsed_dt = None
    for fmt in ["%y%m%d", "%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"]:
        try:
            parsed_dt = datetime.strptime(d_clean, fmt)
            break
        except ValueError:
            pass
            
    if parsed_dt:
        date_query_candidates.append(parsed_dt.strftime("%d/%m/%Y"))
        date_query_candidates.append("%d/%d/%04d" % (parsed_dt.day, parsed_dt.month, parsed_dt.year))
        date_query_candidates.append(parsed_dt.strftime("%Y-%m-%d"))

date_query_candidates = list(dict.fromkeys(date_query_candidates))
print("🎯 SQL 索引精準匹配候選字串:", date_query_candidates)

raw_db_rows = []
try:
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        query = """
            SELECT 
                groupname,
                gusid,
                sentdate,
                senttime,
                userphone,
                messagebody,
                mediacaption,
                quotedmessage
            FROM public.messageview
            WHERE sentdate = ANY(%s)
              AND messagebody IS NOT NULL 
              AND TRIM(messagebody) != ''
              AND messagebody != '[empty]'
            ORDER BY gusid, sentdate, senttime ASC;
        """
        cur.execute(query, (date_query_candidates,))
        raw_db_rows = cur.fetchall()
    conn.close()
    stats["total_db_rows"] = len(raw_db_rows)
    print("✅ 成功從 PostgreSQL 抽取 %d 筆有效對話！" % stats["total_db_rows"])
except Exception as e:
    print("❌ PostgreSQL 連接或查詢失敗:", str(e))
    sys.exit(1)


# ==========================================
# 🧹 5. 前置智能去重 + 號碼品質升級
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
        elif len(phone_str) >= 12:  # 虛擬代號 LID
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
        start = 0
        while True:
            idx = text_lower.find(kw_l, start)
            if idx == -1:
                break
            end = idx + len(kw_l)
            if not any(occupied_mask[idx:end]):
                matched.append(kw)
                for i in range(idx, end):
                    occupied_mask[i] = True
            start = idx + 1
    return matched

def parse_message_layers(text):
    if not text:
        return [], [], []
    
    occupied = [False] * len(text)
    text_lower = text.lower()
    
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

    matched_brands = []
    for reg, raw_kw in brand_regex_rules:
        for m in reg.finditer(text):
            s, e = m.start(), m.end()
            if not any(occupied[s:e]):
                matched_brands.append(m.group(0))
                for i in range(s, e):
                    occupied[i] = True

    for rule in brand_text_rules:
        kw = rule["kw"]
        kw_l = kw.lower()
        m_type = rule["match_type"]
        c_with = rule["combo_with"]
        
        if m_type == "COMBO" and c_with:
            if c_with not in text_lower:
                continue

        start = 0
        while True:
            idx = text_lower.find(kw_l, start)
            if idx == -1:
                break
            end = idx + len(kw_l)
            if not any(occupied[idx:end]):
                matched_brands.append(kw)
                for i in range(idx, end):
                    occupied[i] = True
            start = idx + 1

    matched_formula = extract_unique_kws_longest_match(text, formula_feature_keywords, occupied)
    matched_general = extract_unique_kws_longest_match(text, general_keywords, occupied)

    return matched_brands, matched_formula, matched_general


print("\n🧹 正在進行前置智能去重 (同群、同內容、時間相近或虛擬號/真號合併)...")
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
        b_brand, b_form, b_gen = parse_message_layers(body)
        q_brand, q_form, q_gen = parse_message_layers(quoted)

        all_brand_hits = list(dict.fromkeys(b_brand + q_brand))
        all_formula_hits = list(dict.fromkeys(b_form + q_form))
        all_general_hits = list(dict.fromkeys(b_gen + q_gen))
        all_combined_kws = all_brand_hits + all_formula_hits + all_general_hits

        should_send_to_ai = bool(all_brand_hits or all_formula_hits)

        # 31 欄位結構字典 (reply 位於 messageBody 之後，brand 之前)
        record = {
            "Group": group_name,
            "GroupID": gusid,
            "Date": date_val,
            "Time": time_val,
            "userPhone": phone_raw,
            "phone_score": curr_score,
            "phone_clean": curr_clean_digits,
            "Internal": internal_flag,
            "quotedMessage": quoted,
            "messageBody": body,
            "reply": "",
            "brand": "",
            "keywords": ", ".join(all_combined_kws),
            "warning": "",
            "Other_Brands": "",
            "should_ai": should_send_to_ai,
            "context_history": []
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

# 構建上下文回溯隊列 (匿名化發言內容，同時保存真實原話供後續精確對齊)
records_by_group = {}
for r in cleaned_records:
    gid = r["GroupID"]
    if gid not in records_by_group:
        records_by_group[gid] = []
    
    if r["should_ai"] and CONTEXT_HISTORY_LIMIT > 0:
        history_msgs = [
            str(prev["messageBody"])
            for prev in records_by_group[gid][-CONTEXT_HISTORY_LIMIT:]
            if prev["messageBody"] and prev["messageBody"] not in ["[empty]", "image", "video"]
        ]
        r["context_history"] = history_msgs
        
    records_by_group[gid].append(r)

stats["need_ai_processing"] = sum(1 for r in cleaned_records if r["should_ai"])

print_stage_dashboard(
    "前置智能去重與候選過濾完成",
    {
        "💬 資料庫原始筆數": "%d 行" % stats["total_db_rows"],
        "📝 去重後保留唯一數": "%d 行 (成功壓縮)" % stats["total_deduped_rows"],
        "🎯 觸發需 AI 分析數": "%d 行" % stats["need_ai_processing"],
        "⏱️ 上下文回溯窗口上限": "%d 條" % CONTEXT_HISTORY_LIMIT,
    }
)


# ==========================================
# 🤖 6. 第二階段：AI 語義識別與【原話精確對齊 (reply)】
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
        print("💡 常見原因：公司 Poe 帳號忘記充值欠費、餘額不足，或 API Key 失效。")
        print("🛑 為保護數據庫整潔，系統已自動【中止所有後續嗅探與寫入流程】！")
        print("   未向 Google Sheets 或 Supabase 寫入任何半成品數據。")
        print("!" * 65 + "\n")
        return False

def call_llm_analysis(body_text, quoted_text, context_list, max_retries=3):
    global consecutive_ai_failures
    
    if abort_event.is_set():
        return False, False, [], ""

    # 前文每條清楚帶上序號，並呈現真實原話
    context_str = "\n".join(["[前文發言 " + str(idx+1) + "]: " + str(c) for idx, c in enumerate(context_list)]) if context_list else "無前文記錄"
    quoted_str = quoted_text if quoted_text else "無引用消息"

    prompt = """# Role
你是一位具備 15 年育兒經驗的香港母親，同時擔任頂級母嬰品牌公關與客服風控專家。你精通香港/廣東話社群俚語、常見錯別字及指代習慣。

# Task
解構香港媽媽群聊留言，判斷「是否為無效訊息 (isSpam)」並提取「討論的奶粉品牌與評價立場 (opinions)」。

# 垃圾/商業過濾 (isSpam 判定 - 極度嚴格)：
凡符合以下任一特徵，一律標記為 isSpam: true，且 opinions 強制為空 []，reply_origin 強制為空字串 ""：
1. 純交易/轉讓：徵收、二手買賣、平放、全新未開、有意pm、出讓奶粉券。
2. 促銷轉發/代購報價/積分轉讓：
   - 出現「幫忙儲分」、「代儲分」、「yuu」、「萬寧88折」、「折後$XXX」、「指定門市地址」等藥房超市格價或折扣券轉發。

# 【防腦補與溯源規範】(極度重要！違反將導致重大數據失真)：
1. 嚴禁將通用成分對號入座：
   - 「水解」、「益生元」、「鐵質」、「乳鐵蛋白」、「DHA」是各大品牌奶粉共通成分！
   - ⚠️ 嚴禁因為看到「水解」就猜測是「雀巢能恩」，嚴禁因為看到「乳鐵蛋白」就猜測是「美素皇家」！若未明確出現品牌名稱，opinions 必須為空 []！
2. 嚴禁將教育術語 A+ 誤判為奶粉：
   - 香港群聊中「ITT + A+」、「面試A+」、「成績A+」指教育培訓或成績評級，完全無關奶粉，opinions 必須為空 []！
3. 嚴禁憑空猜測主體 (防指代飄移)：
   - 若發言僅提及賣點或代名詞（例：「我見佢成份比較全面」、「高呀，比起其他奶粉又唔貴」、「poo poo每日都有咁樣」）：
   - ⚠️ 只有當【同一群組前文對話】或【引用消息】中【白紙黑字明確出現過具體品牌名】（如前文明確寫了「Aptamil」或「美素」）時，才允許將代詞關聯過去！
   - ⚠️ 若前文中通篇都只是代名詞（如「新出嗰隻」、「呢間」、「轉咗包裝」、「性價比高嗎」），【嚴禁自行猜測是哪款奶粉】，此時 opinions 必須為空列表 []，reply_origin 為 ""！
4. 💡 reply_origin 欄位輸出規則 (非常嚴格，禁止截斷)：
   - 當本句發言沒有直接提品牌、而是回覆/接續了前文某句發言時，請【完整、原封不動複製粘貼該前文發言全文】到 reply_origin！
   - ⚠️ 嚴禁任何擅自刪減或提煉！
   - ⚠️ 嚴禁去除 Emoji（例如前文是「🤢🤢係呀～飲皇家有機」，必須原汁原味輸出「🤢🤢係呀～飲皇家有機」，絕不可截斷成「飲皇家有機」）！
   - 若本句已經直接提了品牌，或前文無品牌而放棄關聯，reply_origin 必須強制為空字串 ""！
   - 嚴禁輸出「放棄關聯」或「純交易轉讓」等分析廢話，不符合條件時直接留空字串 ""！

# 嚴格對齊 18 個標準品牌：
""" + json.dumps(STANDARD_BRANDS, ensure_ascii=False) + """
若提及名單外的小眾品牌，直接回傳其真實品牌名。

# 立場情感 (sentiment) 評判標準：
- "P" (正面): 讚賞、推介、成分好、長肉長磅、便便順暢、整體優點大於缺點（欲揚先抑算 P，例：「除咗貴冇咩好投訴」-> P）。
- "N" (負面): 針對產品本身的嚴重指控、不良反應（便秘、羊咩屎、嚴重肚脹、腹瀉、起濕疹紅點、極難溶結塊）或重大危機。轉奶因負面原因轉走算 N。
- "I" (中立/客觀): 純詢問、客觀陳述、單純轉奶意向但無評價。BB 挑食不肯喝只判 I。

# 📚 Few-Shot 典型案例：
案例 1 (代儲分/促銷轉發): 
"Free 有多張 幫忙儲分🌸 萬寧 今日88折 愛他美至熠Aptamil Neo 折後$294"
-> {"isSpam": true, "opinions": [], "reply_origin": ""}

案例 2 (前文無具體品牌，嚴禁腦補，純淨留空): 
前文: "[前文發言 1]: 有冇人試過新出嗰隻？" -> 本句: "poo poo每日都有咁樣囉"
-> {"isSpam": false, "opinions": [], "reply_origin": ""}

案例 3 (完整一字不差原樣還原前文，保留Emoji與語氣詞):
前文: "[前文發言 1]: 🤢🤢係呀～飲皇家有機" -> 本句: "冇咁刺激腸胃"
-> {"isSpam": false, "opinions": [{"brand_name": "美素有機", "sentiment": "P", "raw_mention": "冇咁刺激腸胃"}], "reply_origin": "🤢🤢係呀～飲皇家有機"}

案例 4 (本句直接提品牌，reply_origin 強制留空):
"美素皇家除咗貴，真係冇咩好投訴，阿女便便好順"
-> {"isSpam": false, "opinions": [{"brand_name": "美素皇家", "sentiment": "P", "raw_mention": "除咗貴冇咩好投訴，便便好順"}], "reply_origin": ""}

# 輸出 JSON 格式 (嚴禁輸出 Markdown 或其他文字)：
{
  "isSpam": false,
  "reply_origin": "",
  "opinions": [
    {
      "brand_name": "標準品牌名稱或其它品牌名",
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
                        print("\n🚨 【運行中熔斷】已連續 %d 次 AI 調用失敗 (可能餘額耗盡)，終止後續任務！" % consecutive_ai_failures)
                return False, False, [], ""

def process_record_ai(record):
    if abort_event.is_set():
        return record, False, False, [], ""

    success, is_spam, opinions, reply_origin = call_llm_analysis(
        record["messageBody"],
        record["quotedMessage"],
        record["context_history"]
    )
    return record, success, is_spam, opinions, reply_origin

print("\n🤖 開始進行第二階段：AI 語義識別與【原話溯源 (reply)】...")
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

                # 💡【核心演算法：原句自動對齊還原】
                # 若 AI 自作聰明把「🤢🤢係呀～飲皇家有機」截成了「飲皇家有機」，
                # Python 會在 context_history 裡自動找到該句完整原話，100% 像素級還原！
                final_reply = ""
                if reply_origin and not record["quotedMessage"]:
                    cleaned_needle = reply_origin.strip()
                    # 由近到遠比對前文
                    for ctx_msg in reversed(record["context_history"]):
                        full_msg_text = str(ctx_msg).strip()
                        if cleaned_needle in full_msg_text or full_msg_text in cleaned_needle:
                            final_reply = full_msg_text
                            break
                    if not final_reply:
                        final_reply = cleaned_needle
                
                # 有 quotedMessage 則強制清空 reply，避免重複
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
                else:
                    friso_sentiments = []
                    other_brands_collected = []
                    standard_brand_hit = False

                    for op in opinions:
                        b_name = op.get("brand_name", "").strip()
                        s_val = op.get("sentiment", "I").strip()

                        if b_name in STANDARD_BRANDS:
                            record[b_name] = s_val
                            standard_brand_hit = True
                            if "美素" in b_name:
                                friso_sentiments.append(s_val)
                        elif b_name:
                            other_brands_collected.append(b_name + "(" + s_val + ")")

                    if standard_brand_hit:
                        record["brand"] = "1"
                        with stats_lock:
                            stats["brand_identified_count"] += 1
                            if record["reply"]:
                                stats["context_attributed_count"] += 1
                    else:
                        record["brand"] = ""
                        with stats_lock:
                            stats["generic_no_brand"] += 1

                    if friso_sentiments:
                        if "N" in friso_sentiments:
                            record["美素"] = "N"
                        elif "P" in friso_sentiments:
                            record["美素"] = "P"
                        elif "I" in friso_sentiments:
                            record["美素"] = "I"

                    if record.get("美素") == "N":
                        record["warning"] = "✓"

                    if other_brands_collected:
                        record["Other_Brands"] = "; ".join(other_brands_collected)
    print()


# ==========================================
# 💾 7. 雙軌寫入：Google Sheets (31欄) + Supabase (31欄)
# ==========================================
print("\n💾 正在整理資料並準備執行雙軌寫入 (31 欄位最新順序)...")
final_df = pd.DataFrame(cleaned_records)

def escape_sheet_formula(val):
    if not isinstance(val, str):
        return val
    stripped = val.lstrip()
    if stripped and stripped[0] in ("=", "+", "-", "@"):
        return "\u200b" + val
    return val

def get_target_sheet_name(date_str):
    try:
        dt = pd.to_datetime(date_str)
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
    for col in FINAL_HEADERS_31:
        if col not in final_df.columns:
            final_df[col] = ""
    full_31_df = final_df[FINAL_HEADERS_31].copy()

    # ── 7.1 寫入 Google Sheets ──
    sheets_df = full_31_df.copy()
    for text_col in ["messageBody", "quotedMessage", "reply"]:
        sheets_df[text_col] = sheets_df[text_col].apply(escape_sheet_formula)

    is_test_mode = bool(
        TEST_TARGET_SHEET_URL 
        and "your_test_sheet_id" not in TEST_TARGET_SHEET_URL 
        and TEST_TARGET_SHEET_URL.strip().startswith("https://docs.google.com")
    )

    if is_test_mode:
        print("\n🧪 【測試模式啟用】檢測到有效測試表 URL，強制寫入指定測試 Sheet，絕不污染正式表！")
        print("👉 測試目標網址:", TEST_TARGET_SHEET_URL)
        try:
            sh = gc.open_by_url(TEST_TARGET_SHEET_URL)
            worksheet = sh.worksheet(TEST_WORKSHEET_TAB)
            append_to_google_sheet_safe(worksheet, sheets_df.values.tolist())
            print("✅ [測試表] 成功寫入 %d 筆 31 欄測試數據到 【%s】！" % (len(sheets_df), TEST_WORKSHEET_TAB))
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

    # ── 7.2 寫入 Supabase 全量表 (31 欄) ──
    supabase_host = SUPABASE_DB_CONFIG.get("host", "").strip()
    supabase_pw = SUPABASE_DB_CONFIG.get("password", "").strip()

    if supabase_host and supabase_pw:
        print("\n⚡ [Supabase] 正在連接 Supabase 並批次寫入全量表 【" + SUPABASE_FULL_TABLE + "】...")
        try:
            db_cols = [
                '"Group"', '"GroupID"', '"Date"', '"Time"', '"userPhone"', '"Internal"',
                '"quotedMessage"', '"messageBody"', '"reply"', '"brand"', '"keywords"', '"warning"',
                '雅培心美力', '"Apta Platinum"', '"Apta Essensis"', '"Apta Neo"',
                '牛欄牌', '美素', '美素金裝', '美素皇家', '美素有機',
                '"美素Kids"', '"美素Signature"', '"Hipp"', '"Illuma"',
                '"Illuma 有機"', '"美贊臣 A+"', '"美贊臣 Enfinitas"',
                '雀巢能恩', '雀巢全護', '"Other_Brands"'
            ]

            insert_rows = []
            for _, r in full_31_df.iterrows():
                d_val = str(r["Date"]).strip()
                d_parsed = pd.to_datetime(d_val, errors="coerce")
                date_str = d_parsed.strftime("%Y-%m-%d") if pd.notna(d_parsed) else None

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
                    r.get("雅培心美力") or None,
                    r.get("Apta Platinum") or None,
                    r.get("Apta Essensis") or None,
                    r.get("Apta Neo") or None,
                    r.get("牛欄牌") or None,
                    r.get("美素") or None,
                    r.get("美素金裝") or None,
                    r.get("美素皇家") or None,
                    r.get("美素有機") or None,
                    r.get("美素Kids") or None,
                    r.get("美素Signature") or None,
                    r.get("Hipp") or None,
                    r.get("Illuma") or None,
                    r.get("Illuma 有機") or None,
                    r.get("美贊臣 A+") or None,
                    r.get("美贊臣 Enfinitas") or None,
                    r.get("雀巢能恩") or None,
                    r.get("雀巢全護") or None,
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
                print("✅ [Supabase] 成功批次寫入 " + str(len(insert_rows)) + " 筆 31 欄資料到 【" + SUPABASE_FULL_TABLE + "】！")
        except Exception as e:
            print("❌ [Supabase] 寫入全量表失敗:", str(e))
    else:
        if not supabase_pw:
            print("\n⚠️ [Supabase] 檢測到 SUPABASE_DB_PASSWORD 為空，已略過 Supabase 寫入。")
        else:
            print("\nℹ️ [Supabase] 略過 Supabase 寫入。")
else:
    print("⚠️ 目標日期無有效資料可寫入。")

# ==========================================
# 📊 8. 升級版全景監控統計看板
# ==========================================
print_stage_dashboard(
    "自動化任務完成 (全景業務指標)",
    {
        "💬 資料庫原始總數": "%d 行" % stats["total_db_rows"],
        "📝 前置去重後唯一數": "%d 行" % stats["total_deduped_rows"],
        "🎯 AI 實際處理筆數": "%d 行" % stats["ai_actually_processed"],
        "🏷️ 明確命中品牌數": "%d 行 (18品牌有填入，brand=1)" % stats["brand_identified_count"],
        "🔗 成功關聯前文數": "%d 行 (reply 溯源)" % stats["context_attributed_count"],
        "🍼 泛育兒(無品牌)數": "%d 行" % stats["generic_no_brand"],
        "🗑️ 標記為 Spam 垃圾數": "%d 行" % stats["spam_detected"],
        "📝 最終寫入總筆數": "%d 行" % len(final_df)
    }
)
