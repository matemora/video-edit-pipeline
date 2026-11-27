"""Gera o token OAuth que o ingest usa para enviar o vídeo final ao Drive.

Service account não tem cota no Drive de um Gmail pessoal, então o upload
precisa ser feito em nome do dono da pasta de saída. Rode este script UMA vez,
na sua máquina (não no Cloud Run):

    pip install google-auth-oauthlib
    python authorize_drive.py client_secret.json

Ele abre o navegador, você autoriza com a conta dona da pasta de saída, e o
token é salvo em drive_token.json. Depois:

    gcloud secrets create DRIVE_OAUTH_TOKEN --data-file=drive_token.json
    rm drive_token.json

O `client_secret.json` vem de um "ID do cliente OAuth" do tipo "App para
computador" criado no console (APIs e serviços → Credenciais). Os dois
arquivos dão acesso ao seu Drive: não commite nenhum deles.
"""

import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]
OUTPUT_PATH = "drive_token.json"


def main():
    if len(sys.argv) != 2:
        sys.exit("Uso: python authorize_drive.py client_secret.json")

    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
    # prompt=consent garante que o Google devolva um refresh_token.
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not credentials.refresh_token:
        sys.exit("O Google não devolveu refresh_token. Rode de novo.")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as file:
        json.dump(json.loads(credentials.to_json()), file)

    print(f"Token salvo em {OUTPUT_PATH}.")
    print("Agora: gcloud secrets create DRIVE_OAUTH_TOKEN --data-file=drive_token.json")


if __name__ == "__main__":
    main()
