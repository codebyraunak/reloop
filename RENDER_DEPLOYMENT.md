# Deploying ReLoop / EcoRoute to Render

This project is configured for one-click deployment on [Render](https://render.com) using Python 3.10+ and FastAPI.

---

## Prerequisites

1. A [GitHub](https://github.com) or [GitLab](https://gitlab.com) account.
2. A free [Render account](https://render.com).
3. Your Google Gemini API key.

---

## Option 1: Render Blueprint (`render.yaml`) — Recommended

The repository includes a ready-to-use `render.yaml` specification.

1. **Push your code to GitHub**:
   ```bash
   git init
   git add .
   git commit -m "Deploy ReLoop to Render with Gemini integration"
   git remote add origin https://github.com/<your-username>/<your-repo-name>.git
   git branch -M main
   git push -u origin main
   ```
2. In the **Render Dashboard**, click **New +** → **Blueprint**.
3. Select your repository.
4. Render will parse `render.yaml` automatically:
   - **Service Name**: `reloop-ecoroute`
   - **Environment**: Python
   - **Root Directory**: `backend` (or `ecoroute/backend`)
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. When prompted for environment variables, fill in:
   - `GEMINI_API_KEY`: *(paste your Gemini API key)*
   - `GEMINI_MODEL`: `gemini-flash-lite-latest`
6. Click **Apply**. Render will build and deploy your app.

---

## Option 2: Manual Web Service Setup on Render

1. Push your repository to GitHub or GitLab.
2. Log in to [dashboard.render.com](https://dashboard.render.com/).
3. Click **New +** → **Web Service**.
4. Choose **Build and deploy from a Git repository** and connect your repo.
5. Fill in the service configuration:
   - **Name**: `reloop-ecoroute`
   - **Region**: Choose the closest region to you (e.g., Oregon, Frankfurt, Singapore)
   - **Root Directory**: `backend` (if repo root is `ecoroute`, set `ecoroute/backend`)
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Plan**: `Free`
6. Under **Environment Variables**, click **Add Environment Variable**:
   - `GEMINI_API_KEY` = *(your Gemini API key)*
   - `GEMINI_MODEL` = `gemini-flash-lite-latest`
   - `PYTHON_VERSION` = `3.10.11`
7. Click **Create Web Service**.

---

## Option 3: Docker Deployment on Render

If you prefer containerized deployment:
1. In Render, select **New +** → **Web Service**.
2. Select **Docker** as the runtime.
3. If deploying from the repository root, set the Dockerfile path to `backend/Dockerfile` or `Dockerfile`.
4. Add environment variables:
   - `GEMINI_API_KEY` = *(your Gemini API key)*
   - `GEMINI_MODEL` = `gemini-flash-lite-latest`
5. Click **Create Web Service**.

---

## Verifying Deployment

Once the build finishes and shows **Live**:
- **Application URL**: `https://<your-service-name>.onrender.com`
- **Health check**: `https://<your-service-name>.onrender.com/api/health`
  - Returns `{"status":"ok","service":"EcoRoute","mode":"mock-demo","gemini_configured":true,"gemini_model":"gemini-flash-lite-latest"}`
- **Interactive UI**: Open the app URL in any browser. The AI chat widget will show **Gemini AI active** and all 4 scenario benchmarks can be executed right from the frontend.
