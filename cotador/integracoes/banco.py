"""SQLite: idempotencia (nao processar o mesmo email duas vezes) e auditoria."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS processados (
    id_email        TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL,
    remetente       TEXT,
    assunto         TEXT,
    desfecho        TEXT NOT NULL,
    origem          TEXT,
    destino         TEXT,
    id_rota         TEXT,
    qtd_volumes     INTEGER,
    valor_nf        REAL,
    peso_kg         REAL,
    valor_frete     REAL,
    extracao_json   TEXT,
    erro            TEXT,
    criado_em       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processados_thread ON processados(thread_id);
CREATE INDEX IF NOT EXISTS idx_processados_desfecho ON processados(desfecho);
"""

_CAMPOS = (
    "id_email",
    "thread_id",
    "remetente",
    "assunto",
    "desfecho",
    "origem",
    "destino",
    "id_rota",
    "qtd_volumes",
    "valor_nf",
    "peso_kg",
    "valor_frete",
    "extracao_json",
    "erro",
    "criado_em",
)


class Banco:
    def __init__(self, caminho: Path) -> None:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        self._caminho = caminho
        with closing(self._conectar()) as con:
            con.executescript(ESQUEMA)
            con.commit()

    def _conectar(self) -> sqlite3.Connection:
        return sqlite3.connect(self._caminho)

    def ja_processado(self, id_email: str) -> bool:
        with closing(self._conectar()) as con:
            cur = con.execute("SELECT 1 FROM processados WHERE id_email = ?", (id_email,))
            return cur.fetchone() is not None

    def ultima_extracao(self, thread_id: str) -> dict | None:
        """Extracao mais recente ja registrada nesta thread.

        Usada para mesclar quando o cliente responde so o dado que faltava.
        """
        with closing(self._conectar()) as con:
            cur = con.execute(
                """SELECT extracao_json FROM processados
                   WHERE thread_id = ? AND extracao_json IS NOT NULL
                   ORDER BY criado_em DESC LIMIT 1""",
                (thread_id,),
            )
            linha = cur.fetchone()
        return json.loads(linha[0]) if linha else None

    def limpar_erros(self) -> int:
        """Esquece os emails com desfecho 'erro' para que voltem para a fila."""
        with closing(self._conectar()) as con:
            cur = con.execute("DELETE FROM processados WHERE desfecho = 'erro'")
            con.commit()
            return cur.rowcount

    def registrar(
        self,
        *,
        id_email: str,
        thread_id: str,
        remetente: str | None,
        assunto: str | None,
        desfecho: str,
        origem: str | None = None,
        destino: str | None = None,
        id_rota: str | None = None,
        qtd_volumes: int | None = None,
        valor_nf: float | None = None,
        peso_kg: float | None = None,
        valor_frete: float | None = None,
        extracao: dict | None = None,
        erro: str | None = None,
    ) -> None:
        valores = (
            id_email,
            thread_id,
            remetente,
            assunto,
            desfecho,
            origem,
            destino,
            id_rota,
            qtd_volumes,
            valor_nf,
            peso_kg,
            valor_frete,
            json.dumps(extracao, ensure_ascii=False) if extracao else None,
            erro,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        marcadores = ",".join("?" * len(_CAMPOS))
        with closing(self._conectar()) as con:
            con.execute(
                f"INSERT OR REPLACE INTO processados ({','.join(_CAMPOS)}) "
                f"VALUES ({marcadores})",
                valores,
            )
            con.commit()
