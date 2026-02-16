import os
import json
import tkinter as tk
from tkinter import filedialog
from openai import OpenAI
from dotenv import load_dotenv

# =============================
# CONFIGURACIÓN
# =============================

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    print("❌ No se encontró OPENAI_API_KEY en .env")
    exit()

client = OpenAI(api_key=api_key)


# =============================
# UI
# =============================

def seleccionar_carpeta():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    carpeta = filedialog.askdirectory(title="Seleccionar carpeta del legajo")
    root.destroy()
    return carpeta


# =============================
# CARGAR JSON
# =============================

def cargar_legajo(ruta_json):
    with open(ruta_json, "r", encoding="utf-8") as f:
        return json.load(f)


# =============================
# GENERAR RESUMEN CLÍNICO
# =============================

def generar_resumen_clinico(data_legajo):

    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "Sos un médico auditor. Analizá integralmente el legajo clínico "
                    "y generá un resumen clínico estructurado, claro y profesional."
                )
            },
            {
                "role": "user",
                "content": (
                    "A continuación se envía el contenido estructurado del legajo.\n\n"
                    "Generar:\n"
                    "- Identificación del paciente\n"
                    "- Diagnósticos consolidados\n"
                    "- Medicación consolidada\n"
                    "- Estudios relevantes\n"
                    "- Línea cronológica resumida\n"
                    "- Alertas clínicas\n"
                    "- Observaciones relevantes\n\n"
                    f"LEGAJO:\n{json.dumps(data_legajo, ensure_ascii=False)}"
                )
            }
        ]
    )

    return response.choices[0].message.content


# =============================
# MAIN
# =============================

def main():

    carpeta = seleccionar_carpeta()

    if not carpeta:
        print("❌ No se seleccionó carpeta.")
        return

    ruta_json = os.path.join(carpeta, "resultado_legajo.json")

    if not os.path.exists(ruta_json):
        print("❌ No se encontró resultado_legajo.json en la carpeta seleccionada.")
        return

    print("\n📂 Cargando legajo...")
    data_legajo = cargar_legajo(ruta_json)

    print("🧠 Generando resumen clínico automático...")
    resumen = generar_resumen_clinico(data_legajo)

    ruta_salida = os.path.join(carpeta, "resumen_clinico.txt")

    with open(ruta_salida, "w", encoding="utf-8") as f:
        f.write(resumen)

    print("\n🏁 Resumen clínico generado en:")
    print(ruta_salida)


if __name__ == "__main__":
    main()
