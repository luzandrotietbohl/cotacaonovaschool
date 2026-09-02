"""Popula o banco com dados FICTICIOS para demonstracao do painel.

Uso:
    python seed_ficticios.py            # adiciona ~80 registros
    python seed_ficticios.py --limpar   # apaga tudo antes de popular

Gera emails plausiveis com desfechos na proporcao real de operacao
(maioria cotado, alguns aguardando dados, revisao humana, sem rota e
ignorados), threads com mais de um email e datas nos ultimos 14 dias.
So mexe no SQLite; nao toca em Gmail, Sheets nem na Claude API.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone

from cotador.config import Config
from cotador.integracoes.banco import Banco

rnd = random.Random(42)  # reproduzivel: rodar duas vezes gera o mesmo cenario

NOMES = [
    "Ana Souza", "Bruno Lima", "Carla Mendes", "Diego Rocha", "Elisa Prado",
    "Fabio Nunes", "Gabriela Reis", "Henrique Alves", "Iara Campos",
    "Joao Pedro Farias", "Karina Lopes", "Lucas Teixeira", "Marina Duarte",
    "Nelson Barros", "Otavia Pires", "Paulo Cesar Brito", "Renata Gusmao",
    "Sergio Tavares", "Tais Moreira", "Vicente Aragao",
]
EMPRESAS = [
    "acmelog", "transvale", "brcargo", "lojamix", "eletrofort", "casaverde",
    "megapecas", "distrisul", "atacadao10", "modaviva",
]
ROTAS = [
    ("R00001", "Sao Paulo/SP", "Campinas/SP", 16.65, 4.20, 55.0, 0.30, 0.25, 0.0, 1),
    ("R00002", "Sao Paulo/SP", "Rio de Janeiro/RJ", 22.10, 6.80, 90.0, 0.30, 0.25, 0.0, 2),
    ("R00003", "Campinas/SP", "Belo Horizonte/MG", 25.40, 7.10, 110.0, 0.35, 0.25, 15.0, 3),
    ("R00004", "Sao Paulo/SP", "Curitiba/PR", 24.00, 8.30, 95.0, 0.30, 0.25, 0.0, 2),
    ("R00005", "Rio de Janeiro/RJ", "Vitoria/ES", 27.90, 5.50, 120.0, 0.35, 0.30, 20.0, 3),
    ("R00006", "Sao Paulo/SP", "Porto Alegre/RS", 31.20, 9.90, 150.0, 0.35, 0.30, 0.0, 4),
]
SEM_ROTA = [
    ("Manaus/AM", "Sao Paulo/SP"), ("Recife/PE", "Fortaleza/CE"),
    ("Cuiaba/MT", "Belem/PA"), ("Sao Paulo/SP", "Boa Vista/RR"),
]
ASSUNTOS_COTACAO = [
    "Cotacao de frete", "Orcamento de transporte", "Frete urgente",
    "Solicitacao de cotacao", "Preco de envio", "Re: Cotacao de frete",
]
ASSUNTOS_IGNORADOS = [
    "Newsletter semanal de logistica", "Promocao imperdivel!!",
    "Confirmacao de leitura", "Convite: webinar de supply chain",
]


def frete(rota, volumes: int, valor_nf: float) -> float:
    _, _, _, v_vol, pedagio, minimo, gris, adv, taxa, _ = rota
    base = max(volumes * (v_vol + pedagio), minimo)
    return round(base + valor_nf * (gris + adv) / 100 + taxa, 2)


def extracao(origem, destino, volumes, valor_nf, peso, confianca) -> dict:
    return {
        "e_cotacao": True, "confianca": confianca, "origem": origem,
        "destino": destino, "qtd_volumes": volumes, "valor_nf": valor_nf,
        "peso_kg": peso, "volumes": [], "m3_informado": None,
        "modal": None, "observacoes": None,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limpar", action="store_true", help="apaga os registros antes")
    args = p.parse_args()

    cfg = Config.carregar()
    banco = Banco(cfg.banco)
    if args.limpar:
        with sqlite3.connect(cfg.banco) as con:
            con.execute("DELETE FROM processados")
        print("Tabela processados esvaziada.")

    agora = datetime.now(timezone.utc)
    seq = 9_000_000_000_000_000  # ids ficticios bem longe dos reais do Gmail
    registros: list[tuple[str, dict]] = []  # (criado_em, kwargs do registrar)

    def novo_id() -> str:
        nonlocal seq
        seq += 1
        return str(seq)

    def remetente() -> tuple[str, str]:
        nome = rnd.choice(NOMES)
        usuario = nome.split()[0].lower() + "." + nome.split()[-1].lower()
        return nome, f"{usuario}@{rnd.choice(EMPRESAS)}.com.br"

    def quando(dias_max: int = 14) -> str:
        delta = timedelta(
            days=rnd.uniform(0, dias_max), hours=rnd.uniform(0, 10)
        )
        return (agora - delta).isoformat(timespec="seconds")

    # ---- 45 cotados (alguns em thread de 2 emails: incompleto -> cotado) ----
    for _ in range(45):
        rota = rnd.choice(ROTAS)
        _, origem, destino = rota[0], rota[1], rota[2]
        volumes = rnd.randint(1, 40)
        valor_nf = round(rnd.uniform(500, 45_000), 2)
        peso = round(volumes * rnd.uniform(4, 60), 1)
        nome, email = remetente()
        thread = novo_id()
        criado = quando()
        if rnd.random() < 0.35:  # thread com pedido incompleto antes
            registros.append((
                (datetime.fromisoformat(criado) - timedelta(hours=rnd.uniform(2, 30)))
                .isoformat(timespec="seconds"),
                dict(
                    id_email=novo_id(), thread_id=thread, remetente=email,
                    assunto=rnd.choice(ASSUNTOS_COTACAO), desfecho="incompleto",
                    label="cotador-aguardando-dados", origem=origem, destino=destino,
                    extracao=extracao(origem, destino, volumes, None, peso, 0.62),
                ),
            ))
        registros.append((criado, dict(
            id_email=novo_id(), thread_id=thread, remetente=email,
            assunto=rnd.choice(ASSUNTOS_COTACAO), desfecho="cotado",
            label="cotador-processado", origem=origem, destino=destino,
            id_rota=rota[0], qtd_volumes=volumes, valor_nf=valor_nf,
            peso_kg=peso, valor_frete=frete(rota, volumes, valor_nf),
            extracao=extracao(origem, destino, volumes, valor_nf, peso, round(rnd.uniform(0.72, 0.98), 2)),
        )))

    # ---- 12 aguardando dados (thread aberta, cliente ainda nao respondeu) ----
    for _ in range(12):
        rota = rnd.choice(ROTAS)
        origem, destino = rota[1], rota[2]
        nome, email = remetente()
        registros.append((quando(5), dict(
            id_email=novo_id(), thread_id=novo_id(), remetente=email,
            assunto=rnd.choice(ASSUNTOS_COTACAO), desfecho="incompleto",
            label="cotador-aguardando-dados", origem=origem, destino=destino,
            extracao=extracao(origem, destino, rnd.randint(1, 20), None, None, 0.58),
        )))

    # ---- 10 para revisao humana (motivos variados) ----
    MOTIVOS = [
        ("confianca 0.28 abaixo de 0.35", 0.28),
        ("confianca 0.51 abaixo de 0.6 para emitir preco", 0.51),
        ("trecho Sao Paulo/SP -> Salvador/BA cadastrado, porem sem tarifa vigente (INATIVO ou vigencia expirada)", 0.81),
        ("peso medio de 142.3 kg por volume excede o limite de 100 kg da rota R00001", 0.88),
        ("Extracao falhou: APITimeoutError('Request timed out')", None),
    ]
    for i in range(10):
        motivo, conf = MOTIVOS[i % len(MOTIVOS)]
        rota = rnd.choice(ROTAS)
        origem, destino = rota[1], rota[2]
        nome, email = remetente()
        registros.append((quando(7), dict(
            id_email=novo_id(), thread_id=novo_id(), remetente=email,
            assunto=rnd.choice(ASSUNTOS_COTACAO), desfecho="erro",
            label="cotador-revisar", origem=origem, destino=destino, erro=motivo,
            extracao=extracao(origem, destino, rnd.randint(1, 30),
                              round(rnd.uniform(1_000, 30_000), 2),
                              round(rnd.uniform(50, 4_000), 1), conf)
            if conf is not None else None,
        )))

    # ---- 8 sem rota ----
    for _ in range(8):
        origem, destino = rnd.choice(SEM_ROTA)
        nome, email = remetente()
        registros.append((quando(10), dict(
            id_email=novo_id(), thread_id=novo_id(), remetente=email,
            assunto=rnd.choice(ASSUNTOS_COTACAO), desfecho="sem_rota",
            label="cotador-sem-rota", origem=origem, destino=destino,
            extracao=extracao(origem, destino, rnd.randint(1, 15),
                              round(rnd.uniform(800, 20_000), 2), None, 0.85),
        )))

    # ---- 10 ignorados (spam/newsletter/nao-cotacao) ----
    for _ in range(10):
        nome, email = remetente()
        registros.append((quando(14), dict(
            id_email=novo_id(), thread_id=novo_id(),
            remetente=f"noticias@{rnd.choice(EMPRESAS)}.com.br",
            assunto=rnd.choice(ASSUNTOS_IGNORADOS), desfecho="ignorado",
            label="cotador-processado",
        )))

    # Garante movimento no dia de hoje para os cartoes da Visao geral.
    for i in range(min(9, len(registros))):
        registros[i] = (
            (agora - timedelta(hours=rnd.uniform(0, 6))).isoformat(timespec="seconds"),
            registros[i][1],
        )

    ids = [kw["id_email"] for _, kw in registros]
    assert len(ids) == len(set(ids)), (
        f"ids duplicados no gerador: {len(ids) - len(set(ids))}"
    )

    # registrar() carimba criado_em com o agora; corrigimos a data em seguida
    # direto no SQLite para espalhar o historico pelos ultimos 14 dias.
    for criado, kw in registros:
        banco.registrar(**kw)
    with sqlite3.connect(cfg.banco) as con:
        for criado, kw in registros:
            con.execute(
                "UPDATE processados SET criado_em = ? WHERE id_email = ?",
                (criado, kw["id_email"]),
            )

    with sqlite3.connect(cfg.banco) as con:
        total = con.execute("SELECT COUNT(*) FROM processados").fetchone()[0]
        por_desfecho = dict(con.execute(
            "SELECT desfecho, COUNT(*) FROM processados GROUP BY desfecho"
        ).fetchall())
    print(f"{len(registros)} registros ficticios inseridos. Total na tabela: {total}")
    print("Por desfecho:", json.dumps(por_desfecho, ensure_ascii=False))


if __name__ == "__main__":
    main()
