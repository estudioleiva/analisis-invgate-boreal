import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

def conectar_drive():
    # Leer variable de entorno cargada en Railway
    credentials_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")

    if not credentials_json:
        raise Exception("No se encontró la variable GOOGLE_CREDENTIALS_JSON")

    credentials_info = json.loads(credentials_json)

    creds = service_account.Credentials.from_service_account_info(
        credentials_info,
        scopes=SCOPES
    )

    service = build('drive', 'v3', credentials=creds)
    return service


def listar_pdfs(service, folder_id):
    query = f"'{folder_id}' in parents and mimeType='application/pdf'"
    results = service.files().list(q=query).execute()
    return results.get('files', [])


def main():
    folder_id = input("Pegá el ID de la carpeta Drive: ").strip()

    service = conectar_drive()

    print("\n🔎 Buscando PDFs...")
    archivos = listar_pdfs(service, folder_id)

    print(f"\n📂 Encontrados {len(archivos)} PDFs\n")

    for archivo in archivos:
        print(" -", archivo['name'])


if __name__ == "__main__":
    main()
