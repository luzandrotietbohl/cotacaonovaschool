"""Configuracao central, carregada de variaveis de ambiente (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")


def _req(chave: str) -> str:
    valor = os.getenv(chave, "").strip()
    if not valor:
        raise RuntimeError(f"Variavel de ambiente obrigatoria ausente: {chave}")
    return valor


def _bool(chave: str, padrao: bool) -> bool:
    valor = os.getenv(chave, "").strip().lower()
    if not valor:
        return padrao
    return valor in ("1", "true", "sim", "s", "yes")


@dataclass(frozen=True)
class Config:
    # Vazia e permitido: so os comandos que usam o LLM cobram a chave
    # (ver Extrator). Assim --validar-planilha e --cotar funcionam sem ela.
    anthropic_api_key: str
    anthropic_model: str
    # Obrigatorio apenas para chaves identity-linked (a API devolve 400 sem ele).
    anthropic_workspace_id: str
    gmail_user: str
    gmail_query: str
    sheet_id: str
    sheet_aba: str
    modo_resposta: str
    intervalo_segundos: int
    exigir_peso: bool
    # False deixa a curadoria da tabela em modo relatorio: audita e registra,
    # mas nao retira nenhuma linha de circulacao.
    auditoria_bloqueia: bool
    # Envio por SMTP: mantem o gmail.send fora dos escopos OAuth.
    smtp_host: str
    smtp_porta: int
    smtp_usuario: str
    smtp_senha: str
    smtp_remetente: str
    imap_host: str
    imap_porta: int

    service_account_json: Path = RAIZ / "service_account.json"
    banco: Path = RAIZ / "cotador" / "dados" / "cotador.sqlite3"

    @property
    def remetente(self) -> str:
        """Como o cliente ve o campo De."""
        return self.smtp_remetente or self.smtp_usuario or self.gmail_user

    LABEL_PROCESSADO = "cotador-processado"
    LABEL_INCOMPLETO = "cotador-aguardando-dados"
    LABEL_SEM_ROTA = "cotador-sem-rota"
    LABEL_REVISAR = "cotador-revisar"

    @classmethod
    def carregar(cls) -> "Config":
        return cls(
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5"),
            anthropic_workspace_id=os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip(),
            gmail_user=_req("GMAIL_USER"),
            gmail_query=os.getenv("GMAIL_QUERY", "is:unread -label:cotador-processado"),
            sheet_id=_req("SHEET_ID"),
            sheet_aba=os.getenv("SHEET_ABA", "TABELA_ROTAS"),
            modo_resposta=os.getenv("MODO_RESPOSTA", "rascunho"),
            intervalo_segundos=int(os.getenv("INTERVALO_SEGUNDOS", "120")),
            exigir_peso=_bool("EXIGIR_PESO", True),
            auditoria_bloqueia=_bool("AUDITORIA_BLOQUEIA", True),
            smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com").strip(),
            smtp_porta=int(os.getenv("SMTP_PORTA", "465")),
            smtp_usuario=os.getenv("SMTP_USUARIO", "").strip(),
            smtp_senha=os.getenv("SMTP_SENHA_APP", "").replace(" ", ""),
            smtp_remetente=os.getenv("SMTP_REMETENTE", "").strip(),
            imap_host=os.getenv("IMAP_HOST", "imap.gmail.com").strip(),
            imap_porta=int(os.getenv("IMAP_PORTA", "993")),
        )
