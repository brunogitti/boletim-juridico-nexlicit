"""Testes do fatiador — todos rodam sobre o texto_bruto real que cada
coletor já gera a partir dos fixtures existentes em tests/fixtures/ (não
fixtures novas: os mesmos documentos já usados nas Etapas 2 a 3e)."""

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

import coletores.stj as stj
import coletores.tce_mg as tce_mg
import coletores.tce_pr as tce_pr
import coletores.tce_sp as tce_sp
import coletores.tcu as tcu
import coletores.zenite as zenite
import nucleo.fatiador as fatiador

FIXTURES = Path(__file__).parent / "fixtures"


class _RespostaFalsaTexto:
    def __init__(self, texto):
        self.text = texto


class _RespostaFalsaJson:
    def __init__(self, dados_json, headers=None):
        self._dados_json = dados_json
        self.headers = headers or {}

    def json(self):
        return self._dados_json


# --- TCE-PR: fatia de verdade --------------------------------------------

def test_fatiar_tce_pr_usa_fixture_real():
    html_texto = (FIXTURES / "tce_pr" / "boletim_179.html").read_text(encoding="utf-8")
    texto_bruto = tce_pr._extrair_conteudo_principal(html_texto)

    decisoes = fatiador._fatiar_tce_pr(item_bruto_id=1, texto_bruto=texto_bruto)

    assert len(decisoes) == 2

    primeira, segunda = decisoes
    assert primeira.tribunal == "TCE-PR"
    assert primeira.numero_acordao == "3190/2025"
    assert primeira.numero_processo == "330990/2024"
    assert primeira.orgao_julgador == "Primeira Câmara"
    assert primeira.relator == "THIAGO BARBOSA CORDEIRO"
    assert primeira.data_julgamento == "2025-11-10"
    assert primeira.url_inteiro_teor == (
        "https://viajuris.tce.pr.gov.br/Pesquisa/Visualizar/"
        "3190-2025-primeira-camara-thiago-barbosa-cordeiro-tomada-de-"
        "contas-extraordinaria-prejulgados-5/200060"
    )

    assert segunda.numero_acordao == "3220/2025"
    assert segunda.numero_processo == "582863/2012"
    assert segunda.orgao_julgador == "Segunda Câmara"
    assert segunda.relator == "FERNANDO AUGUSTO MELLO GUIMARÃES"
    assert segunda.data_julgamento == "2025-11-10"


# --- TCE-MG: fatia de verdade, dois tribunais dentro da mesma edição -----

def test_fatiar_tce_mg_usa_fixture_real():
    html_texto = (FIXTURES / "tce_mg" / "informativo_333.html").read_text(encoding="utf-8")
    texto_bruto = tce_mg._extrair_conteudo_principal(html_texto)

    decisoes = fatiador._fatiar_tce_mg(item_bruto_id=1, texto_bruto=texto_bruto)

    assert len(decisoes) == 2

    propria, tcu_embutida = decisoes

    assert propria.tribunal == "TCE-MG"
    assert propria.numero_acordao is None  # achado real: TCE-MG não cita acórdão
    assert propria.numero_processo == "1152957"
    assert propria.orgao_julgador == "Primeira Câmara"
    assert propria.relator == "Conselheiro Alencar da Silveira Jr."
    assert propria.data_julgamento == "2026-06-16"
    assert propria.url_inteiro_teor == "https://tcjuris.tce.mg.gov.br/Home/Detalhes/1152957"

    assert tcu_embutida.tribunal == "TCU"
    assert tcu_embutida.numero_acordao == "1370/2026"
    assert tcu_embutida.orgao_julgador == "Plenário"
    assert tcu_embutida.relator == "Jhonatan de Jesus"
    assert tcu_embutida.url_inteiro_teor is None  # achado real: sem link nesse trecho


# --- TCE-SP boletim (PDF): fatia de verdade, pula o Sumário ---------------

def test_fatiar_tce_sp_boletim_usa_fixture_real():
    pdf_bytes = (FIXTURES / "tce_sp" / "boletim_edicao_53_recorte.pdf").read_bytes()
    texto_bruto = tce_sp._extrair_texto_pdf(pdf_bytes)

    decisoes = fatiador._fatiar_tce_sp_boletim(item_bruto_id=1, texto_bruto=texto_bruto)

    assert len(decisoes) == 2  # e não mais, se o Sumário tivesse vazado

    primeira, segunda = decisoes
    assert primeira.tribunal == "TCE-SP"
    assert primeira.numero_acordao is None  # achado real: TCE-SP cita por processo
    assert primeira.numero_processo == "020837.989.25-0 e outros"
    assert primeira.relator == "Conselheiro Renato Martins Costa"
    assert primeira.data_julgamento == "2026-03-11"
    assert primeira.url_inteiro_teor == (
        "https://jurisprudencia.tce.sp.gov.br/arqs_juri/pdf/6/7/7/20075776.pdf"
    )

    assert segunda.numero_processo == "020800.989.25-3 e outro"
    assert segunda.relator == "Conselheiro Dimas Ramalho"
    assert segunda.data_julgamento == "2026-03-11"


def test_remover_linhas_com_pontilhado_preserva_citacao_quebrada_em_duas_linhas():
    # regressão do bug real achado no smoke test: relator com nome longo
    # quebra a citação do Sumário em duas linhas, e só a segunda tem os
    # pontinhos — a primeira linha não pode vazar pro corpo real
    texto = (
        "020837.989.25-0 e outros .......................... 4\n"
        "(Sessão Plenária de 11/03/2026. Redatoria: Conselheiro Renato\n"
        "Martins Costa) .......................... 4\n"
        "CONTEÚDO REAL COMEÇA AQUI\n"
    )
    resultado = fatiador._remover_linhas_com_pontilhado(texto)
    assert resultado.strip() == "CONTEÚDO REAL COMEÇA AQUI"


