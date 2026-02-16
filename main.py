import os
import io
import re
import uuid
import json
import time
import tempfile
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

import pdfplumber
from pdf2image import convert_from_path

from openai import OpenAI

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm
from reportlab.lib.utils import simpleSplit


# =========================================================
# APP
# =========================================================
app = FastAPI()

from fastapi.middleware.cors import CORSMiddleware



app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # temporalmente abierto para test
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# CONFIG
# =========================================================
SCOPES = ["https://www.googleapis.com/auth/drive"]
MODEL_VISION = "gpt-4o"
MODEL_TEXT = "gpt-4o"

# Umbral simple: si hay menos de N caracteres, consideramos "sin texto útil"
MIN_CHARS_TEXT = 150

# Rate limit / pacing suave para evitar 429
SLEEP_BETWEEN_VISION_CALLS_SEC = 0.2


# =========================================================
# REQUEST MODEL
# =========================================================
class DriveRequest(BaseModel):
    folder_id: str


# =========================================================
# IN-MEMORY JOB STORE
# (ojo: si Railway reinicia, se pierde. Luego lo pasamos a DB)
# =========================================================
jobs: Dict[str, dict] = {}


# =========================================================
# DRIVE HELPERS
# =========================================================
def conectar_drive():
    credentials_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not credentials_json:
        raise Exception("No se encontró GOOGLE_CREDENTIALS_JSON en variables de entorno.")

    try:
        credentials_dict = json.loads(credentials_json)
    except json.JSONDecodeError as e:
        raise Exception(f"GOOGLE_CREDENTIALS_JSON no es JSON válido: {e}")

    creds = service_account.Credentials.from_service_account_info(
        credentials_dict,
        scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def drive_list_pdfs(service, folder_id: str) -> List[dict]:
    query = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"
    results = service.files().list(
        q=query,
        fields="files(id, name, mimeType, size)"
    ).execute()
    return results.get("files", [])


def drive_create_subfolder(service, parent_folder_id: str, folder_name: str) -> dict:
    file_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_folder_id],
    }
    created = service.files().create(body=file_metadata, fields="id, name").execute()
    return created


def drive_download_file_to_path(service, file_id: str, dest_path: str) -> None:
    request = service.files().get_media(fileId=file_id)
    fh = io.FileIO(dest_path, "wb")
    downloader = MediaIoBaseDownload(fh, request, chunksize=1024 * 1024)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.close()


def drive_upload_bytes(
    service,
    parent_folder_id: str,
    filename: str,
    mimetype: str,
    content_bytes: bytes
) -> dict:
    file_metadata = {"name": filename, "parents": [parent_folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(content_bytes), mimetype=mimetype, resumable=True)

    created = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, name, webViewLink"
    ).execute()
    return created


def drive_upload_path(
    service,
    parent_folder_id: str,
    filename: str,
    mimetype: str,
    path: str
) -> dict:
    file_metadata = {"name": filename, "parents": [parent_folder_id]}
    media = MediaIoBaseUpload(io.FileIO(path, "rb"), mimetype=mimetype, resumable=True)

    created = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, name, webViewLink"
    ).execute()
    return created


# =========================================================
# PDF TEXT DETECTION / EXTRACTION
# =========================================================
def extract_text_pdfplumber(pdf_path: str) -> str:
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            t = t.strip()
            if t:
                text_parts.append(t)
    return "\n\n".join(text_parts).strip()


def pdf_has_meaningful_text(pdf_text: str) -> bool:
    # Heurística simple: cantidad mínima de caracteres + presencia de palabras
    if not pdf_text:
        return False
    if len(pdf_text) < MIN_CHARS_TEXT:
        return False
    # si tiene al menos algunas letras, lo consideramos
    return bool(re.search(r"[A-Za-zÁÉÍÓÚáéíóúÑñ]{3,}", pdf_text))


# =========================================================
# PDF -> IMAGES (for Vision)
# =========================================================
def pdf_to_images(pdf_path: str, out_dir: str, base_name: str) -> List[str]:
    os.makedirs(out_dir, exist_ok=True)

    # DPI 200: equilibrio costo/legibilidad. Podés subir a 300 si necesitás.
    pages = convert_from_path(pdf_path, dpi=200)

    image_paths = []
    for i, img in enumerate(pages, start=1):
        fname = f"{base_name}_{str(i).zfill(5)}.jpg"
        fpath = os.path.join(out_dir, fname)
        # Si existe, no regeneramos (por si repetís job)
        if not os.path.exists(fpath):
            img.save(fpath, "JPEG", quality=85)
        image_paths.append(fpath)

    return image_paths


