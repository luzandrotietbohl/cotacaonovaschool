"""Leitura da caixa por IMAP, com a mesma senha de app usada no SMTP.

Substitui a Gmail API: sem OAuth client, sem token.json, sem expiracao de 7
dias, sem processo de verificacao do Google.

Usa as extensoes IMAP do Gmail, que preservam o que a API oferecia:
  X-GM-RAW    busca com a sintaxe do Gmail ('is:unread -label:...')
  X-GM-MSGID  id estavel da mensagem, usado como chave de idempotencia
  X-GM-THRID  id da conversa, usado para mesclar respostas da mesma thread
  X-GM-LABELS aplicar labels
"""
from __future__ import annotations

import imaplib
import logging
import re
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import parseaddr

from cotador.core.modelos import Email
from cotador.integracoes import mime

log = logging.getLogger(__name__)

# Mensagens grandes com anexos estouram o limite padrao do imaplib.
imaplib._MAXLINE = max(imaplib._MAXLINE, 10_000_000)


class CredencialInvalida(RuntimeError):
    """Usuario ou senha de app recusados. So um humano resolve."""


def _numero(padrao: str, texto: str) -> str | None:
    achado = re.search(padrao + r"\s+(\d+)", texto)
    return achado.group(1) if achado else None


class CaixaIMAP:
    def __init__(self, host: str, porta: int, usuario: str, senha: str) -> None:
        if not (usuario and senha):
            raise CredencialInvalida(
                "SMTP_USUARIO e SMTP_SENHA_APP sao obrigatorios (a mesma senha "
                "de app serve para IMAP e SMTP)."
            )
        self.host = host
        self.porta = porta
        self.usuario = usuario
        self._senha = senha
        self._con: imaplib.IMAP4_SSL | None = None
        self._pasta_rascunhos: str | None = None

    # ---------------- conexao ----------------
    def __enter__(self) -> "CaixaIMAP":
        self.conectar()
        return self

    def __exit__(self, *_) -> None:
        self.desconectar()

    def conectar(self) -> None:
        if self._con is not None:
            return
        con = imaplib.IMAP4_SSL(self.host, self.porta, timeout=30)
        try:
            con.login(self.usuario, self._senha)
        except imaplib.IMAP4.error as exc:
            raise CredencialInvalida(
                f"O Gmail recusou o login IMAP de {self.usuario} ({exc}). "
                "Confira SMTP_SENHA_APP no .env — precisa ser uma senha de app "
                "(16 caracteres), nao a senha da conta."
            ) from exc
        con.select("INBOX")
        self._con = con
        log.debug("IMAP conectado como %s", self.usuario)

    def desconectar(self) -> None:
        if self._con is None:
            return
        try:
            self._con.close()
            self._con.logout()
        except Exception:  # desconectar nunca deve derrubar o ciclo
            log.debug("Falha ao encerrar IMAP, ignorando", exc_info=True)
        self._con = None

    @property
    def con(self) -> imaplib.IMAP4_SSL:
        if self._con is None:
            self.conectar()
        assert self._con is not None
        return self._con

    def testar_conexao(self) -> None:
        self.conectar()
        self.desconectar()
        log.info("IMAP OK: %s@%s:%d", self.usuario, self.host, self.porta)

    # ---------------- leitura ----------------
    def buscar(self, query: str, limite: int = 25) -> list[str]:
        """Busca com a sintaxe do Gmail e devolve UIDs (mais recentes primeiro)."""
        ok, dados = self.con.uid("SEARCH", "X-GM-RAW", f'"{query}"')
        if ok != "OK":
            raise RuntimeError(f"Busca IMAP falhou: {dados}")
        uids = (dados[0] or b"").split()
        return [u.decode() for u in uids[-limite:]][::-1]

    def ler(self, uid: str) -> Email:
        ok, dados = self.con.uid(
            "FETCH", uid, "(X-GM-MSGID X-GM-THRID BODY.PEEK[])"
        )
        if ok != "OK" or not dados or dados[0] is None:
            raise RuntimeError(f"Nao consegui ler o UID {uid}: {dados}")

        cabecalho_imap = dados[0][0].decode(errors="replace")
        bruto = dados[0][1]
        msg = message_from_bytes(bruto)

        gm_msgid = _numero("X-GM-MSGID", cabecalho_imap) or uid
        gm_thrid = _numero("X-GM-THRID", cabecalho_imap) or gm_msgid

        nome, endereco = parseaddr(mime.decodificar_cabecalho(msg.get("From")))
        return Email(
            # X-GM-MSGID e estavel entre pastas e reinicios; o UID nao e.
            id=gm_msgid,
            thread_id=gm_thrid,
            remetente=endereco,
            nome_remetente=nome or (endereco.split("@")[0] if endereco else ""),
            assunto=mime.decodificar_cabecalho(msg.get("Subject")),
            corpo=mime.sem_historico(mime.corpo(msg)),
            message_id_header=mime.desdobrar(msg.get("Message-ID")),
            references_header=mime.desdobrar(msg.get("References")),
            uid=uid,
        )

    # ---------------- escrita ----------------
    def aplicar_labels(
        self, uid: str, adicionar: list[str], remover_unread: bool = True
    ) -> None:
        for nome in adicionar:
            ok, resp = self.con.uid("STORE", uid, "+X-GM-LABELS", f'("{nome}")')
            if ok != "OK":
                log.warning("Nao consegui aplicar o label %s: %s", nome, resp)
        if remover_unread:
            self.con.uid("STORE", uid, "+FLAGS", r"(\Seen)")

    def devolver_para_fila(self, id_email: str, labels_remover: list[str]) -> bool:
        """Desfaz o fechamento: remove labels e restaura como nao lida.

        Localiza pelo X-GM-MSGID porque o UID gravado em outra sessao ja nao
        vale. Com o email de volta a 'is:unread' sem os labels, o proximo
        ciclo o reprocessa.
        """
        ok, dados = self.con.uid("SEARCH", "X-GM-MSGID", id_email)
        uids = (dados[0] or b"").split() if ok == "OK" else []
        if not uids:
            log.warning("Email %s nao encontrado na caixa para devolver", id_email)
            return False
        uid = uids[-1].decode()
        for nome in labels_remover:
            self.con.uid("STORE", uid, "-X-GM-LABELS", f'("{nome}")')
        self.con.uid("STORE", uid, "-FLAGS", r"(\Seen)")
        return True

    def pasta_rascunhos(self) -> str:
        """Descobre a pasta de rascunhos pelo atributo \\Drafts.

        O nome e localizado ('[Gmail]/Rascunhos', '[Gmail]/Drafts'), entao
        procuramos pelo atributo em vez de chutar o texto.
        """
        if self._pasta_rascunhos:
            return self._pasta_rascunhos
        ok, linhas = self.con.list()
        if ok == "OK":
            for linha in linhas or []:
                texto = linha.decode(errors="replace")
                if "\\Drafts" in texto:
                    self._pasta_rascunhos = texto.split(' "/" ')[-1].strip().strip('"')
                    break
        if not self._pasta_rascunhos:
            self._pasta_rascunhos = "[Gmail]/Drafts"
            log.warning("Pasta de rascunhos nao encontrada; usando o padrao")
        return self._pasta_rascunhos

    def criar_rascunho(self, original: Email, texto: str, remetente: str) -> None:
        """Grava o rascunho na conversa, sem enviar nada."""
        msg = EmailMessage()
        msg["To"] = original.remetente
        msg["From"] = remetente
        assunto = original.assunto or "Cotacao de frete"
        msg["Subject"] = assunto if assunto.lower().startswith("re:") else f"Re: {assunto}"
        if original.message_id_header:
            msg["In-Reply-To"] = original.message_id_header
            refs = original.references_header or ""
            msg["References"] = f"{refs} {original.message_id_header}".strip()
        msg.set_content(texto)

        pasta = self.pasta_rascunhos()
        ok, resp = self.con.append(pasta, r"(\Draft)", None, msg.as_bytes())
        if ok != "OK":
            raise RuntimeError(f"Nao consegui gravar o rascunho em {pasta}: {resp}")
        log.info("Rascunho gravado em %s para %s", pasta, original.remetente)
