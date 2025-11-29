import requests

ENDPOINT = "https://llm-quiz-solver-8fhc.onrender.com/api/quiz"  # change if needed

payload = {
    "email": "23f2004216@ds.study.iitm.ac.in",
    "secret": "42e57fd2-361c-492f-9566-4c08483b9d04",
    "url": "https://tds-llm-analysis.s-anand.net/demo"
}

r = requests.post(ENDPOINT, json=payload, timeout=90)
print("STATUS:", r.status_code)
print("RESPONSE:", r.text)
