"""
API backend para o painel do Instagram Auto Poster.
"""

import os
import subprocess
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

from postar_instagram import (
    carregar_log,
    carregar_postados,
    listar_videos_drive,
    selecionar_video,
    transcrever,
    gerar_legenda,
    fazer_upload_publico,
    criar_container,
    aguardar_processamento,
    publicar,
    salvar_postado,
    INSTAGRAM_ACCOUNT_ID,
    META_ACCESS_TOKEN,
    GRAPH_API,
)

app = FastAPI(title="Instagram Auto Poster API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Status geral ──────────────────────────────────────────────────────────────

@app.get("/status")
def get_status():
    todos = listar_videos_drive()
    postados = carregar_postados()
    restantes = len(todos) - len(postados)
    log = carregar_log()
    ultimo_post = log[-1] if log else None
    return {
        "total_videos": len(todos),
        "postados": len(postados),
        "restantes": restantes,
        "ultimo_post": ultimo_post,
    }


# ── Histórico de posts ────────────────────────────────────────────────────────

@app.get("/posts")
def get_posts():
    log = carregar_log()
    return {"posts": list(reversed(log))}


# ── Analytics de um post ──────────────────────────────────────────────────────

@app.get("/posts/{post_id}/insights")
def get_insights(post_id: str):
    response = requests.get(
        f"{GRAPH_API}/{post_id}/insights",
        params={
            "metric": "plays,likes,comments,reach,saved",
            "access_token": META_ACCESS_TOKEN,
        },
        timeout=15,
    )
    data = response.json()
    if "error" in data:
        raise HTTPException(status_code=400, detail=data["error"])
    return data


# ── Postar agora ──────────────────────────────────────────────────────────────

posting_status = {"running": False, "last_result": None}

def executar_post():
    posting_status["running"] = True
    video = None
    try:
        video, filename = selecionar_video()
        if not video:
            posting_status["last_result"] = {"success": False, "message": "Nenhum vídeo disponível."}
            return
        transcricao = transcrever(video)
        legenda = gerar_legenda(transcricao)
        video_url = fazer_upload_publico(video)
        container_id = criar_container(video_url, legenda)
        aguardar_processamento(container_id)
        post_id = publicar(container_id)
        salvar_postado(filename, post_id, legenda, video_url)
        posting_status["last_result"] = {
            "success": True,
            "post_id": post_id,
            "filename": filename,
            "caption": legenda,
            "posted_at": datetime.now().isoformat(),
        }
    except Exception as e:
        posting_status["last_result"] = {"success": False, "message": str(e)}
    finally:
        posting_status["running"] = False
        if video:
            video.unlink(missing_ok=True)

@app.post("/post/now")
def post_now(background_tasks: BackgroundTasks):
    if posting_status["running"]:
        raise HTTPException(status_code=409, detail="Já há uma postagem em andamento.")
    background_tasks.add_task(executar_post)
    return {"message": "Postagem iniciada em segundo plano."}

@app.get("/post/status")
def get_post_status():
    return posting_status


# ── Horários do cron ──────────────────────────────────────────────────────────

@app.get("/schedule")
def get_schedule():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    linhas = [l for l in result.stdout.splitlines() if "postar_instagram.py" in l]
    horarios = []
    for linha in linhas:
        partes = linha.split()
        if len(partes) >= 2:
            horarios.append(f"{partes[1].zfill(2)}:{partes[0].zfill(2)}")
    return {"horarios": sorted(horarios)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
