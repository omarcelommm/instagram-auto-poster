"""
Sincroniza vídeos da pasta do Google Drive para a pasta local videos/.
Execute quando adicionar novos vídeos ao Drive.
"""

import gdown
from pathlib import Path

DRIVE_FOLDER_URL = "https://drive.google.com/drive/folders/1zmH4XKrAgLvLSFJHNxlevKo02hjwBdHT"
VIDEOS_DIR = Path(__file__).parent / "videos"

def sincronizar():
    VIDEOS_DIR.mkdir(exist_ok=True)
    print(f"Sincronizando vídeos do Drive para {VIDEOS_DIR}...")
    gdown.download_folder(
        url=DRIVE_FOLDER_URL,
        output=str(VIDEOS_DIR),
        quiet=False,
        use_cookies=False,
        remaining_ok=True,
    )
    videos = list(VIDEOS_DIR.glob("*.mp4")) + list(VIDEOS_DIR.glob("*.mov"))
    print(f"\n{len(videos)} vídeo(s) disponíveis localmente.")

if __name__ == "__main__":
    sincronizar()
