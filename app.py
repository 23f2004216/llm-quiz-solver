# app.py
import os
import re
import json
import time
import base64
import tempfile
import traceback
from urllib.parse import urljoin, urlparse

from flask import Flask, request, jsonify
import requests
import pandas as pd
import pdfplumber
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Config
SECRET = os.environ.get("QUIZ_SECRET", "42e57fd2-361c-492f-9566-4c08483b9d04")
MAX_SECONDS = int(os.environ.get("QUIZ_MAX_SECONDS", "170"))  # overall max seconds for solve
PLAYWRIGHT_TIMEOUT = int(os.environ.get("PLAYWRIGHT_TIMEOUT_MS", "60000"))  # per action ms

app = Flask(__name__)

def safe_json(req):
    try:
        return req.get_json(force=True)
    except Exception:
        return None

def find_submit_url_from_text(text):
    m = re.search(r"https?://[^\s'\"<>]+/submit[^\s'\"<>]*", text, re.IGNORECASE)
    if m:
        return m.group(0)
    m2 = re.search(r'action=["\']([^"\']+)["\']', text, re.IGNORECASE)
    if m2:
        return m2.group(1)
    return None

def extract_b64_candidates(text):
    return re.findall(r"[A-Za-z0-9+/=]{80,}", text)

def try_decode_b64(s):
    try:
        b = base64.b64decode(s, validate=False)
        return b
    except Exception:
        return None