# =========================================================
# OPENAI HELPERS
# =========================================================
def get_openai_client() -> OpenAI:
    # Lee OPENAI_API_KEY automáticamente si está en env
    return OpenAI()


def image_file_to_data_url(path: str) -> str:
    import base64
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


def gpt_vision_extract_json(client: OpenAI, image_path: str) -> dict:
    data_url = image_file_to_data_url(image_path)

    # JSON mode: pedimos un JSON estricto.
    resp = client.chat.completions.create(
        model=MODEL_VISION,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Sos un auditor médico y administrativo. "
                    "Extraés información de documentos de legajo de pacientes (turnos, recetas, informes, autorizaciones, etc.). "
                    "Tu salida DEBE ser JSON válido y completo."
                )
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Extraé y estructurá la información del documento.\n\n"
                            "Devolvé un JSON con esta forma (completá lo que encuentres):\n"
                            "{\n"
                            '  "tipo_documento": "",\n'
                            '  "fecha_documento": "",\n'
                            '  "paciente": {"nombre": "", "dni": "", "cuil": ""},\n'
                            '  "prestador": {"nombre": "", "matricula": "", "institucion": ""},\n'
                            '  "diagnostico_texto": "",\n'
                            '  "medicacion": [{"nombre": "", "dosis": "", "frecuencia": "", "duracion": ""}],\n'
                            '  "estudios": [{"nombre": "", "hallazgos": ""}],\n'
                            '  "procedimientos": [{"nombre": "", "detalle": ""}],\n'
                            '  "cobertura": {"obra_social": "", "plan": "", "autorizacion": "", "vigencia": ""},\n'
                            '  "observaciones": "",\n'
                            '  "items_clave": [""]\n'
                            "}\n\n"
                            "Si algo no aparece, dejalo vacío. No inventes."
                        )
                    },
                    {"type": "image_url", "image_url": {"url": data_url}}
                ]
            }
        ],
        max_tokens=2500
    )

    content = resp.choices[0].message.content
    return json.loads(content)


def gpt_text_extract_json(client: OpenAI, pdf_text: str, doc_name: str) -> dict:
    resp = client.chat.completions.create(
        model=MODEL_TEXT,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Sos un auditor médico y administrativo. "
                    "Extraés información de texto de documentos de legajo de pacientes. "
                    "Tu salida DEBE ser JSON válido."
                )
            },
            {
                "role": "user",
                "content": (
                    f"Documento: {doc_name}\n\n"
                    "A partir del texto a continuación, extraé información y devolvé JSON con la misma estructura:\n"
                    "{\n"
                    '  "tipo_documento": "",\n'
                    '  "fecha_documento": "",\n'
                    '  "paciente": {"nombre": "", "dni": "", "cuil": ""},\n'
                    '  "prestador": {"nombre": "", "matricula": "", "institucion": ""},\n'
                    '  "diagnostico_texto": "",\n'
                    '  "medicacion": [{"nombre": "", "dosis": "", "frecuencia": "", "duracion": ""}],\n'
                    '  "estudios": [{"nombre": "", "hallazgos": ""}],\n'
                    '  "procedimientos": [{"nombre": "", "detalle": ""}],\n'
                    '  "cobertura": {"obra_social": "", "plan": "", "autorizacion": "", "vigencia": ""},\n'
                    '  "observaciones": "",\n'
                    '  "items_clave": [""]\n'
                    "}\n\n"
                    "Texto:\n"
                    "----------------\n"
                    f"{pdf_text}\n"
                    "----------------\n\n"
                    "No inventes datos."
                )
            }
        ],
        max_tokens=2500
    )
    return json.loads(resp.choices[0].message.content)


