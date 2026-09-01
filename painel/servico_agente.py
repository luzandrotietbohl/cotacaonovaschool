"""Roda o ciclo do agente numa thread de fundo, com parada cooperativa.

O painel e o loop vivem no mesmo processo: ligar/desligar e um Event, nao um
processo a gerenciar. `fabrica_agente` e chamada a cada ciclo para montar o
Agente com a configuracao vigente (o modo rascunho/enviar muda em runtime).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable

from cotador.integracoes.caixa_imap import CredencialInvalida

log = logging.getLogger(__name__)


class ServicoAgente:
    def __init__(
        self,
        fabrica_agente: Callable,
        intervalo_segundos: float,
        timeout_desligar: float = 10.0,
    ) -> None:
        self._fabrica = fabrica_agente
        self.intervalo_segundos = intervalo_segundos
        # Quanto desligar() espera o ciclo corrente terminar antes de desistir.
        self.timeout_desligar = timeout_desligar
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        # Um ciclo por vez, mesmo se "rodar agora" coincidir com o loop.
        self._trava = threading.Lock()
        # Flask atende em varias threads: dois ligar() simultaneos nao podem
        # criar dois lacos, nem ligar() correr junto com desligar().
        self._trava_liga = threading.Lock()
        self.ultimo_resumo: dict[str, int] | None = None
        self.ultimo_erro: str | None = None
        self.ultimo_ciclo_em: str | None = None
        self.credencial_recusada = False

    @property
    def rodando(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def ligar(self) -> None:
        with self._trava_liga:
            if self.rodando:
                return
            self._parar.clear()
            self._thread = threading.Thread(
                target=self._laco, daemon=True, name="loop-agente"
            )
            self._thread.start()

    def desligar(self) -> None:
        with self._trava_liga:
            self._parar.set()
            thread = self._thread
            if thread is None:
                return
            thread.join(timeout=self.timeout_desligar)
            if thread.is_alive():
                # Sem o handle, `rodando` mentiria e um ligar() seguinte criaria
                # um segundo laco com o primeiro ainda girando.
                log.warning("Ciclo ainda em andamento; a thread encerra ao terminar")
                return
            self._thread = None

    def ciclo_unico(self) -> dict[str, int]:
        with self._trava:
            return self._um_ciclo()

    # ---------------- interno ----------------
    def _um_ciclo(self) -> dict[str, int]:
        try:
            resumo = self._fabrica().rodar_ciclo()
            self.ultimo_resumo = resumo
            self.ultimo_erro = None
            self.credencial_recusada = False
            return resumo
        except CredencialInvalida as exc:
            self.ultimo_erro = str(exc)
            self.credencial_recusada = True
            raise
        except Exception as exc:
            self.ultimo_erro = repr(exc)
            raise
        finally:
            self.ultimo_ciclo_em = datetime.now().isoformat(timespec="seconds")

    def _laco(self) -> None:
        while not self._parar.is_set():
            try:
                with self._trava:
                    # A espera pela trava pode ter atravessado um desligar();
                    # sem esta conferencia rodariamos um ciclo a mais.
                    if self._parar.is_set():
                        return
                    self._um_ciclo()
            except CredencialInvalida:
                # Insistir nao resolve credencial ruim; o painel exibe o erro.
                log.error("Credencial recusada; loop desligado")
                return
            except Exception:
                log.exception("Ciclo falhou; proxima tentativa no intervalo")
            self._parar.wait(self.intervalo_segundos)
