from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from cotador.ml.clusterizacao import (
    FEATURES_CLUSTER,
    _leitor,
    _nomear,
    carregar_embarques,
    clusterizar,
    escolher_k,
    preparar_features,
)


def escrever_olist(pasta: Path, embarques: int = 80) -> Path:
    """Mini-Olist sintético com dois regimes: local leve e interestadual pesado."""
    pasta.mkdir(parents=True, exist_ok=True)
    gerador = np.random.default_rng(7)
    linhas_pedidos, linhas_itens, linhas_clientes, linhas_avaliacoes = [], [], [], []
    for i in range(embarques):
        pesado = i % 2 == 0
        pedido, cliente = f"o{i:04d}", f"c{i:04d}"
        cep = "30110" if pesado else "01001"
        dia = 1 + i % 27
        compra = f"2018-03-{dia:02d} 10:00:00"
        entrega = f"2018-04-{dia:02d} 10:00:00"
        linhas_pedidos.append({
            "order_id": pedido, "customer_id": cliente, "order_status": "delivered",
            "order_purchase_timestamp": compra, "order_approved_at": compra,
            "order_delivered_carrier_date": compra, "order_delivered_customer_date": entrega,
            "order_estimated_delivery_date": f"2018-04-{min(28, dia + 2):02d} 00:00:00",
        })
        linhas_clientes.append({
            "customer_id": cliente, "customer_unique_id": cliente,
            "customer_zip_code_prefix": cep, "customer_city": "belo horizonte" if pesado else "sao paulo",
            "customer_state": "MG" if pesado else "SP",
        })
        linhas_itens.append({
            "order_id": pedido, "order_item_id": 1, "product_id": "p1" if pesado else "p2",
            "seller_id": "s1", "shipping_limit_date": compra,
            "price": float(200 + gerador.normal(0, 5)) if pesado else float(40 + gerador.normal(0, 2)),
            "freight_value": float(40 + gerador.normal(0, 2)) if pesado else float(9 + gerador.normal(0, 1)),
        })
        linhas_avaliacoes.append({
            "review_id": f"r{i:04d}", "order_id": pedido, "review_score": 3 if pesado else 5,
            "review_comment_title": "", "review_comment_message": "",
            "review_creation_date": entrega, "review_answer_timestamp": entrega,
        })
    pd.DataFrame(linhas_pedidos).to_csv(pasta / "olist_orders_dataset.csv", index=False)
    pd.DataFrame(linhas_itens).to_csv(pasta / "olist_order_items_dataset.csv", index=False)
    pd.DataFrame(linhas_clientes).to_csv(pasta / "olist_customers_dataset.csv", index=False)
    pd.DataFrame(linhas_avaliacoes).to_csv(pasta / "olist_order_reviews_dataset.csv", index=False)
    pd.DataFrame([
        {"product_id": "p1", "product_category_name": "moveis_escritorio", "product_weight_g": 12000,
         "product_length_cm": 60, "product_height_cm": 40, "product_width_cm": 40},
        {"product_id": "p2", "product_category_name": "telefonia", "product_weight_g": 300,
         "product_length_cm": 20, "product_height_cm": 10, "product_width_cm": 15},
    ]).to_csv(pasta / "olist_products_dataset.csv", index=False)
    pd.DataFrame([{"seller_id": "s1", "seller_zip_code_prefix": "01001",
                   "seller_city": "sao paulo", "seller_state": "SP"}]).to_csv(pasta / "olist_sellers_dataset.csv", index=False)
    pd.DataFrame([
        {"geolocation_zip_code_prefix": "01001", "geolocation_lat": -23.5505, "geolocation_lng": -46.6333,
         "geolocation_city": "sao paulo", "geolocation_state": "SP"},
        {"geolocation_zip_code_prefix": "30110", "geolocation_lat": -19.9167, "geolocation_lng": -43.9345,
         "geolocation_city": "belo horizonte", "geolocation_state": "MG"},
    ]).to_csv(pasta / "olist_geolocation_dataset.csv", index=False)
    return pasta


