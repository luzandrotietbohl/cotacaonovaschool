"""Envio das respostas por SMTP, em vez da Gmail API.

Motivo: enviar automaticamente por um app OAuth nao verificado com escopo
restrito de Gmail atrai o antiabuso do Google — foi o que derrubou o primeiro
OAuth client deste projeto. SMTP com senha de app e um canal separado, estavel,
e permite remover o escopo gmail.send.

A leitura da caixa e os labels continuam pela API (escopo gmail.modify), assim
como os rascunhos: em MODO_RESPOSTA=rascunho nada sai pela rede.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from cotador.core.modelos import Email
from cotador.integracoes import mime

log = logging.getLogger(__name__)


class ConfiguracaoSMTPAusente(RuntimeError):
    """Falta host/usuario/senha para enviar."""


class EnviadorSMTP:
    def __init__(
        self,
        host: str,
        porta: int,
        usuario: str,
        senha: str,
        remetente_exibido: str = "",
    ) -> None:
        faltando = [
            nome
            for nome, valor in (
                ("SMTP_HOST", host),
                ("SMTP_USUARIO", usuario),
                ("SMTP_SENHA_APP", senha),
            )
            if not valor
        ]
        if faltando:
            raise ConfiguracaoSMTPAusente(
                f"Faltam no .env: {', '.join(faltando)}. "
                "A senha de app do Gmail e gerada em "
                "https://myaccount.google.com/apppasswords (exige verificacao "
                "em duas etapas ativa) e tem 16 caracteres."
            )
        self.host = host
        self.porta = porta
        self.usuario = usuario
        self._senha = senha
        self.remetente_exibido = remetente_exibido or usuario

    def montar(self, original: Email, texto: str) -> EmailMessage:
        """Resposta com os cabecalhos que fazem o Gmail agrupar na thread."""
        msg = EmailMessage()
        msg["To"] = original.remetente
        msg["From"] = self.remetente_exibido
        assunto = original.assunto or "Cotacao de frete"
        msg["Subject"] = assunto if assunto.lower().startswith("re:") else f"Re: {assunto}"
        msg_id = mime.desdobrar(original.message_id_header)
        if msg_id:
            # Sem estes dois cabecalhos a resposta vira uma conversa nova na
            # caixa do cliente, e a mesclagem por thread perde o encadeamento.
            # desdobrar() e obrigatorio: References longo chega quebrado em
            # varias linhas e o email.message recusa gravar assim.
            msg["In-Reply-To"] = msg_id
            refs = mime.desdobrar(original.references_header) or ""
            msg["References"] = f"{refs} {msg_id}".strip()
        msg.set_content(texto)
        return msg

    def responder(self, original: Email, texto: str) -> None:
        msg = self.montar(original, texto)
        contexto = ssl.create_default_context()
        with smtplib.SMTP_SSL(self.host, self.porta, context=contexto, timeout=30) as s:
            s.login(self.usuario, self._senha)
            s.send_message(msg)
        log.info("Resposta enviada por SMTP para %s", original.remetente)

    def testar_conexao(self) -> None:
        """Valida host/porta/credencial sem enviar nada."""
        contexto = ssl.create_default_context()
        with smtplib.SMTP_SSL(self.host, self.porta, context=contexto, timeout=30) as s:
            s.login(self.usuario, self._senha)
        log.info("SMTP OK: %s@%s:%d", self.usuario, self.host, self.porta)
