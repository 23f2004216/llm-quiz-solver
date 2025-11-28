from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright

app = Flask(__name__)

@app.route("/api/quiz", methods=["POST"])
def quiz():
    data = request.get_json()

    email = data.get("email")
    secret = data.get("secret")
    url = data.get("url")

    if not email or not secret or not url:
        return jsonify({"error": "Missing fields"}), 400

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url)
            title = page.title()
            browser.close()

        return jsonify({
            "email": email,
            "secret": secret,
            "url": url,
            "answer": f"Title extracted: {title}"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def home():
    return "LLM Quiz Solver is running!"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
