# ==========================================
# 🔐 Google API 授權模組 (google_auth.py)
# ==========================================
import json
import os
import time
from functools import wraps
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread

SCOPES = [
  'https://www.googleapis.com/auth/spreadsheets',
  'https://www.googleapis.com/auth/drive'
]
SERVICE_ACCOUNT_FILE = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', 'service_account.json')

def get_google_clients():
  """驗證並返回 Google API 客戶端 (支援 GitHub Secrets JSON 字串與本地實體檔案雙模式)"""
  creds = None

  # 1. 優先軌道：檢查環境變數是否傳入完整 JSON 內容 (支援 GOOGLE_SERVICE_ACCOUNT_KEY 或 GCP_SA_KEY)
  sa_json_str = (
    os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY", "").strip() 
    or os.environ.get("GCP_SA_KEY", "").strip()
  )

  if sa_json_str and sa_json_str.startswith("{"):
    try:
      sa_info = json.loads(sa_json_str)
      creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    except Exception as e:
      raise ValueError(f"❌ 解析環境變數中的 Google Service Account JSON 失敗: {str(e)}")

  # 2. 備用軌道：本地電腦調試時檢查實體檔案
  elif os.path.exists(SERVICE_ACCOUNT_FILE):
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)

  else:
    raise FileNotFoundError(
      f"❌ 找不到 Google 授權憑證！"
      f"請確保 GitHub Secrets 設置了 GOOGLE_SERVICE_ACCOUNT_KEY/GCP_SA_KEY，"
      f"或本地目錄存在實體檔案: {SERVICE_ACCOUNT_FILE}"
    )

  gc = gspread.authorize(creds)
  drive_service = build('drive', 'v3', credentials=creds, cache_discovery=False)
  sheets_service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
   
  return gc, drive_service, sheets_service

def with_retry(max_retries=5, base_delay=2):
  """Google API 防暴斃重試機制 (Exponential Backoff) - 專門抵禦 429 頻率限制"""
  def decorator(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
      retries = 0
      while retries < max_retries:
        try:
          return func(*args, **kwargs)
        except Exception as e:
          error_msg = str(e).lower()
          # 捕捉 429、Quota 或 Too Many Requests 錯誤
          if '429' in error_msg or 'quota' in error_msg or 'too many requests' in error_msg or 'exceeded' in error_msg:
            delay = base_delay * (2 ** retries)
            print(f" ⏳ 觸發 Google API 頻率限制，等待 {delay} 秒後自動重試... ({retries+1}/{max_retries})", flush=True)
            time.sleep(delay)
            retries += 1
          else:
            raise e # 其他非配額錯誤直接拋出
      raise Exception("❌ Google API 重試次數已達上限，請求失敗。")
    return wrapper
  return decorator