def gpt_generate_final_report(client: OpenAI, consolidated: dict) -> dict:
    """
    Devuelve JSON estructurado del informe final (para usar en HTML/PDF)
    """
    resp = client.chat.completions.create(
        model=MODEL_TEXT,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Sos un auditor médico experto. "
                    "Generás un informe formal, claro y defendible, basado SOLO en el legajo aportado."
                )
            },
            {
                "role": "user",
                "content": (
                    "Con la información consolidada del legajo (JSON), generá un informe formal con:\n"
                    "1) Resumen clínico\n"
                    "2) Diagnóstico presuntivo / problema principal (si se infiere)\n"
                    "3) Justificación (evidencia en el legajo)\n"
                    "4) Evaluación de cobertura (si hay datos)\n"
                    "5) Recomendación y próximos pasos\n"
                    "6) Red flags / inconsistencias\n"
                    "7) Pendientes de información (qué falta pedir)\n\n"
                    "Devolvé SOLO JSON con esta estructura:\n"
                    "{\n"
                    '  "resumen_clinico": "",\n'
                    '  "diagnostico_presuntivo": "",\n'
                    '  "justificacion": ["..."],\n'
                    '  "evaluacion_cobertura": ["..."],\n'
                    '  "recomendaciones": ["..."],\n'
                    '  "red_flags": ["..."],\n'
                    '  "pendientes": ["..."]\n'
                    "}\n\n"
                    "JSON del legajo:\n"
                    f"{json.dumps(consolidated, ensure_ascii=False)}"
                )
            }
        ],
        max_tokens=2500
    )
    return json.loads(resp.choices[0].message.content)


