# 🚀 AI-Driven Business Operations & Marketing Intelligence Suite

> **An enterprise AI operations portfolio** integrating real-time Generative AI assistants with automated data pipelines. Built to eliminate manual daily workflows, unlock conversational data insights, and turn community chat into structured brand-sentiment data.

**English** | [繁體中文](README_zh.md)

---

## 📌 Executive Summary

This repository demonstrates the end-to-end implementation of **Generative AI (Gemini 3.8 Flash)** and **Google Cloud Platform (GCP)** architecture in real-world business operations. The solution consists of two complementary systems:

1. **🤖 Multi-Modal WhatsApp AI Operations Assistant (Real-Time)**: An on-demand conversational agent deployed on GCP. Non-technical staff can query databases via natural language, extract data from documents/images, and dispatch CSV/email reports directly within WhatsApp.
2. **📊 WhatsApp Community Sentiment & NLP Pipeline (Automated Hourly Batch)**: A GitHub Actions data pipeline that pulls the last hour's WhatsApp group messages, de-duplicates and keyword-filters them, runs context-aware LLM brand-sentiment classification, and dual-writes a 48-column dataset to Google Sheets and Supabase for dashboards and negative-sentiment alerts.

---

## 🤖 System 1: Multi-Modal WhatsApp AI Operations Assistant
### 🏗️ Cloud & Agent Architecture
![Cloud & Agent Architecture](assets/architecture-system1.png)

### 🎬 Demo Video (2:13)


https://github.com/user-attachments/assets/b4423a86-953f-437b-a650-f37288afe17b


Recorded on the live system. Sensitive company data (chat list, contacts, emails, database schema, customer data, internal brand names) is redacted.

| Time | Scenario |
|---|---|
| 0:00 | Test 1: Multi-turn memory & `/reset` |
| 0:20 | Test 2: Natural-language database query (Text-to-SQL) |
| 0:51 | Test 4: Image OCR & document understanding |
| 1:07 | Test 3 & 5: Data analysis, CSV delivery & email report |
| 1:53 | Test 6: Real-time web search |

*Test 7 (group @mention filtering & idempotency) is a production guardrail and is not shown in the video.*

### 🧪 Verified Capabilities & Test Scenarios

The system has undergone end-to-end verification across operational workflows:

* **Test 1: Multi-Turn Memory & Session Reset (`/reset`)**
  * **Workflow:** Verified contextual memory across conversations stored in Cloud Firestore.
  * **Result:** Agent recalls contextual user attributes (e.g., user identity) and cleanly wipes history upon issuing `/reset`.
* **Test 2: Natural Language Database Querying (Text-to-SQL)**
  * **Workflow:** Inspects database schemas dynamically and executes safe read-only `SELECT` queries with connection pooling (SQLAlchemy).
  * **Result:** Automatically queries message counts and timestamps, displaying real-time UI feedback (`🔍 Executing SQL...`) before outputting structured insights.
* **Test 3: Big Data Analysis & Physical File Delivery**
  * **Workflow:** Extracts datasets into temporary storage, performs LLM sentiment/categorization analysis, and compiles downloadable reports.
  * **Result:** Delivers real-time progress indicators (`🧠 Analyzing data... ➔ 📊 Packaging report...`) and pushes the physical `.csv` file directly into the WhatsApp chat window.
* **Test 4: Multi-Modal & Document Understanding (Images & Files)**
  * **Workflow:** Evaluates image OCR and multi-format document parsing (`.docx`, `.xlsx`, `.pdf`, `.pptx`).
  * **Result:** Successfully extracts text and embedded flowcharts/screenshots inside documents, combining visual and textual reasoning into concise summaries.
* **Test 5: Automated Email Reporting with Attachments**
  * **Workflow:** Connects with Gmail SMTP to compile formatted HTML reports with data attachments.
  * **Result:** Pushes formatted executive summaries with generated `.csv` files directly to designated stakeholder inboxes.