# --- STJ: já atômico, só extrai metadados ---------------------------------

def test_fatiar_stj_usa_fixture_real():
    html_texto = (FIXTURES / "stj" / "edicao_887.html").read_text(encoding="utf-8")
    edicao = stj.EdicaoFeed(
        numero="0887", data_publicacao="2026-05-05T03:00:00+00:00", url="https://x/887",
    )
    itens = stj._extrair_notas_administrativas(html_texto, edicao)
    assert len(itens) == 2

    com_link = fatiador._fatiar_stj(1, itens[0].texto_bruto, itens[0].url_origem, itens[0].data_publicacao)[0]
    assert com_link.tribunal == "STJ"
    assert com_link.numero_processo == "AgInt no REsp 2.162.500-RJ"
    assert com_link.orgao_julgador == "Primeira Turma"
    assert com_link.relator == "Benedito Gonçalves"
    assert com_link.data_julgamento == "2026-04-13"

    sem_link = fatiador._fatiar_stj(1, itens[1].texto_bruto, itens[1].url_origem, itens[1].data_publicacao)[0]
    assert sem_link.numero_processo == "Processo em segredo de justiça"
    assert sem_link.relator == "Gurgel de Faria"
    assert sem_link.url_inteiro_teor == "https://x/887"  # fallback pra URL da edição


# --- TCU: já atômico, só extrai metadados ----------------------------------

def test_fatiar_tcu_usa_fixture_real():
    texto_csv = (FIXTURES / "tcu" / "boletim-informativo-lc.csv").read_text(encoding="utf-8-sig")
    leitor = csv.DictReader(io.StringIO(texto_csv), delimiter="|")
    linhas = [linha for linha in leitor if linha["TEXTOACORDAO"].strip()]
    assert len(linhas) == 3  # confirma que o fixture tem 3 linhas válidas

    item = tcu._montar_item(linhas[0], "TCU Informativo LC")
    decisao = fatiador._fatiar_tcu(1, item.texto_bruto, item.url_origem)[0]

    assert decisao.tribunal == "TCU"
    assert decisao.numero_acordao == "2357/2026"
    assert decisao.orgao_julgador == "Primeira Câmara"
    assert decisao.relator == "Bruno Dantas"
    assert decisao.url_inteiro_teor == item.url_origem


def test_fatiar_tcu_nao_confunde_titulo_com_citacao():
    # regressão do bug real achado no smoke test: o título já repete
    # "Acórdão N/AAAA ÓRGÃO" antes da linha "Citação:" — sem a âncora
    # certa, o relator/órgão extraído vinha errado
    texto_bruto = (
        "TCU Informativo LC 1/2026 — Acórdão 999/2026 Segunda Câmara\n\n"
        "Citação: Acórdão 111/2026 Plenário, Representação, "
        "Relator Ministro Fulano de Tal\n\n"
        "Ementa: texto qualquer"
    )
    decisao = fatiador._fatiar_tcu(1, texto_bruto, "https://x")[0]
    assert decisao.numero_acordao == "111/2026"
    assert decisao.orgao_julgador == "Plenário"
    assert decisao.relator == "Fulano de Tal"


# --- TCE-SP súmulas: já atômico, só extrai metadados -----------------------

def test_fatiar_tce_sp_sumula_usa_fixture_real(monkeypatch):
    pagina = (FIXTURES / "tce_sp" / "sumulas.html").read_text(encoding="utf-8")
    monkeypatch.setattr(tce_sp, "_requisitar", lambda sessao, url: _RespostaFalsaTexto(pagina))

    itens = tce_sp._coletar_sumulas(sessao=None)
    assert len(itens) == 2

    normal = fatiador.fatiar_item(
        "TCE-SP — Boletim de Jurisprudência + Súmulas", 1,
        itens[0].titulo, itens[0].texto_bruto, itens[0].url_origem, itens[0].data_publicacao,
    )[0]
    assert normal.tribunal == "TCE-SP"
    assert normal.numero_acordao == "1"  # número da súmula, decisão combinada antes de implementar
    assert normal.url_inteiro_teor == itens[0].url_origem


# --- Zênite: já atômico, sem extração de metadados -------------------------

def test_fatiar_zenite_usa_fixture_real(monkeypatch):
    posts = json.loads((FIXTURES / "zenite" / "wp_json_posts.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(
        zenite, "_requisitar",
        lambda sessao, url, params=None: _RespostaFalsaJson(posts, {"X-WP-TotalPages": "1"}),
    )

    itens = zenite._coletar_wp_json(
        sessao=requests.Session(), desde=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    assert len(itens) == 2

    decisao = fatiador.fatiar_item(
        "Zênite", 1, itens[0].titulo, itens[0].texto_bruto,
        itens[0].url_origem, itens[0].data_publicacao,
    )[0]

    assert decisao.tribunal is None
    assert decisao.numero_acordao is None
    assert decisao.url_inteiro_teor is None
    assert decisao.texto_decisao == itens[0].texto_bruto


# --- dispatcher -------------------------------------------------------------

def test_fatiar_item_fonte_desconhecida_levanta_erro():
    with pytest.raises(ValueError):
        fatiador.fatiar_item("Fonte Desconhecida", 1, "t", "x", "https://x", None)