def download_file(url, dest_folder=None):
    dest_folder = dest_folder or tempfile.gettempdir()
    try:
        r = requests.get(url, stream=True, timeout=30)
        r.raise_for_status()
        fname = os.path.basename(urlparse(url).path) or "file"
        path = os.path.join(dest_folder, fname)
        with open(path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return path
    except Exception:
        return None

def parse_csv_or_excel(path):
    try:
        if path.lower().endswith((".csv", ".txt")):
            return pd.read_csv(path)
        if path.lower().endswith((".xls", ".xlsx")):
            return pd.read_excel(path)
    except Exception:
        return None
    return None

def parse_pdf_for_tables_and_text(path):
    tables = []
    text = ""
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                try:
                    page_tables = page.extract_tables()
                    for t in page_tables:
                        if t:
                            df = pd.DataFrame(t[1:], columns=t[0]) if len(t) > 1 else pd.DataFrame(t)
                            tables.append(df)
                except Exception:
                    pass
                try:
                    text += (page.extract_text() or "") + "\n"
                except Exception:
                    pass
    except Exception:
        return [], None
    return tables, text

def compute_answer_from_dataframe(df):
    if df is None or df.empty:
        return None
    cols = [str(c).lower() for c in df.columns]
    if "value" in cols:
        try:
            s = pd.to_numeric(df.iloc[:, cols.index("value")], errors="coerce").sum()
            if pd.notna(s):
                return float(s)
        except Exception:
            pass
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            try:
                s = df[col].sum()
                return float(s)
            except Exception:
                pass
    try:
        numeric = []
        for col in df.columns:
            try:
                numeric.extend(pd.to_numeric(df[col], errors="coerce").dropna().tolist())
            except Exception:
                pass
        if numeric:
            return float(sum(numeric))
    except Exception:
        pass
    return None

def find_numeric_in_text(text):
    if not text:
        return None
    m = re.search(r"([-+]?\d[\d,]*\.?\d*)", text.replace(",", ""))
    if m:
        num = m.group(1)
        try:
            if "." in num:
                return float(num)
            return int(num)
        except Exception:
            return None
    return None

def post_answer(submit_url, payload):
    try:
        r = requests.post(submit_url, json=payload, timeout=25)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text
    except Exception as e:
        return None, str(e)

@app.route("/api/quiz", methods=["POST"])
def quiz():
    start_time = time.time()
    req = safe_json(request)
    if req is None:
        return jsonify({"error": "Invalid JSON"}), 400

    email = req.get("email")
    secret = req.get("secret")
    url = req.get("url")

    if not email or not secret or not url:
        return jsonify({"error": "Missing fields"}), 400

    if secret != SECRET:
        return jsonify({"error": "Invalid secret"}), 403

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.goto(url, timeout=PLAYWRIGHT_TIMEOUT)
            page.wait_for_load_state("networkidle", timeout=PLAYWRIGHT_TIMEOUT)
            content_html = page.content()
            try:
                body_text = page.inner_text("body")
            except Exception:
                body_text = content_html
            scripts_text = []
            try:
                scripts = page.query_selector_all("script")
                for s in scripts:
                    try:
                        t = s.inner_text()
                        if t:
                            scripts_text.append(t)
                    except Exception:
                        pass
            except Exception:
                pass
            script_combined = "\n".join(scripts_text)
            browser.close()
    except PWTimeout as e:
        return jsonify({"error": "timeout rendering page", "detail": str(e)}), 500
    except Exception as e:
        return jsonify({"error": "render error", "detail": str(e), "trace": traceback.format_exc()}), 500

    soup_text = "\n".join([body_text or "", script_combined or "", content_html or ""])
    submit_url = find_submit_url_from_text(soup_text)
    if submit_url and not submit_url.lower().startswith("http"):
        submit_url = urljoin(url, submit_url)

    decoded_json = None
    for cand in extract_b64_candidates(script_combined + "\n" + content_html):
        d = try_decode_b64(cand)
        if d:
            try:
                txt = d.decode("utf-8", errors="ignore")
                j = None
                try:
                    j = json.loads(txt)
                except Exception:
                    m = re.search(r"\{[\s\S]*\}", txt)
                    if m:
                        try:
                            j = json.loads(m.group(0))
                        except Exception:
                            j = None
                if isinstance(j, dict):
                    decoded_json = j
                    break
            except Exception:
                pass

    final_answer = None
    if decoded_json and "answer" in decoded_json:
        final_answer = decoded_json["answer"]
    else:
        file_urls = re.findall(r"https?://[^\s'\"<>]+\.(?:csv|pdf|xlsx|xls|txt)", soup_text, re.IGNORECASE)
        for furl in file_urls:
            local = download_file(furl)
            if not local:
                continue
            df = parse_csv_or_excel(local)
            if df is not None:
                val = compute_answer_from_dataframe(df)
                if val is not None:
                    final_answer = val
                    break
            if local.lower().endswith(".pdf"):
                tables, pdf_text = parse_pdf_for_tables_and_text(local)
                for t in tables:
                    v = compute_answer_from_dataframe(t)
                    if v is not None:
                        final_answer = v
                        break
                if final_answer is not None:
                    break
                if pdf_text:
                    n = find_numeric_in_text(pdf_text)
                    if n is not None:
                        final_answer = n
                        break
        if final_answer is None:
            n = find_numeric_in_text(soup_text)
            if n is not None:
                final_answer = n

    if final_answer is None:
        snippet = (soup_text or "")[:1600]
        return jsonify({"status": "no_answer_found", "snippet": snippet}), 200

    if not submit_url:
        m = re.search(r"https?://[^\s'\"<>]+/submit[^\s'\"<>]*", soup_text, re.IGNORECASE)
        if m:
            submit_url = m.group(0)

    if not submit_url:
        return jsonify({"status": "no_submit_url", "answer": final_answer}), 200

    payload = {
        "email": email,
        "secret": secret,
        "url": url,
        "answer": final_answer
    }

    elapsed = time.time() - start_time
    if elapsed > MAX_SECONDS - 10:
        code, resp = post_answer(submit_url, payload)
        return jsonify({"status": "submitted_partial_due_to_time", "post_status": code, "response": resp}), 200

    code, resp = post_answer(submit_url, payload)
    next_url = None
    if isinstance(resp, dict):
        next_url = resp.get("url")

    result = {
        "submitted_status": code,
        "submit_response": resp,
        "answer_sent": final_answer,
        "submit_url": submit_url
    }

    if next_url:
        remaining = MAX_SECONDS - (time.time() - start_time) - 5
        if remaining > 5:
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.goto(next_url, timeout=PLAYWRIGHT_TIMEOUT)
                    page.wait_for_load_state("networkidle", timeout=PLAYWRIGHT_TIMEOUT)
                    content2 = page.content()
                    browser.close()
                result["followed_next_url"] = next_url
                result["next_snippet"] = content2[:1000]
            except Exception as e:
                result["follow_error"] = str(e)

    return jsonify(result), 200

@app.route("/")
def home():
    return "LLM Quiz Solver is running!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
