import os
import uuid
import shutil
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from typing import Dict

app = FastAPI()

# ==============================
# MODELO REQUEST
# ==============================

class DriveRequest(BaseModel):
    folder_id: str

# ==============================
# STORAGE SIMPLE EN MEMORIA
# ==============================

jobs: Dict[str, dict] = {}

# ==============================
# FUNCION BACKGROUND
# ==============================

def procesar_drive_job(job_id: str, folder_id: str):

    jobs[job_id]["status"] = "procesando"

    try:
        # Crear carpeta temporal del job
        base_path = f"jobs/{job_id}"
        os.makedirs(base_path, exist_ok=True)

        # 🔹 ACÁ VAMOS A INTEGRAR:
        # - Descargar PDFs
        # - Procesar texto / vision
        # - Generar resumen

        # Simulación temporal
        import time
        time.sleep(5)

        resumen = f"Procesado folder {folder_id}"

        jobs[job_id]["status"] = "finalizado"
        jobs[job_id]["resumen"] = resumen
        jobs[job_id]["documentos_procesados"] = 14

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
