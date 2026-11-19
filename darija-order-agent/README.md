# Algerian Darija Order-Confirmation Voice Agent (prototype)

Customer fills a small website form (name, phone, product) -> an AI agent **phones them in Algerian Darija** -> the answer is saved in a database.

```
Website form -> FastAPI -> Twilio call -> customer speaks
   -> Twilio recording -> Deepgram (STT, ar-DZ) -> OpenAI (intent + Darija reply)
   -> Azure TTS (ar-DZ-IsmaelNeural) -> played back on the call
   -> SQLite (+ optional Supabase copy): CONFIRMED / REJECTED / NEEDS_HUMAN_REVIEW / NO_ANSWER
```

## Structure

```
darija-order-agent/
├── app.py              FastAPI: website API + Twilio webhooks
├── services.py         Deepgram STT, OpenAI intent, Azure TTS
├── db.py               SQLite + optional Supabase mirror
├── telephony.py        Places the Twilio call
├── main.py             CLI (init-db / seed-demo / call)
├── static/index.html   Test website
├── supabase_schema.sql Optional Supabase table
├── audio/              Generated MP3s
├── requirements.txt  .env.example  .gitignore
```

## 1. Install (Windows PowerShell)

Your earlier errors: Python 3.11 wasn't installed, and the commands ran in `C:\Users\is` instead of the project folder.

```powershell
py install 3.12
py -3.12 --version
cd C:\path\to\darija-order-agent          # the folder that contains app.py
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

- If activation is blocked: `Set-ExecutionPolicy -Scope Process Bypass` then activate again.
- If `py install` doesn't work: `winget install Python.Python.3.12` (or python.org), reopen PowerShell.
- No compiled Azure SDK is used anymore (Azure is called over REST), so any recent Python works.

## 2. Accounts and keys (put them in `.env`)

| Service | Used for | Where |
|---|---|---|
| Twilio | phone calls (needs a Twilio number; trial = verified numbers only; enable Algeria in Voice > Geographic Permissions) | console.twilio.com |
| Deepgram | speech-to-text | console.deepgram.com |
| OpenAI | understanding + reply | platform.openai.com |
| Azure Speech | Darija voice | portal.azure.com -> Speech service (key + region) |

Set `ALLOWED_PHONES` in `.env` to **your own number** (e.g. `+2135XXXXXXXX`). Calls to Algerian mobiles cost roughly USD 1.8/min on Twilio, so keep the allow-list on while testing.

## 3. Run

Terminal 1:
```powershell
ngrok http 8000
```
Copy the `https://....ngrok-free.app` URL into `PUBLIC_BASE_URL` in `.env`.

Terminal 2 (venv active, inside the project folder):
```powershell
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```
Check `https://YOUR-NGROK-DOMAIN/health` -> `{"ok":true,...}`.

## 4. Test with the website

Open `https://YOUR-NGROK-DOMAIN/` (or http://localhost:8000). Enter name, your phone (`0550123456` or `+213550123456`), pick a product, tick the consent box, press **Place order & call me**. Your phone rings; say **"إيه، نأكدها"** -> the page shows CONFIRMED.

Try also: `لا، ما نيش حاب` (REJECTED), `نحب نبدل العنوان` (NEEDS_HUMAN_REVIEW), staying silent / unclear (never confirms; human review after `MAX_TURNS`).

CLI alternative: `python main.py seed-demo +2135XXXXXXXX` then `python main.py call DZ-DEMO-001`.

## 5. Where the data is stored

- **SQLite** `agent.db` (free, no setup). Tables: `orders`, `calls`, `call_turns`. Browse with DB Browser for SQLite or:
  `python -c "import sqlite3;print(sqlite3.connect('agent.db').execute('select id,customer_name,product_name,status from orders').fetchall())"`
- **Supabase (optional, free tier):** create a project, run `supabase_schema.sql` in the SQL Editor, then put `SUPABASE_URL` and the **service_role** key in `.env`. Orders are copied automatically on creation and on every status change. Never expose the service_role key in a browser.

## Safety rules built in

- Only an explicit CONFIRM with confidence >= 0.75 confirms an order; silence/hesitation/questions never do.
- The LLM only returns JSON; only Python code writes to the database.
- Prices are set server-side; one order per phone per `COOLDOWN_MINUTES`; optional number allow-list.
- The agent announces it is automated.

## Before real use

Set `SKIP_TWILIO_SIGNATURE_VALIDATION=false`; deploy on a real HTTPS host; add consent/opt-out, calling hours, retry limits, auth on the order API, transcript retention policy, and test the Darija quality with real Algerian speakers (Deepgram/Azure ar-DZ are Arabic locales, not guaranteed colloquial Darija). Replies come after a few seconds of silence because this is turn-based, not live streaming.
