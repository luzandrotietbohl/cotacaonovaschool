"""Orquestracao: le a caixa, decide o desfecho de cada email e responde."""
from __future__ import annotations

import dataclasses
import logging

from cotador.config import Config
from cotador.core import mensagens, precificacao
from cotador.core.extracao import Extrator
from cotador.core.modelos import Desfecho, Email, PedidoCotacao
from cotador.integracoes.banco import Banco
from cotador.integracoes.caixa_imap import CaixaIMAP, CredencialInvalida
from cotador.integracoes.email_smtp import EnviadorSMTP
from cotador.integracoes.planilha import TabelaTarifas

log = logging.getLogger(__name__)

# Dois cortes, porque os erros custam diferente:
# - mandar um preco errado e caro, entao cotar exige confianca alta;
# - pedir dados a um falso positivo e inofensivo, entao basta um corte baixo.
# Um pedido vago ("quanto fica um frete de SP para Vitoria?") cai naturalmente
# em confianca ~0.55: e cotacao de verdade, so incompleta.
CONFIANCA_PARA_COTAR = 0.6
CONFIANCA_PARA_PEDIR_DADOS = 0.35

# Remetentes que nunca devem receber resposta automatica (evita loop de robos).
REMETENTES_IGNORADOS = ("noreply", "no-reply", "nao-responda", "mailer-daemon", "postmaster")


