from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import os

app = FastAPI(title="Uniter Shop Charging", version="1.0.0")

# Mount static and templates
os.makedirs("/home/jabboud/projects/Uniter/backend/static", exist_ok=True)
os.makedirs("/home/jabboud/projects/Uniter/backend/templates", exist_ok=True)

app.mount("/static", StaticFiles(directory="/home/jabboud/projects/Uniter/backend/static"), name="static")
templates = Jinja2Templates(directory="/home/jabboud/projects/Uniter/backend/templates")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "project": "Uniter", "operators": ["MTN", "Syriatel"]}
