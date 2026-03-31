"""
Seleciona um vídeo aleatório, transcreve, gera legenda e posta no Instagram.
Rode via cron 3x/dia.
"""

import os
import json
import random
import time
import tempfile
import shutil
import subprocess
from pathlib import Path
from dotenv import load_dotenv
import openai

# Garante que ffmpeg está no PATH (baixa automaticamente se não estiver instalado)
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except ImportError:
    pass
import anthropic
import requests
import cloudinary
import cloudinary.uploader
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)

# Config
INSTAGRAM_ACCOUNT_ID = os.getenv("INSTAGRAM_ACCOUNT_ID")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "1R6Bvuj5rwDDezfiIn649RUvTVLpyvQvb")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

POSTED_LOG = Path(__file__).parent / "posted_videos.json"
GRAPH_API = "https://graph.instagram.com/v22.0"
DATABASE_URL = os.getenv("DATABASE_URL")
NTFY_TOPIC = "marcelo-social-media-alerts"


# ── Google Drive ───────────────────────────────────────────────────────────────

def _drive_service():
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        import json as _json
        import base64 as _b64
        import re as _re
        raw = GOOGLE_SERVICE_ACCOUNT_JSON.strip()
        # Try base64-encoded JSON first (most reliable for env vars)
        try:
            decoded = _b64.b64decode(raw).decode('utf-8')
            info = _json.loads(decoded)
        except Exception:
            # Fallback: raw JSON with newline normalization
            raw = GOOGLE_SERVICE_ACCOUNT_JSON.replace('\r\n', '\n').replace('\r', '\n')
            try:
                info = _json.loads(raw)
            except _json.JSONDecodeError:
                def _fix_key(m):
                    return m.group(1) + m.group(2).replace('\n', '\\n') + m.group(3)
                raw = _re.sub(r'("private_key"\s*:\s*")(.*?)(")', _fix_key, raw, flags=_re.DOTALL)
                info = _json.loads(raw)
            if 'private_key' in info:
                info['private_key'] = info['private_key'].replace('\\n', '\n')
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        sa_file = Path(__file__).parent / "service_account.json"
        creds = service_account.Credentials.from_service_account_file(str(sa_file), scopes=scopes)
    return build("drive", "v3", credentials=creds)


def listar_videos_drive() -> list[dict]:
    service = _drive_service()
    result = service.files().list(
        q=f"'{GOOGLE_DRIVE_FOLDER_ID}' in parents and trashed=false and (mimeType contains 'video/')",
        fields="files(id, name)",
        pageSize=200,
    ).execute()
    return result.get("files", [])