* **Test 6: Real-Time Web Grounding Search**
  * **Workflow:** Dispatches queries requiring external real-time data (e.g., today's international tech news, live exchange rates) using a dedicated Google Grounding client.
  * **Result:** Accurately summarizes live web data without conflicting with internal tool schemas.
* **Test 7: Group Mention Filtering & Idempotency (Production Guardrail)**
  * **Workflow:** Filters non-relevant group messages and avoids duplicate executions from Pub/Sub retries.
  * **Result:** Agent remains silent unless explicitly `@mentioned` in groups; Firestore-backed deduplication ignores duplicate message IDs.

---

## 📊 System 2: WhatsApp Community Sentiment & NLP Pipeline

Hourly incremental job that converts raw parenting-community WhatsApp chats into brand-level sentiment data (35 standard brand / sub-brand columns across 9 brand families + "other brands"), ready for BI dashboards and CS/PR alerting.

### 🏗️ Pipeline Flow

![System 2 Pipeline](assets/pipeline-system2.jpg)

### 🌟 What the Pipeline Does

* **⚙️ Business-editable rules:** Brand keywords (`CONTAINS` / `COMBO` / `REGEX` match types, each mapped to a `Brand` + `Sub_Brand` code), topic keywords and exclusion words live in Google Sheets, so the marketing team can tune detection without touching code. A startup health check warns when a core brand term is missing from the keyword sheet. See [Keyword Sheet Structure](#-keyword-sheet-structure).
* **🧹 Smart de-duplication:** Merges the same message seen twice within 60 seconds in the same group (e.g. WhatsApp virtual ID vs. real number), keeps the highest-quality phone format, and tags internal/staff accounts from a private list.
* **💰 Cost-controlled AI:** A longest-match keyword layer (with exclusion masking) decides which messages need the LLM; only messages that mention a tracked brand are sent, processed with 10 parallel workers.
* **🧠 Context-aware sentiment:** Each candidate message is sent with its quoted message and up to 5 previous messages from the same group. The LLM returns structured JSON: spam flag, per-brand sentiment (`P` positive · `I` neutral/inquiry · `N` negative) and the original message it replies to.
* **🛡️ Anti-hallucination guardrails:** Prompt rules and few-shot examples stop the model from guessing brands from generic ingredients (e.g. DHA, hydrolysed), from misreading education terms like "A+", or from linking pronouns to brands that never appear in context. Spam (resale, points-sharing, promo forwards) is filtered out.
* **🧩 Contextual short-name resolution:** Short product names that are ambiguous on their own (e.g. a sub-brand name without its parent brand, `COMBO` rules) are only counted when the context confirms them: the quoted message mentions the parent brand, the parent brand appeared in the same group within the last 30 minutes, or the directly preceding message clearly talks about infant formula.
* **🔗 Reply attribution:** When a message answers an earlier one, Python re-aligns the model's `reply_origin` to the full original text (emoji included) in the context window, producing a clean `reply` column.
* **🏷️ Brand roll-up & alert flag:** Sub-brand sentiments automatically roll up to their parent brand column (`MASTER_BRAND_ROLLUP`, N > P > I priority), a literal-match safety net marks a brand as `I` if the LLM missed a brand that was explicitly named, and a `warning` flag marks negative mentions of the core brand for CS/PR follow-up.
* **🚦 Circuit breaker:** An AI health check runs before the batch, and the job aborts after 5 consecutive LLM failures (e.g. exhausted API credit) so no half-processed data is written.
* **💾 Dual-write output:** A fixed 48-column schema (12 message fields + 35 brand columns + `Other_Brands`) is appended to half-month Google Sheets (`yymm_DailyData_Part1` = days 1–15, `Part2` = 16–end) with formula-injection escaping and 429 back-off retries, and bulk-inserted into a Supabase `message_full` table. A test-sheet mode redirects all output away from production sheets.
* **🔁 Incremental runs & reruns:** Each run processes the last 65 minutes (HKT, a 5-minute overlap guards against gaps), and a concurrency lock queues overlapping runs so two jobs never write at once. Reruns via the workflow's `target_date` input: a full day (`260928`) or a precise time range (`260928 14:00-16:00`).

### 🧰 Supporting Scripts

* `dashboard.py` – aggregates a day's DailyData into the monthly Google Sheets dashboard (reach, group categories, brand sentiment counts). Run separately with `--dashboard_id`.
* `scripts/email_automation/` + `docs/MANUS_EMAIL_AUTOMATION.md` – daily summary email with negative-sentiment alerts, orchestrated by a Manus AI schedule.

### 🚀 Setup

1. `pip install -r requirements.txt` (Python 3.11)
2. Copy `.env.example` → `.env` and fill in values (for local runs), and place the Google service-account key at `service_account.json`. Share the keyword, group-info and DailyData sheets with the service account.
3. Optional: copy `internal_phones.example.json` → `internal_phones.json` (gitignored) to tag internal accounts.
4. `python main.py` (empty `MANUAL_DATE` = last 65 minutes; `MANUAL_DATE=260928` = full day; `MANUAL_DATE="260928 14:00-16:00"` = time range).

**GitHub Actions secrets** (workflow: `.github/workflows/daily_whatsapp_nlp.yml`):

| Secret | Required | Purpose |
| :--- | :--- | :--- |
| `GCP_SA_KEY` | ✅ | Google service-account JSON |
| `POE_API_KEY` | ✅ | Poe API key (LLM) |
| `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | ✅ | Source PostgreSQL (WhatsApp message store) |
| `DB_PORT` | optional | Defaults to `5432` |
| `KEYWORDS_SPREADSHEET_ID` or `KEYWORDS_SHEET_URL` | ✅ (one of) | Sheet with `brand_keywords` / `ift_keywords` tabs (tab names overridable via `BRAND_SHEET_NAME` / `IFT_SHEET_NAME`) |
| `GROUPINFO_SHEET_URL` | ✅ | Sheet with `groups` tab (group ID → name) |
| `SUPABASE_DB_HOST`, `SUPABASE_DB_USER`, `SUPABASE_DB_PASSWORD` | for Supabase | Supabase session pooler; write is skipped if host/password is empty |
| `SUPABASE_DB_PORT`, `SUPABASE_DB_NAME` | optional | Default `5432` / `postgres` |
| `TEST_TARGET_SHEET_URL` | optional | If set, all output goes to this test sheet |
| `INTERNAL_PHONES_JSON` | optional | JSON like `{"LabelA": ["9xxxxxxx"], "LabelB": [...]}` for the `Internal` column |

Trigger: **Actions → Hourly WhatsApp Data NLP Pipeline → Run workflow** (optional `target_date`), or an hourly `repository_dispatch` event of type `trigger-nlp-pipeline` from an external scheduler (`client_payload.target_date` is also accepted).

### 🔑 Keyword Sheet Structure

The real keyword lists are business data and stay in a private Google Sheet. Example files with the same columns are in [`examples/`](examples/).

**`brand_keywords` tab** – one row per detection rule:

| Column | Meaning |
| :--- | :--- |
| `Keyword` | Text or regex to match |
| `Match_Type` | `CONTAINS` (plain match), `COMBO` (ambiguous short name, only counted with context, see above), `REGEX` |
| `Combo_With` | For `COMBO`: the parent-brand word that must appear in context |
| `Brand` / `Sub_Brand` | Brand code pair (e.g. `friso` / `prestige`), mapped to one of the 35 output columns by `CODE_TO_COLUMN_MAP` in `main.py`; `master` = the parent brand itself |

**`ift_keywords` tab** – `type` + `keyword`: `formula_feature` (infant-formula context words that support short-name resolution), `general` (topic keywords), `exclude` (phrases masked before matching to avoid false hits).

---

## 💼 Business Impact & Efficiency Gains

| Metric / Dimension | Traditional Manual Workflow | AI-Automated Solution | Impact & Value Added |
| :--- | :--- | :--- | :--- |
| **Daily Data Processing** | 2 – 3 Hours / day | ~15 Minutes / day | **>80% Operational Time Saved** |
| **Data Querying Barrier** | Relies on data/IT team requests | Instant via WhatsApp conversation | **Zero learning curve** for non-technical teams |
| **Crisis Detection** | Discovered passively after complaints | Automated daily morning email alerts | Enables **proactive PR & CS intervention** |

---

## 🛠️ Technology Stack

* **AI & Multi-Modal Frameworks:** Google Vertex AI (Gemini Flash), Poe API (OpenAI-compatible, `gemini-3.1-flash-lite`), MarkItDown, Python-docx, Prompt Engineering
* **Cloud & Serverless:** Google Cloud Platform (Cloud Functions, Cloud Run, Cloud Pub/Sub, Cloud Secret Manager, Cloud Firestore)
* **Data & Storage:** PostgreSQL, Supabase, SQLAlchemy (Connection Pooling), psycopg2, Pandas
* **Automation & Gateways:** Evolution API (WhatsApp Gateway), Google Workspace APIs (Sheets & Drive), Gmail SMTP, GitHub Actions

---

## 🔒 Security & Privacy Notice

* **Encrypted Secrets:** All credentials, database hosts, sheet URLs and API tokens are supplied via environment variables (GCP Secret Manager / GitHub Secrets); the code ships with no real defaults (see `.env.example`).
* **Private Lists Stay Private:** Internal account phone lists are loaded at runtime from a secret or a gitignored local file, never committed.
* **De-Identified Data:** All demonstration logs, database schemas, and media samples are sanitized for public presentation.
