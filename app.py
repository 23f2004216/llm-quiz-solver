# app.py
import os
import re
import time
import json
import base64
import tempfile
import requests
from urllib.parse import urljoin, urlparse
from flask import Flask, request, jsonify

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import pandas as pd
import pdfplumber

# Configuration
SECRET = os.environ.get("QUIZ_SECRET", "42e57fd2-361c-492f-9566-4c08483b9d04")
MAX_SECONDS = int(os.environ.get("QUIZ_MAX_SECONDS", "170"))  # under 3 minutes
PLAYWRIGHT_TIMEOUT = int(os.environ.get("PLAYWRIGHT_TIMEOUT_MS", "60000"))  # ms per action

app = Flask(__name__)

def safe_json(req):
    try:
        return req.get_json(force=True)
    except Exception:
        return None

def find_submit_url_from_html(soup_text):
    # look for obvious submit endpoints
    m = re.search(r"https?://[^\s'\"<>]+/submit[^\s'\"<>]*", soup_text, re.IGNORECASE)
    if m:
        return m.group(0)
    # sometimes action in forms
    m2 = re.search(r'action=["\']([^"\']+)["\']', soup_text, re.IGNORECASE)
    if m2:
        return m2.group(1)
    return None

def extract_b64_candidates(text):
    # capture long base64-like strings
    candidates = re.findall(r"[A-Za-z0-9+/=]{80,}", text)
    return candidates

def try_decode_b64(s):
    try:
        b = base64.b64decode(s, validate=False)
        return b
    except Exception:
        return None

def download_file(url, dest_folder="/tmp"):
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
    # try CSV then excel
    try:
        if path.lower().endswith(".csv") or path.lower().endswith(".txt"):
            df = pd.read_csv(path)
            return df
        if path.lower().endswith((".xls", ".xlsx")):
            df = pd.read_excel(path)
            return df
    except Exception:
        return None
    return None

def parse_pdf_for_tables(path):
    # try to extract tables or text, return list of DataFrames or text
    tables = []
    text = ""
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                # table extraction if present
                try:
                    page_tables = page.extract_tables()
                    for t in page_tables:
                        if t:
                            # convert to DataFrame
                            df = pd.DataFrame(t[1:], columns=t[0]) if len(t) > 1 else pd.DataFrame(t)
                            tables.append(df)
                except Exception:
                    pass
                try:
                    text += page.extract_text() or ""
                except Exception:
                    pass
    except Exception:
        return tables, None
    return tables, text

def compute_answer_from_dataframe(df):
    # heuristic: if 'value' column present, sum it
    if df is None or df.empty:
        return None
    cols = [c.lower() for c in df.columns.astype(str)]
    if "value" in cols:
        try:
            s = df.iloc[:, cols.index("value")].astype(float).sum()
            return float(s) if not pd.isna(s) else None
        except Exception:
            pass
    # fallback: if any numeric column exists, return sum of first numeric column
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            try:
                s = df[col].sum()
                return float(s)
            except Exception:
                pass
    return None

