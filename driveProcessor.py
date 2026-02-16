import os
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
SERVICE_ACCOUNT_FILE = 'drive_credentials.json'

def conectar_drive():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
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
