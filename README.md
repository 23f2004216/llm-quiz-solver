# llm-quiz-solver
## Deploy (Render Docker)

1. Ensure Dockerfile is in repo root (already present).
2. Push repo to GitHub (public).
3. On Render: create **New → Web Service** and choose your repo.
   - Render will detect Dockerfile and build.
4. If using environment variables:
   - Add `QUIZ_SECRET=42e57fd2-361c-492f-9566-4c08483b9d04` in Render dashboard (Environment).
5. Test with:
   python test_api.py   # or use script shown in README
