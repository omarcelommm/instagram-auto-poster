"""
API backend para o painel do Instagram Auto Poster.
"""

import os
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

TZ_BRASILIA = timezone(timedelta(hours=-3))
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
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

# ── Settings ──────────────────────────────────────────────────────────────────

SETTINGS_FILE = Path(__file__).parent / "settings.json"

DEFAULT_SETTINGS = {
    "auto_post": True,
    "posts_per_day": 3,
    "interval_minutes": 240,
    "start_hour": "07",
    "end_hour": "21",
    "active_days": [0, 1, 2, 3, 4, 5, 6],
}

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text())}
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(data: dict):
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

class Settings(BaseModel):
    auto_post: bool
    posts_per_day: int
    interval_minutes: int
    start_hour: str
    end_hour: str
    active_days: List[int]

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
    ultimo_post = log[-1].get("posted_at") if log else None
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


@app.get("/queue")
def get_queue():
    todos = listar_videos_drive()
    postados = carregar_postados()
    fila = [v["name"] for v in todos if v["name"] not in postados]
    return {"queue": fila}


# ── Debug: ver resposta bruta da API Meta ────────────────────────────────────

@app.get("/debug/{post_id}")
def debug_post(post_id: str):
    """Retorna resposta bruta da API Meta para diagnóstico de métricas."""
    results = {}

    # Testa métricas de insights com vários nomes possíveis
    for metric in [
        "plays",
        "ig_reels_aggregated_all_plays_count",
        "ig_reels_video_view_total_time",
        "reach,saved",
        "total_interactions",
    ]:
        resp = requests.get(
            f"{GRAPH_API}/{post_id}/insights",
            params={"metric": metric, "period": "lifetime", "access_token": META_ACCESS_TOKEN},
            timeout=15,
        )
        results[f"insights:{metric}"] = resp.json()

    # Testa campos diretos no objeto de mídia
    resp2 = requests.get(
        f"{GRAPH_API}/{post_id}",
        params={"fields": "like_count,comments_count,media_type,timestamp", "access_token": META_ACCESS_TOKEN},
        timeout=15,
    )
    results["media_fields"] = resp2.json()

    return results


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


@app.get("/analytics")
def get_analytics():
    log = carregar_log()
    posts_with_ids = [p for p in log if p.get("post_id")]

    enriched = []
    for post in posts_with_ids:
        post_id = post["post_id"]
        metrics = {}
        try:
            # Insights: reach, saved e tempo de visualização (plays não é suportado via API)
            resp = requests.get(
                f"{GRAPH_API}/{post_id}/insights",
                params={
                    "metric": "reach,saved,ig_reels_video_view_total_time",
                    "period": "lifetime",
                    "access_token": META_ACCESS_TOKEN,
                },
                timeout=15,
            )
            data = resp.json()
            if "data" in data:
                for item in data["data"]:
                    val = item.get("value", item.get("values", [{}])[0].get("value", 0) if item.get("values") else 0)
                    metrics[item["name"]] = val
        except Exception:
            pass

        permalink = None
        try:
            # Curtidas, comentários e permalink vêm do objeto de mídia diretamente
            resp2 = requests.get(
                f"{GRAPH_API}/{post_id}",
                params={
                    "fields": "like_count,comments_count,permalink",
                    "access_token": META_ACCESS_TOKEN,
                },
                timeout=15,
            )
            media = resp2.json()
            metrics["likes"] = media.get("like_count", 0)
            metrics["comments"] = media.get("comments_count", 0)
            permalink = media.get("permalink")
        except Exception:
            pass

        posted_at = post.get("posted_at", "")
        hour, day = None, None
        if posted_at:
            try:
                dt = datetime.fromisoformat(posted_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=TZ_BRASILIA)
                else:
                    dt = dt.astimezone(TZ_BRASILIA)
                hour = dt.hour
                day = dt.weekday()  # 0=seg, 6=dom
            except Exception:
                pass

        enriched.append({
            **post,
            "reach": metrics.get("reach", 0),
            "saved": metrics.get("saved", 0),
            "watch_time_ms": metrics.get("ig_reels_video_view_total_time", 0),
            "likes": metrics.get("likes", 0),
            "comments": metrics.get("comments", 0),
            "permalink": permalink,
            "hour": hour,
            "day": day,
        })

    return {"posts": enriched}