class Agente:
    def __init__(
        self,
        cfg: Config,
        caixa: CaixaIMAP,
        tarifas: TabelaTarifas,
        extrator: Extrator,
        banco: Banco,
        enviador: EnviadorSMTP | None = None,
    ) -> None:
        self.cfg = cfg
        self.caixa = caixa
        self.tarifas = tarifas
        self.extrator = extrator
        self.banco = banco
        # So e exigido em MODO_RESPOSTA=enviar; em rascunho fica None.
        self.enviador = enviador

    # ---------------- loop ----------------
    def rodar_ciclo(self) -> dict[str, int]:
        # A conexao IMAP e por ciclo: aberta, usada e fechada. Deixar aberta
        # entre ciclos de 2 minutos so acumula sessao ociosa no servidor.
        try:
            with self.caixa:
                return self._ciclo()
        finally:
            self.caixa.desconectar()

    def _ciclo(self) -> dict[str, int]:
        self.tarifas.carregar()
        ids = self.caixa.buscar(self.cfg.gmail_query)
        log.info("Emails a avaliar: %d", len(ids))

        contagem: dict[str, int] = {}
        for uid in ids:
            try:
                desfecho = self._processar(uid)
            except CredencialInvalida:
                raise  # credencial ruim afeta todos; nao adianta seguir
            except Exception:
                # Um email problematico nao pode impedir os outros de serem
                # processados. Ele fica sem label e volta na proxima rodada.
                log.exception("Falha ao processar o UID %s; seguindo para o proximo", uid)
                desfecho = "erro"
            contagem[desfecho] = contagem.get(desfecho, 0) + 1
        return contagem

    # ---------------- unidade ----------------
    def _processar(self, uid: str) -> Desfecho:
        email = self.caixa.ler(uid)

        # A dedupe so pode acontecer aqui: a busca devolve UID, que muda entre
        # sessoes, enquanto o X-GM-MSGID estavel so aparece depois do FETCH.
        if self.banco.ja_processado(email.id):
            log.debug("Email %s ja processado, pulando", email.id)
            return "ignorado"

        log.info("Analisando %s de %s | %s", email.id, email.remetente, email.assunto)

        if self._deve_ignorar(email):
            return self._fechar(email, "ignorado", label=self.cfg.LABEL_PROCESSADO)

        try:
            pedido = self.extrator.analisar(email.assunto, email.corpo)
        except Exception as exc:  # a falha nao pode travar a fila
            log.exception("Falha na extracao de %s", email.id)
            self.banco.registrar(
                id_email=email.id,
                thread_id=email.thread_id,
                remetente=email.remetente,
                assunto=email.assunto,
                desfecho="erro",
                label=self.cfg.LABEL_REVISAR,
                erro=repr(exc),
            )
            self.caixa.aplicar_labels(email.uid, [self.cfg.LABEL_REVISAR], remover_unread=False)
            return "erro"

        # Resposta do cliente na mesma thread: ele manda so o dado que faltava,
        # e o corpo enviado ao LLM nao traz o historico citado. Sem recuperar o
        # que a thread ja tinha, o agente pediria os mesmos campos para sempre.
        if pedido.e_cotacao:
            anterior = self.banco.ultima_extracao(email.thread_id)
            if anterior:
                pedido = pedido.mesclar(PedidoCotacao.de_dict(anterior))
                log.info(
                    "Mesclado com a extracao anterior da thread (confianca %.2f)",
                    pedido.confianca,
                )

        extraido = dataclasses.asdict(pedido)

        if not pedido.e_cotacao:
            log.info("Nao e cotacao (confianca %.2f)", pedido.confianca)
            return self._fechar(
                email, "ignorado", label=self.cfg.LABEL_PROCESSADO, extracao=extraido
            )

        if pedido.confianca < CONFIANCA_PARA_PEDIR_DADOS:
            log.warning("Confianca muito baixa (%.2f) — enviando para revisao", pedido.confianca)
            return self._fechar(
                email,
                "erro",
                label=self.cfg.LABEL_REVISAR,
                extracao=extraido,
                erro=f"confianca {pedido.confianca:.2f} abaixo de "
                f"{CONFIANCA_PARA_PEDIR_DADOS}",
            )

        # Checar a rota ANTES de pedir os dados que faltam: nao adianta pedir o
        # peso de uma carga para um trecho que nao atendemos. Isso exige apenas
        # origem e destino, que costumam vir no primeiro email.
        if pedido.origem and pedido.destino:
            recusa = self._checar_rota(email, pedido, extraido)
            if recusa:
                return recusa

        exigir_peso = self.cfg.exigir_peso
        if not pedido.completo(exigir_peso):
            log.info("Dados faltantes: %s", pedido.campos_faltantes(exigir_peso))
            self._responder(
                email, mensagens.solicitar_dados(pedido, email.primeiro_nome, exigir_peso)
            )
            return self._fechar(
                email,
                "incompleto",
                label=self.cfg.LABEL_INCOMPLETO,
                extracao=extraido,
                origem=pedido.origem,
                destino=pedido.destino,
            )

        # Dados completos: daqui para frente sai um preco, entao o corte sobe.
        if pedido.confianca < CONFIANCA_PARA_COTAR:
            log.warning(
                "Dados completos mas confianca %.2f insuficiente para cotar",
                pedido.confianca,
            )
            return self._fechar(
                email,
                "erro",
                label=self.cfg.LABEL_REVISAR,
                extracao=extraido,
                origem=pedido.origem,
                destino=pedido.destino,
                erro=f"confianca {pedido.confianca:.2f} abaixo de "
                f"{CONFIANCA_PARA_COTAR} para emitir preco",
            )

        tarifa = self.tarifas.buscar(pedido.origem, pedido.destino, pedido.modal)
        if tarifa is None:  # ja tratado em _checar_rota; guarda de seguranca
            return self._checar_rota(email, pedido, extraido) or "erro"

        cotacao = precificacao.calcular(pedido, tarifa)
        log.info(
            "Cotado %s -> %s | rota %s | %d volumes | R$ %.2f",
            pedido.origem,
            pedido.destino,
            tarifa.id_rota,
            cotacao.qtd_volumes,
            cotacao.total,
        )

        if cotacao.alerta_peso:
            # Peso acima do limite por volume: nao respondemos automaticamente.
            log.warning("Peso fora do limite: %s", cotacao.alerta_peso)
            return self._fechar(
                email,
                "erro",
                label=self.cfg.LABEL_REVISAR,
                extracao=extraido,
                origem=pedido.origem,
                destino=pedido.destino,
                erro=cotacao.alerta_peso,
            )

        self._responder(email, mensagens.enviar_cotacao(pedido, cotacao, email.primeiro_nome))
        return self._fechar(
            email,
            "cotado",
            label=self.cfg.LABEL_PROCESSADO,
            extracao=extraido,
            origem=pedido.origem,
            destino=pedido.destino,
            id_rota=tarifa.id_rota,
            qtd_volumes=cotacao.qtd_volumes,
            valor_nf=cotacao.valor_nf,
            peso_kg=pedido.peso_kg,
            valor_frete=cotacao.total,
        )

    # ---------------- auxiliares ----------------
    def _checar_rota(
        self, email: Email, pedido: PedidoCotacao, extraido: dict
    ) -> Desfecho | None:
        """None se ha tarifa vigente. Caso contrario, ja fecha o email."""
        if self.tarifas.buscar(pedido.origem, pedido.destino, pedido.modal):
            return None

        if self.tarifas.trecho_cadastrado(pedido.origem, pedido.destino):
            # Trecho existe mas a tarifa esta INATIVA ou fora da vigencia.
            # Nunca dizer ao cliente que nao atendemos: e falha de cadastro.
            motivo = (
                f"trecho {pedido.origem} -> {pedido.destino} cadastrado, "
                "porem sem tarifa vigente (INATIVO ou vigencia expirada)"
            )
            log.warning("Tarifa indisponivel: %s", motivo)
            return self._fechar(
                email,
                "erro",
                label=self.cfg.LABEL_REVISAR,
                extracao=extraido,
                origem=pedido.origem,
                destino=pedido.destino,
                erro=motivo,
            )

        log.info("Rota nao atendida: %s -> %s", pedido.origem, pedido.destino)
        self._responder(email, mensagens.sem_rota(pedido, email.primeiro_nome))
        return self._fechar(
            email,
            "sem_rota",
            label=self.cfg.LABEL_SEM_ROTA,
            extracao=extraido,
            origem=pedido.origem,
            destino=pedido.destino,
        )

    def _deve_ignorar(self, email: Email) -> bool:
        endereco = (email.remetente or "").lower()
        if not endereco or endereco == self.cfg.gmail_user.lower():
            return True
        return any(marca in endereco for marca in REMETENTES_IGNORADOS)

    def _responder(self, email: Email, texto: str) -> None:
        if self.cfg.modo_resposta != "enviar":
            self.caixa.criar_rascunho(email, texto, self.cfg.remetente)
            return
        if self.enviador is None:
            raise RuntimeError(
                "MODO_RESPOSTA=enviar exige SMTP configurado no .env "
                "(SMTP_USUARIO e SMTP_SENHA_APP)"
            )
        self.enviador.responder(email, texto)

    def _fechar(self, email: Email, desfecho: Desfecho, *, label: str, **extra) -> Desfecho:
        self.banco.registrar(
            id_email=email.id,
            thread_id=email.thread_id,
            remetente=email.remetente,
            assunto=email.assunto,
            desfecho=desfecho,
            label=label,
            **extra,
        )
        self.caixa.aplicar_labels(email.uid, [label])
        return desfecho
