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
    assert primeira.identificador_exibicao is not None
    assert "3190/2025" in primeira.identificador_exibicao
    assert "THIAGO BARBOSA CORDEIRO" in primeira.identificador_exibicao

    assert segunda.numero_acordao == "3220/2025"
    assert segunda.numero_processo == "582863/2012"
    assert segunda.orgao_julgador == "Segunda Câmara"
    assert segunda.relator == "FERNANDO AUGUSTO MELLO GUIMARÃES"
    assert segunda.data_julgamento == "2025-11-10"


# --- TCE-MG: fatia de verdade, dois tribunais dentro da mesma edição -----
#
# Os dois fixtures abaixo são página real capturada ao vivo em 2026-08-07
# (não simplificada à mão) — a versão anterior tinha <h2> sintético em
# volta do nome do tribunal, que não existe no site de verdade, e por
# isso escondeu o bug real de segmentação até rodar contra dado real.

def test_fatiar_tce_mg_variante_1_ancora_vazia_usa_fixture_real():
    # edição 333: cabeçalho de seção é <p><a></a>Nome do Tribunal</p>
    html_texto = (FIXTURES / "tce_mg" / "informativo_333.html").read_text(encoding="utf-8")
    texto_bruto = tce_mg._extrair_conteudo_principal(html_texto)

    decisoes = fatiador._fatiar_tce_mg(item_bruto_id=1, texto_bruto=texto_bruto)

    assert len(decisoes) == 9  # 3 próprias + 6 do TCU embutidas na mesma edição

    proprias = [d for d in decisoes if d.tribunal == "TCE-MG"]
    outras = [d for d in decisoes if d.tribunal != "TCE-MG"]
    assert len(proprias) == 3
    assert len(outras) == 6
    assert {d.tribunal for d in outras} == {"TCU"}

    primeira = proprias[0]
    assert primeira.numero_acordao is None  # achado real: TCE-MG não cita acórdão aqui
    assert primeira.numero_processo == "1152957"
    assert primeira.orgao_julgador == "Primeira Câmara"
    assert primeira.relator == "Conselheiro Alencar da Silveira Jr."
    assert primeira.data_julgamento == "2026-06-16"
    assert primeira.url_inteiro_teor == "https://tcjuris.tce.mg.gov.br/Home/Detalhes/1152957"
    assert primeira.identificador_exibicao == (
        "Processo 1152957 — Rel. Conselheiro Alencar da Silveira Jr."
    )
    # regressão: texto_decisao não pode ser só a linha de citação (~100
    # caracteres) — o parágrafo de "Destaque" que descreve o caso de
    # verdade tem que estar junto (achado real, 2026-08-10)
    assert len(primeira.texto_decisao) > 1000
    assert "Johny Claudy Fernandes" in primeira.texto_decisao
    assert "Município de Caratinga" in primeira.texto_decisao

    por_numero = {d.numero_acordao: d for d in outras}

    # citação normal: "Acórdão N/AAAA Órgão" numa <b> só
    normal = por_numero["2507/2026"]
    assert normal.orgao_julgador == "Primeira Câmara"
    assert normal.relator == "Benjamin Zymler"
    assert normal.url_inteiro_teor is None  # achado real: sem link nesse trecho
    assert normal.identificador_exibicao == "Acórdão 2507/2026 — Rel. Benjamin Zymler"
    assert "aferição da proporcionalidade da multa" in normal.texto_decisao

    # achado real: "Acórdão" e "N/AAAA Órgão" em duas tags <b> separadas
    # (resíduo de exportação do Word) — mesma edição, formato diferente
    com_b_separado = por_numero["1370/2026"]
    assert com_b_separado.orgao_julgador == "Plenário"
    assert com_b_separado.relator == "Jhonatan de Jesus"
    assert com_b_separado.identificador_exibicao == "Acórdão 1370/2026 — Rel. Jhonatan de Jesus"
    assert "Minha Casa Minha Vida" in com_b_separado.texto_decisao


def test_fatiar_tce_mg_variante_2_ancora_envolve_texto_usa_fixture_real():
    # edição 334: cabeçalho de seção é <p><b><a>Nome do Tribunal</a></b></p>
    # (âncora envolve o texto, tudo em negrito) — variante real diferente
    # da 333, mesma citação própria por baixo
    html_texto = (FIXTURES / "tce_mg" / "informativo_334.html").read_text(encoding="utf-8")
    texto_bruto = tce_mg._extrair_conteudo_principal(html_texto)

    decisoes = fatiador._fatiar_tce_mg(item_bruto_id=1, texto_bruto=texto_bruto)

    assert len(decisoes) == 23  # 14 próprias + 9 do TCU

    proprias = [d for d in decisoes if d.tribunal == "TCE-MG"]
    outras = [d for d in decisoes if d.tribunal != "TCE-MG"]
    assert len(proprias) == 14
    assert {d.tribunal for d in outras} == {"TCU"}
    assert all(d.numero_processo is not None for d in proprias)
    assert all(d.numero_acordao is not None for d in outras)
    # regressão: nenhuma decisão pode ficar só com a linha de citação
    assert all(len(d.texto_decisao) > 300 for d in decisoes)


def test_dividir_por_secao_tce_mg_ignora_cabecalho_generico_sem_trocar_tribunal():
    # regressão: "DESTAQUE"/"Ementas por Área Temática" não são nome de
    # tribunal — não podem resetar o tribunal corrente pro meio do caminho
    texto = (
        "<p>conteúdo antes de qualquer cabeçalho (TCE-MG implícito)</p>"
        '<p><a></a>DESTAQUE</p>'
        "<p>ainda TCE-MG, só reorganizado</p>"
        '<p><a></a>Tribunal de Contas da União</p>'
        "<p>agora sim é TCU</p>"
    )
    segmentos = fatiador._dividir_por_secao_tce_mg(texto)
    tribunais = [tribunal for tribunal, _ in segmentos]
    assert tribunais == ["TCE-MG", "TCE-MG", "TCU"]


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
    assert com_link.identificador_exibicao == (
        "Processo AgInt no REsp 2.162.500-RJ — Rel. Benedito Gonçalves"
    )

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
    assert decisao.identificador_exibicao == "Acórdão 2357/2026 — Rel. Bruno Dantas"


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
    # achado real: url_inteiro_teor não precisa ser extraído do texto (ao
    # contrário de tribunal/acórdão/relator) — url_origem já é a fonte
    # original de verdade, não usar isso tirava a Zênite do e-mail sempre
    assert decisao.url_inteiro_teor == itens[0].url_origem
    assert decisao.texto_decisao == itens[0].texto_bruto


# --- dispatcher -------------------------------------------------------------

def test_fatiar_item_fonte_desconhecida_levanta_erro():
    with pytest.raises(ValueError):
        fatiador.fatiar_item("Fonte Desconhecida", 1, "t", "x", "https://x", None)