def find_numeric_in_text(text):
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
    start = time.time()
    data = safe_json(request)
    if data is None:
        return ("Invalid JSON", 400)

    email = data.get("email")
    secret = data.get("secret")
    url = data.get("url")
    if not email or not secret or not url:
        return jsonify({"error": "Missing fields"}), 400
    if secret != SECRET:
        return jsonify({"error": "Invalid secret"}), 403

    # rendering + scraping
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.goto(url, timeout=PLAYWRIGHT_TIMEOUT)
            page.wait_for_load_state("networkidle", timeout=PLAYWRIGHT_TIMEOUT)
            # grab page content and scripts
            content = page.content()
            text_content = page.inner_text("body") if page.query_selector("body") else content
            scripts = ""
            try:
                # collect inline scripts
                scripts = page.query_selector_all("script")
                scripts_text = []
                for s in scripts:
                    try:
                        txt = s.inner_text()
                        if txt:
                            scripts_text.append(txt)
                    except Exception:
                        pass
                scripts = "\n".join(scripts_text)
            except Exception:
                scripts = ""

            browser.close()
    except PWTimeout as e:
        return jsonify({"error": "timeout rendering page", "detail": str(e)}), 500
    except Exception as e:
        return jsonify({"error": "render error", "detail": str(e)}), 500

    # quick heuristics
    soup_text = text_content + "\n" + scripts + "\n" + content

    # find submit url
    submit_url = find_submit_url_from_html(soup_text)
    # if submit_url is relative, join with page url
    if submit_url and not submit_url.lower().startswith("http"):
        submit_url = urljoin(url, submit_url)

    # 1) Check for base64 payloads in scripts
    b64_candidates = extract_b64_candidates(scripts + "\n" + content)
    decoded_json = None
    for c in b64_candidates:
        d = try_decode_b64(c)
        if d:
            try:
                txt = d.decode("utf-8", errors="ignore")
                # sometimes there's a <pre>{...}</pre>
                j = None
                try:
                    j = json.loads(txt)
                except Exception:
                    # try to find JSON in text
                    m = re.search(r"\{[\s\S]*\}", txt)
                    if m:
                        try:
                            j = json.loads(m.group(0))
                        except Exception:
                            j = None
                if j:
                    decoded_json = j
                    break
            except Exception:
                pass

    # 2) If decoded_json contains a direct answer structure, use it
    if decoded_json and "answer" in decoded_json:
        final_answer = decoded_json["answer"]
    else:
        # 3) find file urls in page (csv, pdf, xlsx)
        file_urls = re.findall(r"https?://[^\s'\"<>]+\.(?:csv|pdf|xlsx|xls|txt)", soup_text, re.IGNORECASE)
        final_answer = None

        # try parsing files (CSV/XLSX/PDF). Prefer first found.
        for f_url in file_urls:
            local = download_file(f_url, dest_folder=tempfile.gettempdir())
            if not local:
                continue
            # CSV/XLSX
            df = parse_csv_or_excel(local)
            if df is not None:
                val = compute_answer_from_dataframe(df)
                if val is not None:
                    final_answer = val
                    break
            # PDF
            if local.lower().endswith(".pdf"):
                tables, pdf_text = parse_pdf_for_tables(local)
                # check tables first
                for t in tables:
                    val = compute_answer_from_dataframe(t)
                    if val is not None:
                        final_answer = val
                        break
                if final_answer is not None:
                    break
                # try numeric in text
                if pdf_text:
                    num = find_numeric_in_text(pdf_text)
                    if num is not None:
                        final_answer = num
                        break

        # 4) If no files, try to find a numeric in the page text
        if final_answer is None:
            num = find_numeric_in_text(soup_text)
            if num is not None:
                final_answer = num

    # STOP if can't find answer — return helpful snippet for manual debug
    if final_answer is None:
        return jsonify({"status": "no_answer_found", "snippet": soup_text[:1500]}), 200

    # Ensure submit_url exists
    if not submit_url:
        # try to find /submit in scripts/text again
        s = re.search(r"https?://[^\s'\"<>]+/submit[^\s'\"<>]*", soup_text, re.IGNORECASE)
        if s:
            submit_url = s.group(0)

    if not submit_url:
        return jsonify({"status": "no_submit_url", "answer": final_answer}), 200

    # Prepare payload and post; allow retry within time window
    payload = {
        "email": email,
        "secret": secret,
        "url": url,
        "answer": final_answer
    }

    elapsed = time.time() - start
    if elapsed > MAX_SECONDS - 10:
        # time nearly up, send what we have
        code, resp = post_answer(submit_url, payload)
        return jsonify({"status": "submitted_partial_due_to_time", "post_status": code, "response": resp}), 200

    code, resp = post_answer(submit_url, payload)

    # If response contains new url, follow it (one level). You can loop more if needed
    next_url = None
    if isinstance(resp, dict):
        next_url = resp.get("url")

    final = {
        "submitted_status": code,
        "submit_response": resp,
        "answer_sent": final_answer,
        "submit_url": submit_url
    }

    # if there is a new url, optionally attempt to solve it (one level)
    if next_url:
        # small attempt to follow next url within remaining time
        remaining = MAX_SECONDS - (time.time() - start) - 5
        if remaining > 5:
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.goto(next_url, timeout=PLAYWRIGHT_TIMEOUT)
                    page.wait_for_load_state("networkidle", timeout=PLAYWRIGHT_TIMEOUT)
                    content2 = page.content()
                    browser.close()
                final["followed_next_url"] = next_url
                final["next_snippet"] = content2[:1000]
            except Exception as e:
                final["follow_error"] = str(e)

    return jsonify(final), 200

@app.route("/")
def home():
    return "LLM Quiz Solver is running!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