# ── Settings endpoints ────────────────────────────────────────────────────────

@app.get("/settings")
def get_settings():
    return load_settings()

@app.post("/settings")
def update_settings(s: Settings):
    save_settings(s.dict())
    return {"ok": True}


# ── Postar agora ──────────────────────────────────────────────────────────────

posting_status = {"running": False, "current_step": None, "last_result": None}

def _check_settings() -> str | None:
    """Retorna mensagem de bloqueio ou None se pode postar."""
    s = load_settings()
    if not s["auto_post"]:
        return "Postagem automática desativada nas configurações."
    now = datetime.now(TZ_BRASILIA)
    if now.weekday() not in s["active_days"]:
        return "Dia inativo nas configurações."
    if int(now.strftime("%H")) < int(s["start_hour"]):
        return f"Fora do horário ativo (início: {s['start_hour']}h)."
    if int(now.strftime("%H")) >= int(s["end_hour"]):
        return f"Fora do horário ativo (fim: {s['end_hour']}h)."
    # Verifica limite diário
    today = now.strftime("%Y-%m-%d")
    log = carregar_log()
    posts_hoje = sum(1 for p in log if p.get("posted_at", "").startswith(today))
    if posts_hoje >= s["posts_per_day"]:
        return f"Limite diário atingido ({posts_hoje}/{s['posts_per_day']} posts)."
    return None

NTFY_TOPIC = "marcelo-social-media-alerts"

def _notify(title: str, message: str, priority: str = "default"):
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            timeout=15,
        )
        print(f"ntfy: {r.status_code} — {title}")
    except Exception as e:
        print(f"ntfy erro: {e}")

def executar_post():
    posting_status["running"] = True
    posting_status["current_step"] = "Selecionando vídeo..."
    video = None
    _notify("⏳ Instagram — Postagem iniciada", "Selecionando vídeo e iniciando processo...")
    try:
        video, filename = selecionar_video()
        if not video:
            posting_status["last_result"] = {"success": False, "message": "Nenhum vídeo disponível."}
            _notify("⚠️ Instagram — Sem vídeos", "Nenhum vídeo disponível na fila.", priority="high")
            return
        posting_status["current_step"] = "Transcrevendo áudio..."
        transcricao = transcrever(video)
        posting_status["current_step"] = "Gerando legenda com IA..."
        legenda = gerar_legenda(transcricao)
        posting_status["current_step"] = "Fazendo upload para Cloudinary..."
        video_url = fazer_upload_publico(video)
        posting_status["current_step"] = "Criando container no Instagram..."
        container_id = criar_container(video_url, legenda)
        posting_status["current_step"] = "Aguardando processamento do Instagram..."
        aguardar_processamento(container_id)
        posting_status["current_step"] = "Publicando..."
        post_id = publicar(container_id)
        restantes = len(listar_videos_drive()) - len(carregar_postados())
        salvar_postado(filename, post_id, legenda, video_url)
        posting_status["current_step"] = None
        posting_status["last_result"] = {
            "success": True,
            "post_id": post_id,
            "filename": filename,
            "caption": legenda,
            "posted_at": datetime.now(TZ_BRASILIA).isoformat(),
        }
        _notify(
            "✓ Instagram — Post publicado",
            f"{filename}\n{restantes} vídeos restantes na fila.",
        )
    except Exception as e:
        posting_status["current_step"] = None
        posting_status["last_result"] = {"success": False, "message": str(e)}
        _notify("✗ Instagram — Falha na postagem", str(e), priority="high")
    finally:
        posting_status["running"] = False
        if video:
            video.unlink(missing_ok=True)

@app.post("/post/now")
def post_now(background_tasks: BackgroundTasks):
    if posting_status["running"]:
        raise HTTPException(status_code=409, detail="Já há uma postagem em andamento.")
    bloqueio = _check_settings()
    if bloqueio:
        raise HTTPException(status_code=403, detail=bloqueio)
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