def baixar_video_drive(file_id: str, filename: str) -> Path:
    service = _drive_service()
    tmp = tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False)
    tmp.close()
    request = service.files().get_media(fileId=file_id)
    with open(tmp.name, "wb") as f:
        downloader = MediaIoBaseDownload(f, request, chunksize=10 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return Path(tmp.name)


# ── Controle de vídeos postados ────────────────────────────────────────────────

def _db_conn():
    if not DATABASE_URL:
        return None
    import psycopg2
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS posted_videos (
                id SERIAL PRIMARY KEY,
                filename TEXT NOT NULL,
                post_id TEXT,
                caption TEXT,
                video_url TEXT,
                posted_at TIMESTAMP DEFAULT NOW()
            )
        """)
    conn.commit()
    return conn

def carregar_log() -> list:
    from datetime import timezone, timedelta
    tz_utc = timezone.utc
    tz_brasilia = timezone(timedelta(hours=-3))
    conn = _db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT filename, post_id, caption, video_url, posted_at FROM posted_videos ORDER BY posted_at ASC")
                rows = cur.fetchall()
            conn.close()
            def fmt_dt(dt):
                if not dt:
                    return None
                # psycopg2 + TIMESTAMP WITHOUT TIME ZONE: armazena o horário local (Brasília)
                # sem conversão para UTC — basta anexar o tz correto diretamente.
                return dt.replace(tzinfo=tz_brasilia).isoformat()
            return [
                {"filename": r[0], "post_id": r[1], "caption": r[2], "video_url": r[3],
                 "posted_at": fmt_dt(r[4])}
                for r in rows
            ]
        except Exception as e:
            print(f"Erro ao ler banco: {e}")
            conn.close()
    # fallback: arquivo local
    if POSTED_LOG.exists():
        dados = json.loads(POSTED_LOG.read_text())
        if dados and isinstance(dados[0], str):
            return [{"filename": f} for f in dados]
        return dados
    return []

def carregar_postados() -> set:
    return {e["filename"] for e in carregar_log()}

def salvar_postado(nome: str, post_id: str, legenda: str, video_url: str):
    from datetime import datetime, timezone, timedelta
    tz_brasilia = timezone(timedelta(hours=-3))
    agora = datetime.now(tz_brasilia)
    conn = _db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO posted_videos (filename, post_id, caption, video_url, posted_at) VALUES (%s, %s, %s, %s, %s)",
                    (nome, post_id, legenda, video_url, agora),
                )
            conn.commit()
            conn.close()
            return
        except Exception as e:
            print(f"Erro ao salvar no banco: {e}")
            conn.close()
    # fallback: arquivo local
    log = carregar_log()
    log.append({
        "filename": nome, "post_id": post_id, "caption": legenda,
        "video_url": video_url, "posted_at": agora.isoformat(),
    })
    POSTED_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2))


# ── Selecionar vídeo ──────────────────────────────────────────────────────────

def selecionar_video() -> tuple[Path, str] | tuple[None, None]:
    """Retorna (path_local_temporário, filename) ou (None, None)."""
    postados = carregar_postados()
    todos = listar_videos_drive()
    if not todos:
        print("Nenhum vídeo encontrado no Google Drive.")
        return None, None
    disponiveis = [v for v in todos if v["name"] not in postados]
    if not disponiveis:
        print(f"Todos os {len(todos)} vídeos já foram postados.")
        return None, None
    print(f"Vídeos restantes: {len(disponiveis)}/{len(todos)}")
    escolhido = random.choice(disponiveis)
    print(f"Baixando do Drive: {escolhido['name']}...")
    tmp_path = baixar_video_drive(escolhido["id"], escolhido["name"])
    return tmp_path, escolhido["name"]


# ── Transcrição ───────────────────────────────────────────────────────────────

def transcrever(video_path: Path) -> str:
    print(f"Transcrevendo: {video_path.name}...")
    tamanho_mb = video_path.stat().st_size / (1024 * 1024)
    print(f"Tamanho do vídeo: {tamanho_mb:.1f}MB")
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        audio_path = tmp.name
    try:
        print("Extraindo áudio com ffmpeg...")
        subprocess.run(
            ["ffmpeg", "-i", str(video_path), "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k", audio_path, "-y"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
        audio_mb = Path(audio_path).stat().st_size / (1024 * 1024)
        print(f"Áudio extraído: {audio_mb:.1f}MB — enviando para Whisper...")
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        with open(audio_path, "rb") as f:
            resultado = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="pt",
            )
        print(f"Transcrição concluída: {len(resultado.text)} caracteres")
        return resultado.text
    finally:
        Path(audio_path).unlink(missing_ok=True)


# ── Geração de legenda ────────────────────────────────────────────────────────

def gerar_legenda(transcricao: str) -> str:
    print("Gerando legenda com Claude...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resposta = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""Você é um especialista em copywriting para Instagram de médicos e profissionais de saúde.

Com base nesta transcrição de um corte de aula, escreva uma legenda para Instagram que:
- Comece com um gancho forte (primeira linha impacta ou gera curiosidade)
- Seja direta, sem enrolação
- Tenha entre 150 e 300 palavras
- Use quebras de linha para facilitar a leitura
- Inclua 3 a 5 hashtags relevantes no final
- Tom: autoridade + proximidade. Nunca use emojis em excesso — no máximo 2.
- NÃO use introduções como "Nesta aula..." ou "Neste vídeo..."
- NÃO use formatação Markdown (sem asteriscos, sem negrito, sem itálico). Texto puro apenas.

Transcrição:
{transcricao}

Escreva apenas a legenda, sem comentários adicionais."""
        }]
    )
    return resposta.content[0].text


