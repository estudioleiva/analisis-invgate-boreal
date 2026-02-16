import os
import sys
import json
import base64
import time
import pdfplumber
import tkinter as tk
from tkinter import filedialog
from pdf2image import convert_from_path
from openai import OpenAI
from dotenv import load_dotenv

# =============================
# CONFIGURACIÓN
# =============================

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    print("❌ No se encontró OPENAI_API_KEY en el archivo .env")
    sys.exit()

client = OpenAI(api_key=api_key)

DPI = 200
JPEG_QUALITY = 80


# =============================
# UI
# =============================

def seleccionar_carpeta():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    carpeta = filedialog.askdirectory(title="Seleccionar carpeta con PDFs")
    root.destroy()
    return carpeta


# =============================
# TEXTO DIGITAL
# =============================

def pdf_tiene_texto(ruta_pdf):
    with pdfplumber.open(ruta_pdf) as pdf:
        for page in pdf.pages:
            texto = page.extract_text()
            if texto and texto.strip():
                return True
    return False


def extraer_texto_pdf(ruta_pdf):
    texto_total = ""
    with pdfplumber.open(ruta_pdf) as pdf:
        for page in pdf.pages:
            texto = page.extract_text()
            if texto:
                texto_total += texto + "\n"
    return texto_total


# =============================
# GPT VISION
# =============================

def imagen_a_base64(ruta_imagen):
    with open(ruta_imagen, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def analizar_imagen_con_gpt(ruta_imagen):
    imagen_base64 = imagen_a_base64(ruta_imagen)

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": (
                    "Sos auditor médico. Extraé información estructurada del documento. "
                    "Identificá: tipo_documento, fecha, paciente, diagnosticos, medicacion, "
                    "estudios, indicaciones, firmas, observaciones."
                )
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Analizar documento y devolver JSON estructurado."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{imagen_base64}"
                        }
                    }
                ]
            }
        ],
        temperature=0
    )

    return response.choices[0].message.content


# =============================
# PDF → IMÁGENES
# =============================

def convertir_pdf_a_imagenes(ruta_pdf, carpeta_img):
    nombre_base = os.path.splitext(os.path.basename(ruta_pdf))[0]
    paginas = convert_from_path(ruta_pdf, dpi=DPI)
    rutas_generadas = []

    for i, pagina in enumerate(paginas, start=1):
        nombre_imagen = f"{nombre_base}_{str(i).zfill(5)}.jpg"
        ruta_imagen = os.path.join(carpeta_img, nombre_imagen)

        # 🔎 Si ya existe, no la regeneramos
        if os.path.exists(ruta_imagen):
            print(f"         ↳ Imagen ya existe, se reutiliza: {nombre_imagen}")
        else:
            pagina.save(ruta_imagen, "JPEG", quality=JPEG_QUALITY)
            print(f"         ↳ Imagen generada: {nombre_imagen}")

        rutas_generadas.append(ruta_imagen)

    return rutas_generadas




# =============================
# PROCESAMIENTO PDF
# =============================

def procesar_pdf(ruta_pdf, carpeta_img):
    nombre_pdf = os.path.basename(ruta_pdf)
    print(f"\n📄 Procesando: {nombre_pdf}")
    inicio = time.time()

    if pdf_tiene_texto(ruta_pdf):
        print("   → Texto digital detectado.")
        texto = extraer_texto_pdf(ruta_pdf)

        duracion = round(time.time() - inicio, 2)
        print(f"   ✔ Finalizado en {duracion}s")

        return {
            "archivo": nombre_pdf,
            "tipo_procesamiento": "texto_digital",
            "duracion_segundos": duracion,
            "contenido": texto
        }

    else:
        print("   → No tiene texto digital. Usando GPT Vision.")
        rutas_imagenes = convertir_pdf_a_imagenes(ruta_pdf, carpeta_img)

        resultados_paginas = []

        for idx, ruta_img in enumerate(rutas_imagenes, start=1):
            print(f"      ↳ Enviando página {idx}/{len(rutas_imagenes)}...")
            try:
                resultado = analizar_imagen_con_gpt(ruta_img)

                resultados_paginas.append({
                    "imagen": os.path.basename(ruta_img),
                    "resultado_gpt": resultado
                })

            except Exception as e:
                print(f"      ❌ Error en página {idx}: {e}")
                resultados_paginas.append({
                    "imagen": os.path.basename(ruta_img),
                    "error": str(e)
                })

        duracion = round(time.time() - inicio, 2)
        print(f"   ✔ Finalizado en {duracion}s")

        return {
            "archivo": nombre_pdf,
            "tipo_procesamiento": "vision_ocr",
            "duracion_segundos": duracion,
            "contenido": resultados_paginas
        }


# =============================
# MAIN
# =============================

def main():
    carpeta = seleccionar_carpeta()

    if not carpeta:
        print("No se seleccionó carpeta.")
        sys.exit()

    carpeta_img = os.path.join(carpeta, "img")
    os.makedirs(carpeta_img, exist_ok=True)

    resultados_totales = []

    for archivo in os.listdir(carpeta):
        if archivo.lower().endswith(".pdf"):
            ruta_pdf = os.path.join(carpeta, archivo)
            resultado = procesar_pdf(ruta_pdf, carpeta_img)
            resultados_totales.append(resultado)

    informe_final = {
        "cantidad_documentos": len(resultados_totales),
        "documentos": resultados_totales
    }

    ruta_salida = os.path.join(carpeta, "resultado_legajo.json")

    with open(ruta_salida, "w", encoding="utf-8") as f:
        json.dump(informe_final, f, indent=4, ensure_ascii=False)

    print("\n🏁 PROCESO COMPLETADO")
    print(f"📂 Resultado guardado en: {ruta_salida}")


if __name__ == "__main__":
    main()
