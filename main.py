import os
import uuid
import json
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from typing import Dict

from google.oauth2 import service_account
from googleapiclient.discovery import build

app = FastAPI()

# ==============================
# CONFIG DRIVE
# ==============================

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

def conectar_drive():
    credentials_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")

    if not credentials_json:
        raise Exception("No se encontró GOOGLE_CREDENTIALS_JSON en variables de entorno")

    credentials_dict = json.loads(credentials_json)

    creds = service_account.Credentials.from_service_account_info(
        credentials_dict,
        scopes=SCOPES
    )

    service = build('drive', 'v3', credentials=creds)
    return service


def listar_pdfs(service, folder_id):
    query = f"'{folder_id}' in parents and mimeType='application/pdf'"

    results = service.files().list(
        q=query,
        fields="files(id, name)"
    ).execute()

    return results.get('files', [])


# ==============================
# MODELO REQUEST
# ==============================

class DriveRequest(BaseModel):
    folder_id: str


# ==============================
# STORAGE EN MEMORIA
# ==============================

jobs: Dict[str, dict] = {}


# ==============================
# FUNCION BACKGROUND
# ==============================

def procesar_drive_job(job_id: str, folder_id: str):

    jobs[job_id]["status"] = "procesando"

    try:
        service = conectar_drive()

        archivos = listar_pdfs(service, folder_id)

        jobs[job_id]["status"] = "finalizado"
        jobs[job_id]["resumen"] = f"Procesado folder {folder_id}"
        jobs[job_id]["documentos_procesados"] = len(archivos)
        jobs[job_id]["archivos"] = [a["name"] for a in archivos]

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


# ==============================
# ENDPOINT INICIO
# ==============================

@app.post("/procesar")
def iniciar_proceso(request: DriveRequest, background_tasks: BackgroundTasks):

    job_id = str(uuid.uuid4())

    jobs[job_id] = {
        "status": "en_cola",
        "folder_id": request.folder_id
    }

    background_tasks.add_task(
        procesar_drive_job,
        job_id,
        request.folder_id
    )

    return {
        "status": "en_proceso",
        "job_id": job_id
    }


# ==============================
# ENDPOINT ESTADO
# ==============================

@app.get("/estado/{job_id}")
def consultar_estado(job_id: str):

    if job_id not in jobs:
        return {"error": "Job no encontrado"}

    return jobs[job_id]