# ── Upload para host temporário ────────────────────────────────────────────────

def comprimir_video(video_path: Path) -> Path:
    tamanho_mb = video_path.stat().st_size / (1024 * 1024)
    if tamanho_mb <= 80:
        return video_path
    print(f"Vídeo grande ({tamanho_mb:.0f}MB) — comprimindo antes do upload...")
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()
    subprocess.run(
        ["ffmpeg", "-i", str(video_path), "-vcodec", "libx264", "-crf", "28",
         "-acodec", "aac", "-b:a", "128k", "-movflags", "+faststart", tmp.name, "-y"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
    )
    return Path(tmp.name)


def fazer_upload_publico(video_path: Path) -> str:
    tamanho_mb = video_path.stat().st_size / (1024 * 1024)
    print(f"Fazendo upload do vídeo para Cloudinary ({tamanho_mb:.1f}MB)...")
    try:
        resultado = cloudinary.uploader.upload_large(
            str(video_path),
            resource_type="video",
            folder="instagram_posts",
            chunk_size=6 * 1024 * 1024,
        )
    finally:
        pass
    url = resultado["secure_url"]
    print(f"URL pública: {url}")
    return url


# ── Postagem no Instagram ─────────────────────────────────────────────────────

def criar_container(video_url: str, legenda: str) -> str:
    print("Criando container de mídia no Instagram...")
    response = requests.post(
        f"{GRAPH_API}/{INSTAGRAM_ACCOUNT_ID}/media",
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": legenda,
            "share_to_feed": "true",
            "access_token": META_ACCESS_TOKEN,
        },
        timeout=60,
    )
    data = response.json()
    if "error" in data:
        raise Exception(f"Erro ao criar container: {data['error']}")
    return data["id"]


def aguardar_processamento(container_id: str, max_tentativas: int = 20):
    print("Aguardando processamento do vídeo pelo Instagram...")
    for i in range(max_tentativas):
        response = requests.get(
            f"{GRAPH_API}/{container_id}",
            params={
                "fields": "status_code",
                "access_token": META_ACCESS_TOKEN,
            },
            timeout=30,
        )
        status = response.json().get("status_code", "")
        print(f"  Status: {status} (tentativa {i+1}/{max_tentativas})")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise Exception("Instagram retornou erro no processamento do vídeo.")
        time.sleep(15)
    raise Exception("Timeout aguardando processamento do vídeo.")


def publicar(container_id: str) -> str:
    print("Publicando no Instagram...")
    response = requests.post(
        f"{GRAPH_API}/{INSTAGRAM_ACCOUNT_ID}/media_publish",
        data={
            "creation_id": container_id,
            "access_token": META_ACCESS_TOKEN,
        },
        timeout=30,
    )
    data = response.json()
    if "error" in data:
        raise Exception(f"Erro ao publicar: {data['error']}")
    return data["id"]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("Instagram Auto Poster")
    print("=" * 50)

    video, filename = selecionar_video()
    if not video:
        return

    print(f"\nVídeo selecionado: {filename}")

    try:
        transcricao = transcrever(video)
        legenda = gerar_legenda(transcricao)
        print(f"\nLegenda gerada:\n{legenda}\n")

        video_url = fazer_upload_publico(video)
        container_id = criar_container(video_url, legenda)
        aguardar_processamento(container_id)
        post_id = publicar(container_id)

        salvar_postado(filename, post_id, legenda, video_url)
        postados = carregar_postados()
        todos = listar_videos_drive()
        restantes = len(todos) - len(postados)
        print(f"\nPostado com sucesso! ID: {post_id}")
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=f"Postado: {filename}\nVídeos restantes: {restantes}/{len(todos)}".encode("utf-8"),
            headers={"Title": "✓ Instagram — Post publicado", "Priority": "default"},
            timeout=10,
        )

    except Exception as e:
        print(f"\nErro: {e}")
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=f"Erro ao postar no Instagram: {e}".encode("utf-8"),
            headers={"Title": "✗ Instagram — Falha na postagem", "Priority": "high"},
            timeout=10,
        )
        raise
    finally:
        video.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