# =========================================================
# OUTPUT BUILDERS: HTML + PDF
# =========================================================
def build_html_report(job_id: str, folder_id: str, final_report: dict, consolidated: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def li(items: List[str]) -> str:
        if not items:
            return "<li>(sin datos)</li>"
        return "\n".join([f"<li>{escape_html(x)}</li>" for x in items])

    html = f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Informe Auditoría Médica IA</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #111; }}
    .header {{ border-bottom: 2px solid #ddd; padding-bottom: 12px; margin-bottom: 18px; }}
    .meta {{ color: #444; font-size: 12px; }}
    h1 {{ font-size: 22px; margin: 0; }}
    h2 {{ font-size: 16px; margin-top: 18px; border-left: 4px solid #999; padding-left: 8px; }}
    p {{ line-height: 1.45; }}
    ul {{ margin-top: 6px; }}
    .box {{ background: #f7f7f7; padding: 12px; border-radius: 8px; }}
    .small {{ font-size: 12px; color: #555; }}
    code {{ background: #eee; padding: 2px 6px; border-radius: 6px; }}
    .footer {{ margin-top: 24px; border-top: 1px solid #eee; padding-top: 10px; }}
    details {{ margin-top: 12px; }}
    pre {{ white-space: pre-wrap; word-wrap: break-word; background: #fafafa; padding: 10px; border: 1px solid #eee; border-radius: 8px; }}
  </style>
</head>
<body>
  <div class="header">
    <h1>Informe de Auditoría Médica Automatizada (IA)</h1>
    <div class="meta">
      Job: <code>{job_id}</code> — Folder: <code>{folder_id}</code> — Generado: {now}
    </div>
  </div>

  <h2>Resumen clínico</h2>
  <div class="box"><p>{escape_html(final_report.get("resumen_clinico",""))}</p></div>

  <h2>Diagnóstico presuntivo</h2>
  <p>{escape_html(final_report.get("diagnostico_presuntivo",""))}</p>

  <h2>Justificación</h2>
  <ul>
    {li(final_report.get("justificacion", []))}
  </ul>

  <h2>Evaluación de cobertura</h2>
  <ul>
    {li(final_report.get("evaluacion_cobertura", []))}
  </ul>

  <h2>Recomendaciones</h2>
  <ul>
    {li(final_report.get("recomendaciones", []))}
  </ul>

  <h2>Red flags / inconsistencias</h2>
  <ul>
    {li(final_report.get("red_flags", []))}
  </ul>

  <h2>Pendientes de información</h2>
  <ul>
    {li(final_report.get("pendientes", []))}
  </ul>

  <details>
    <summary class="small">Ver JSON consolidado (debug)</summary>
    <pre>{escape_html(json.dumps(consolidated, ensure_ascii=False, indent=2))}</pre>
  </details>

  <div class="footer small">
    Documento generado automáticamente. Validar clínicamente antes de decisiones prestacionales.
  </div>
</body>
</html>"""
    return html


def escape_html(s: str) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#039;")
    )


def build_pdf_report(path_pdf: str, title: str, final_report: dict, job_id: str, folder_id: str):
    c = canvas.Canvas(path_pdf, pagesize=A4)
    width, height = A4

    margin_x = 2 * cm
    y = height - 2 * cm

    def draw_h(text: str, size=14):
        nonlocal y
        c.setFont("Helvetica-Bold", size)
        c.drawString(margin_x, y, text)
        y -= 0.8 * cm

    def draw_p(text: str, size=10):
        nonlocal y
        c.setFont("Helvetica", size)
        lines = simpleSplit(text or "", "Helvetica", size, width - 2 * margin_x)
        for ln in lines:
            if y < 2 * cm:
                c.showPage()
                y = height - 2 * cm
                c.setFont("Helvetica", size)
            c.drawString(margin_x, y, ln)
            y -= 0.45 * cm
        y -= 0.25 * cm

    def draw_list(items: List[str], size=10):
        nonlocal y
        if not items:
            draw_p("- (sin datos)", size=size)
            return
        for it in items:
            draw_p(f"- {it}", size=size)

    draw_h(title, 16)
    c.setFont("Helvetica", 9)
    c.drawString(margin_x, y, f"Job: {job_id}   Folder: {folder_id}   Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    y -= 0.8 * cm

    draw_h("Resumen clínico", 12)
    draw_p(final_report.get("resumen_clinico", ""))

    draw_h("Diagnóstico presuntivo", 12)
    draw_p(final_report.get("diagnostico_presuntivo", ""))

    draw_h("Justificación", 12)
    draw_list(final_report.get("justificacion", []))

    draw_h("Evaluación de cobertura", 12)
    draw_list(final_report.get("evaluacion_cobertura", []))

    draw_h("Recomendaciones", 12)
    draw_list(final_report.get("recomendaciones", []))

    draw_h("Red flags / inconsistencias", 12)
    draw_list(final_report.get("red_flags", []))

    draw_h("Pendientes", 12)
    draw_list(final_report.get("pendientes", []))

    c.showPage()
    c.save()


# =========================================================
# CONSOLIDATION HELPERS
# =========================================================
def consolidate_documents(doc_results: List[dict]) -> dict:
    # Consolidación simple: juntamos listas y mantenemos fuente
    consolidated = {
        "documentos": doc_results
    }
    return consolidated


# =========================================================
# MAIN PIPELINE FOR A SINGLE PDF
# =========================================================
def process_single_pdf(
    client: OpenAI,
    pdf_path: str,
    pdf_name: str,
    work_dir: str,
) -> dict:
    extracted_text = extract_text_pdfplumber(pdf_path)
    has_text = pdf_has_meaningful_text(extracted_text)

    if has_text:
        extracted_json = gpt_text_extract_json(client, extracted_text, pdf_name)
        return {
            "archivo": pdf_name,
            "tipo_procesamiento": "texto_digital",
            "texto_extraido_chars": len(extracted_text),
            "resultado_json": extracted_json,
        }

    # No hay texto útil: convertimos a imágenes + Vision
    img_dir = os.path.join(work_dir, "img")
    base_name = os.path.splitext(pdf_name)[0]
    image_paths = pdf_to_images(pdf_path, img_dir, base_name)

    pages_json = []
    for idx, img_path in enumerate(image_paths, start=1):
        # pacing suave
        time.sleep(SLEEP_BETWEEN_VISION_CALLS_SEC)
        page_json = gpt_vision_extract_json(client, img_path)
        pages_json.append({"pagina": idx, "imagen": os.path.basename(img_path), "resultado_json": page_json})

    return {
        "archivo": pdf_name,
        "tipo_procesamiento": "vision_ocr",
        "paginas": len(image_paths),
        "resultado_paginas": pages_json,
    }


# =========================================================
# BACKGROUND JOB
# =========================================================
def procesar_drive_job(job_id: str, folder_id: str):
    jobs[job_id]["status"] = "procesando"
    jobs[job_id]["started_at"] = datetime.now().isoformat()

    try:
        # 1) Conectar Drive
        service = conectar_drive()

        # 2) Listar PDFs
        pdfs = drive_list_pdfs(service, folder_id)
        jobs[job_id]["documentos_encontrados"] = len(pdfs)
        jobs[job_id]["archivos"] = [p["name"] for p in pdfs]

        # 3) Crear subcarpeta de salida
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_folder_name = f"AUDITORIA_{stamp}"
        out_folder = drive_create_subfolder(service, folder_id, out_folder_name)
        out_folder_id = out_folder["id"]
        jobs[job_id]["output_folder_id"] = out_folder_id
        jobs[job_id]["output_folder_name"] = out_folder_name

        # 4) Crear workspace temporal
        with tempfile.TemporaryDirectory() as tmpdir:
            client = get_openai_client()

            doc_results = []

            for i, f in enumerate(pdfs, start=1):
                pdf_id = f["id"]
                pdf_name = f["name"]

                jobs[job_id]["status_detalle"] = f"Descargando {i}/{len(pdfs)}: {pdf_name}"

                local_pdf_path = os.path.join(tmpdir, pdf_name)
                drive_download_file_to_path(service, pdf_id, local_pdf_path)

                jobs[job_id]["status_detalle"] = f"Procesando {i}/{len(pdfs)}: {pdf_name}"

                result = process_single_pdf(
                    client=client,
                    pdf_path=local_pdf_path,
                    pdf_name=pdf_name,
                    work_dir=tmpdir,
                )
                doc_results.append(result)

            # 5) Consolidar
            consolidated = consolidate_documents(doc_results)

            # 6) Informe final (JSON estructurado del informe)
            jobs[job_id]["status_detalle"] = "Generando informe final (GPT)"
            final_report = gpt_generate_final_report(client, consolidated)

            # 7) Armar archivos: JSON + HTML + PDF
            resultado_legajo = {
                "job_id": job_id,
                "folder_id": folder_id,
                "generado": datetime.now().isoformat(),
                "documentos_procesados": len(doc_results),
                "documentos": doc_results,
                "informe_struct": final_report,
            }
            json_bytes = json.dumps(resultado_legajo, ensure_ascii=False, indent=2).encode("utf-8")

            html_str = build_html_report(job_id, folder_id, final_report, consolidated)
            html_bytes = html_str.encode("utf-8")

            pdf_path = os.path.join(tmpdir, "informe.pdf")
            build_pdf_report(
                path_pdf=pdf_path,
                title="Informe de Auditoría Médica Automatizada (IA)",
                final_report=final_report,
                job_id=job_id,
                folder_id=folder_id,
            )

            # 8) Subir a Drive (misma carpeta original, dentro de subcarpeta AUDITORIA_xxx)
            jobs[job_id]["status_detalle"] = "Subiendo resultados a Drive"

            up_json = drive_upload_bytes(service, out_folder_id, "resultado_legajo.json", "application/json", json_bytes)
            up_html = drive_upload_bytes(service, out_folder_id, "informe.html", "text/html", html_bytes)
            up_pdf = drive_upload_path(service, out_folder_id, "informe.pdf", "application/pdf", pdf_path)

            # 9) Guardar referencias
            jobs[job_id]["status"] = "finalizado"
            jobs[job_id]["finished_at"] = datetime.now().isoformat()
            jobs[job_id]["documentos_procesados"] = len(doc_results)

            jobs[job_id]["outputs"] = {
                "json": {"id": up_json["id"], "name": up_json["name"], "url": up_json.get("webViewLink")},
                "html": {"id": up_html["id"], "name": up_html["name"], "url": up_html.get("webViewLink")},
                "pdf": {"id": up_pdf["id"], "name": up_pdf["name"], "url": up_pdf.get("webViewLink")},
            }

            # (opcional) también devolvemos un resumen corto
            jobs[job_id]["resumen"] = final_report.get("resumen_clinico", "")

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["finished_at"] = datetime.now().isoformat()


# =========================================================
# ENDPOINTS
# =========================================================
@app.post("/procesar")
def iniciar_proceso(request: DriveRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "en_cola",
        "folder_id": request.folder_id,
    }

    background_tasks.add_task(procesar_drive_job, job_id, request.folder_id)

    return {"status": "en_proceso", "job_id": job_id}


@app.get("/estado/{job_id}")
def consultar_estado(job_id: str):
    if job_id not in jobs:
        return {"error": "Job no encontrado"}
    return jobs[job_id]


@app.get("/health")
def health():
    return {"ok": True}