class Leitura(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pasta = escrever_olist(Path(self.tmp.name) / "csvs")

    def tearDown(self):
        self.tmp.cleanup()

    def test_leitor_aceita_pasta_de_csvs(self):
        ler = _leitor(self.pasta)
        self.assertEqual(len(ler("olist_sellers_dataset.csv")), 1)

    def test_leitor_aceita_zip(self):
        destino = Path(self.tmp.name) / "olist.zip"
        with zipfile.ZipFile(destino, "w") as arquivo:
            for csv in self.pasta.glob("*.csv"):
                arquivo.write(csv, f"archive/{csv.name}")
        self.assertEqual(len(_leitor(destino)("olist_sellers_dataset.csv")), 1)

    def test_leitor_recusa_pasta_incompleta(self):
        vazia = Path(self.tmp.name) / "vazia"
        vazia.mkdir()
        with self.assertRaises(FileNotFoundError):
            _leitor(vazia)

    def test_carregar_embarques_deriva_cubagem_e_prazo(self):
        dados = carregar_embarques(self.pasta)
        self.assertEqual(len(dados), 80)
        pesado = dados.loc[dados["category"].eq("moveis_escritorio")].iloc[0]
        self.assertAlmostEqual(pesado["weight_total_kg"], 12.0)
        self.assertAlmostEqual(pesado["cubic_weight_kg"], 96000 / 6000)
        self.assertAlmostEqual(pesado["density"], 12.0 / 16.0)
        self.assertGreater(pesado["distance_km"], 400)
        self.assertGreater(pesado["lead_days"], 0)
        self.assertTrue((dados["freight_value"] > 0).all())

    def test_embarque_sem_geolocalizacao_e_descartado(self):
        clientes = pd.read_csv(self.pasta / "olist_customers_dataset.csv")
        clientes.loc[0, "customer_zip_code_prefix"] = 99999
        clientes.to_csv(self.pasta / "olist_customers_dataset.csv", index=False)
        self.assertEqual(len(carregar_embarques(self.pasta)), 79)


class Features(unittest.TestCase):
    def test_preparar_features_aplica_log(self):
        dados = pd.DataFrame([{"distance_km": 100, "weight_total_kg": 3, "cubic_weight_kg": 2,
                               "declared_value": 50, "quantity_items": 2, "density": 1.5}])
        x = preparar_features(dados)
        self.assertEqual(list(x.columns), FEATURES_CLUSTER)
        self.assertAlmostEqual(x.loc[0, "distance_km"], np.log1p(100))
        self.assertAlmostEqual(x.loc[0, "log_density"], np.log(1.5))

    def test_preparar_features_nao_propaga_nulo(self):
        dados = pd.DataFrame([{"distance_km": np.nan, "weight_total_kg": 1, "cubic_weight_kg": 1,
                               "declared_value": 10, "quantity_items": 1, "density": np.nan}])
        self.assertFalse(preparar_features(dados).isna().to_numpy().any())


class Nomes(unittest.TestCase):
    def test_nomear_usa_traco_dominante(self):
        perfil = pd.DataFrame({
            "distance_km": [20, 500, 480, 450, 510, 470],
            "weight_total_kg": [.4, 8.3, .8, 1.6, .2, 1.0],
            "quantity_items": [1.0, 1.1, 1.0, 2.5, 1.0, 1.0],
            "freight_pct_value": [18, 18, 22, 28, 45, 16],
            "density": [.57, 1.03, 1.93, .58, .45, .5],
            "cubic_weight_kg": [.75, 7.8, .46, 2.56, .6, 2.25],
        })
        nomes = _nomear(perfil)
        self.assertEqual(nomes[0], "local_metropolitano")
        self.assertEqual(nomes[1], "carga_pesada")
        self.assertEqual(nomes[2], "denso_compacto")
        self.assertEqual(nomes[3], "multi_item")
        self.assertEqual(nomes[4], "miudo_baixo_valor")
        self.assertEqual(nomes[5], "volumoso_leve")

    def test_nomear_rotula_grupos_extra(self):
        perfil = pd.DataFrame({
            "distance_km": [10, 20, 30, 40, 50, 60, 70],
            "weight_total_kg": [1, 2, 3, 4, 5, 6, 7],
            "quantity_items": [1, 1, 1, 1, 1, 1, 2],
            "freight_pct_value": [10, 20, 30, 40, 50, 60, 70],
            "density": [1, 2, 3, 4, 5, 6, 7],
            "cubic_weight_kg": [1, 2, 3, 4, 5, 6, 7],
        })
        self.assertEqual(sum(nome.startswith("grupo_") for nome in _nomear(perfil).values()), 1)


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.tmp.name)
        self.pasta = escrever_olist(self.raiz / "csvs")

    def tearDown(self):
        self.tmp.cleanup()

    def test_escolher_k_cobre_a_faixa(self):
        matriz = preparar_features(carregar_embarques(self.pasta)).to_numpy()
        diagnostico = escolher_k(matriz, k_min=2, k_max=3, seed=1)
        self.assertEqual([d["k"] for d in diagnostico], [2, 3])
        self.assertTrue(all(-1 <= d["silhouette"] <= 1 for d in diagnostico))

    def test_clusterizar_gera_artefatos_e_separa_regimes(self):
        saida = self.raiz / "relatorio"
        metadata = clusterizar(self.pasta, saida, k=2, k_min=2, k_max=3, replicas=2, seed=42)
        self.assertEqual(metadata["k"], 2)
        self.assertEqual(metadata["rows"], 80)
        for arquivo in ("clusters_embarques.csv", "clusters_perfil.csv", "clusters.png", "clusters.md", "clusters_metadata.json"):
            self.assertTrue((saida / arquivo).exists(), arquivo)
        rotulos = pd.read_csv(saida / "clusters_embarques.csv")
        # Os dois regimes sintéticos são bem separados: cada cluster fica com uma categoria.
        self.assertEqual(rotulos.groupby("cluster")["category"].nunique().tolist(), [1, 1])
        self.assertEqual(rotulos["cluster_nome"].nunique(), 2)
        gravado = json.loads((saida / "clusters_metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(gravado["features"], FEATURES_CLUSTER)
        self.assertEqual(len(gravado["ari_bootstrap"]), 2)
        self.assertIn("Clusterização dos embarques Olist", (saida / "clusters.md").read_text(encoding="utf-8"))

    def test_clusterizar_escolhe_k_quando_nao_informado(self):
        metadata = clusterizar(self.pasta, self.raiz / "auto", k=None, k_min=2, k_max=3, replicas=1, seed=42)
        self.assertIn(metadata["k"], (2, 3))


if __name__ == "__main__":
    unittest.main()
