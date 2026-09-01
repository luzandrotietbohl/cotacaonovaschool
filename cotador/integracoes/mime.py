"""Decodificacao de email cru (RFC 822), compartilhada pelo IMAP.

Estava embutido no cliente da Gmail API; virou modulo proprio quando a leitura
passou para IMAP, que entrega bytes em vez do payload em dicionario.
"""
from __future__ import annotations

import html
import re
from email.header import decode_header
from email.message import Message


def decodificar_cabecalho(valor: str | None) -> str:
    """'=?UTF-8?Q?Cota=C3=A7=C3=A3o?=' -> 'Cotação'."""
    if not valor:
        return ""
    partes = []
    for texto, charset in decode_header(valor):
        if isinstance(texto, bytes):
            partes.append(texto.decode(charset or "utf-8", errors="replace"))
        else:
            partes.append(texto)
    return "".join(partes)


def desdobrar(valor: str | None) -> str | None:
    """Junta cabecalho quebrado em varias linhas numa linha so.

    A RFC 5322 permite dobrar cabecalhos longos (References com muitos
    Message-IDs quase sempre vem dobrado). O email.message recusa gravar um
    valor com quebra de linha, entao normalizamos ao ler e ao escrever.
    """
    if valor is None:
        return None
    return re.sub(r"\s+", " ", valor).strip()


def html_para_texto(bruto: str) -> str:
    sem_script = re.sub(r"(?is)<(script|style).*?</\1>", " ", bruto)
    com_quebras = re.sub(r"(?i)<(br|/p|/div|/tr)[^>]*>", "\n", sem_script)
    texto = re.sub(r"(?s)<[^>]+>", " ", com_quebras)
    texto = html.unescape(texto)
    return re.sub(r"[ \t]{2,}", " ", texto).strip()


def corpo(msg: Message) -> str:
    """Texto do email, preferindo text/plain e ignorando anexos."""
    planos: list[str] = []
    htmls: list[str] = []

    for parte in msg.walk():
        if parte.get_content_maintype() == "multipart":
            continue
        if parte.get_filename():  # anexo, nao e corpo
            continue
        tipo = parte.get_content_type()
        if tipo not in ("text/plain", "text/html"):
            continue
        carga = parte.get_payload(decode=True)
        if not carga:
            continue
        texto = carga.decode(parte.get_content_charset() or "utf-8", errors="replace")
        (planos if tipo == "text/plain" else htmls).append(texto)

    if planos:
        return "\n".join(planos).strip()
    return html_para_texto("\n".join(htmls)) if htmls else ""


# Marcadores de inicio de thread citada — cortamos aqui para o LLM ler so a
# mensagem nova, senao ele extrai dados de cotacoes antigas da mesma conversa.
_INICIO_CITACAO = [
    r"^\s*Em .{0,80}escreveu:\s*$",
    r"^\s*On .{0,80}wrote:\s*$",
    r"^\s*-{2,}\s*(Mensagem original|Original Message|Forwarded message)",
    r"^\s*De:\s.+$",
    r"^\s*From:\s.+$",
]


def sem_historico(texto: str) -> str:
    linhas = texto.splitlines()
    for i, linha in enumerate(linhas):
        if any(re.match(p, linha, re.IGNORECASE) for p in _INICIO_CITACAO):
            return "\n".join(linhas[:i]).strip()
    return "\n".join(l for l in linhas if not l.lstrip().startswith(">")).strip()
