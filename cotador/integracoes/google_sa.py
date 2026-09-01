"""Credencial de conta de servico para ler a planilha.

Conta de servico nao passa por tela de consentimento, nao expira em 7 dias e
nao exige verificacao do Google — diferente do OAuth de usuario, que era o
gargalo operacional deste projeto.

Requisito: a planilha precisa estar compartilhada (leitor) com o email da
conta de servico, que aparece no JSON como "client_email".
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from google.oauth2.service_account import Credentials

log = logging.getLogger(__name__)

ESCOPOS = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class ContaDeServicoAusente(RuntimeError):
    """Falta o arquivo de chave da conta de servico."""


def email_da_conta(caminho: Path) -> str:
    return json.loads(caminho.read_text(encoding="utf-8")).get("client_email", "")


def credenciais(caminho: Path) -> Credentials:
    if not caminho.exists():
        raise ContaDeServicoAusente(
            f"{caminho.name} nao encontrado. No Google Cloud Console crie uma "
            "conta de servico, gere uma chave JSON, salve com esse nome na raiz "
            "do projeto e compartilhe a planilha com o email da conta."
        )
    cred = Credentials.from_service_account_file(str(caminho), scopes=ESCOPOS)
    log.debug("Conta de servico: %s", cred.service_account_email)
    return cred
