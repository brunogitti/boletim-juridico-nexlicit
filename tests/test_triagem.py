"""Testes de nucleo/triagem.py — cliente LLM falso (não chama a API real,
custaria quota e não seria reprodutível), regra de precedência de
metadados e corte de trecho."""

from pathlib import Path

import coletores.stj as stj
import coletores.tcu as tcu
import nucleo.fatiador as fatiador
from nucleo.fatiador import DecisaoFatiada
from nucleo.llm import ClienteLLM
from nucleo.triagem import (
    TRIBUNAL_NAO_IDENTIFICADO,
    ResultadoTriagem,
    extrair_trecho,
    mesclar_metadados,
    triar,
)

FIXTURES = Path(__file__).parent / "fixtures"


class _ClienteFalso(ClienteLLM):
    def __init__(self, resposta: dict):
        self.resposta = resposta
        self.ultima_chamada: dict | None = None

    def gerar_json(self, *, instrucoes, entrada, schema) -> dict:
        self.ultima_chamada = {
            "instrucoes": instrucoes, "entrada": entrada, "schema": schema,
        }
        return self.resposta


def _decisao_fatiada(**overrides) -> DecisaoFatiada:
    padrao = dict(
        item_bruto_id=1, tribunal=None, numero_acordao=None,
        numero_processo=None, orgao_julgador=None, relator=None,
        data_julgamento=None, url_inteiro_teor=None, texto_decisao="texto",
    )
    padrao.update(overrides)
    return DecisaoFatiada(**padrao)


# --- triar() ----------------------------------------------------------------

def test_triar_monta_entrada_com_titulo_e_trecho():
    cliente = _ClienteFalso({"relevante": True, "motivo": "trata de habilitação técnica"})
    triar(cliente, titulo="Acórdão 123/2026", trecho="Trecho da ementa.")

    assert cliente.ultima_chamada is not None
    assert "Acórdão 123/2026" in cliente.ultima_chamada["entrada"]
    assert "Trecho da ementa." in cliente.ultima_chamada["entrada"]


def test_triar_aceita_resposta_so_com_campos_obrigatorios():
    # schema só exige "relevante" e "motivo" — os demais campos podem vir
    # ausentes do JSON (é assim que o schema representa "null" sem
    # depender de {"type": ["string", "null"]}, que a doc do Gemini
    # registra como inconsistente em response_json_schema)
    cliente = _ClienteFalso({"relevante": False, "motivo": "trata só de pessoal"})
    resultado = triar(cliente, titulo="x", trecho="y")

    assert resultado == ResultadoTriagem(
        relevante=False, motivo="trata só de pessoal", tema=None, tribunal=None,
        numero_acordao=None, numero_identificador=None, relator=None,
        data_julgamento=None, impacto_estimado=None,
    )


def test_triar_le_todos_os_campos_quando_presentes():
    cliente = _ClienteFalso({
        "relevante": True, "motivo": "exigência de atestado técnico",
        "tema": "qualificacao_tecnica", "tribunal": "TCE-PR",
        "numero_acordao": "3190/2025", "relator": "FULANO", "data_julgamento": "2025-11-10",
        "impacto_estimado": "medio",
    })
    resultado = triar(cliente, titulo="x", trecho="y")

    assert resultado.tema == "qualificacao_tecnica"
    assert resultado.impacto_estimado == "medio"


def test_triar_le_numero_identificador_de_edital():
    # achado real (2026-08-11): notícia da Zênite sobre licitação em
    # andamento cita o número do próprio instrumento (Concorrência/Pregão/
    # Edital/Resolução), não um acórdão — o schema tem um campo próprio
    # pra isso, separado de numero_acordao
    cliente = _ClienteFalso({
        "relevante": True, "motivo": "suspensão cautelar de licitação",
        "numero_identificador": "Concorrência Presencial n. 05/2026",
    })
    resultado = triar(cliente, titulo="x", trecho="y")

    assert resultado.numero_identificador == "Concorrência Presencial n. 05/2026"
    assert resultado.numero_acordao is None


def test_triar_trata_string_null_literal_como_ausencia():
    # achado real (2026-08-10): o modelo às vezes devolve a string "null"
    # (4 caracteres) pra um campo fora de "required", em vez de omitir a
    # chave — 2 registros no banco saíram assim antes desse fix
    cliente = _ClienteFalso({
        "relevante": True, "motivo": "x", "tribunal": "null", "numero_acordao": "",
    })
    resultado = triar(cliente, titulo="x", trecho="y")

    assert resultado.tribunal is None
    assert resultado.numero_acordao is None


# --- extrair_trecho() --------------------------------------------------------

def test_extrair_trecho_pega_so_o_primeiro_paragrafo():
    texto = "Primeiro parágrafo, a ementa.\n\nSegundo parágrafo, detalhamento longo."
    assert extrair_trecho(texto) == "Primeiro parágrafo, a ementa."


def test_extrair_trecho_respeita_teto_de_caracteres():
    texto = "a" * 2000
    assert len(extrair_trecho(texto, limite=100)) == 100


def test_extrair_trecho_sem_rotulo_mantem_comportamento_antigo():
    # TCE-PR/TCE-SP/TCE-MG/Zênite não têm seção "Ementa:"/"Tema:" rotulada
    # — não pode regredir pra elas por causa do fix do TCU/STJ
    texto = "Acórdão n.º 3190/2025, Primeira Câmara, Rel. FULANO, julgado em 10/11/2025"
    assert extrair_trecho(texto) == texto


def test_extrair_trecho_tcu_pega_ementa_de_verdade_usando_fixture_real():
    # achado real (2026-08-10, lendo a tabela de descartes): sem esse fix,
    # TCU tinha 100% de descarte — a triagem só via o título repetindo a
    # citação, nunca a Ementa. Usa o mesmo fixture real da Etapa 4/5.
    import csv
    import io

    texto_csv = (FIXTURES / "tcu" / "boletim-informativo-lc.csv").read_text(encoding="utf-8-sig")
    leitor = csv.DictReader(io.StringIO(texto_csv), delimiter="|")
    linha = next(l for l in leitor if l["TEXTOACORDAO"].strip())
    item = tcu._montar_item(linha, "TCU Informativo LC")
    decisao = fatiador._fatiar_tcu(1, item.texto_bruto, item.url_origem)[0]

    trecho = extrair_trecho(decisao.texto_decisao)

    assert "Ementa:" not in trecho  # o rótulo em si não precisa sobrar
    assert "afronta o art. 59" in trecho  # conteúdo real da ementa
    assert "Detalhamento" not in trecho  # isso é trabalho da Camada 5, não da 4


def test_extrair_trecho_stj_pega_tema_de_verdade_usando_fixture_real():
    edicao = stj.EdicaoFeed(
        numero="0887", data_publicacao="2026-05-05T03:00:00+00:00", url="https://x/887",
    )
    html_texto = (FIXTURES / "stj" / "edicao_887.html").read_text(encoding="utf-8")
    itens = stj._extrair_notas_administrativas(html_texto, edicao)
    decisao = fatiador._fatiar_stj(
        1, itens[0].texto_bruto, itens[0].url_origem, itens[0].data_publicacao,
    )[0]

    trecho = extrair_trecho(decisao.texto_decisao)

    assert "Tema:" not in trecho
    assert len(trecho) > len(decisao.texto_decisao.split("\n\n", 1)[0])


# --- mesclar_metadados() ------------------------------------------------------

def test_mesclar_metadados_fatiador_tem_precedencia():
    # TCE-PR: fatiador já extraiu tudo por regex — a triagem não pode
    # sobrescrever, mesmo que devolva algo diferente
    decisao = _decisao_fatiada(
        tribunal="TCE-PR", numero_acordao="3190/2025",
        relator="THIAGO BARBOSA CORDEIRO", data_julgamento="2025-11-10",
    )
    resultado = ResultadoTriagem(
        relevante=True, motivo="x", tema=None, tribunal="TCU",
        numero_acordao="9999/2099", numero_identificador=None,
        relator="OUTRO NOME", data_julgamento="2099-01-01",
        impacto_estimado=None,
    )

    metadados = mesclar_metadados(decisao, resultado)

    assert metadados == {
        "tribunal": "TCE-PR", "numero_acordao": "3190/2025", "numero_processo": None,
        "relator": "THIAGO BARBOSA CORDEIRO", "data_julgamento": "2025-11-10",
    }


def test_mesclar_metadados_usa_triagem_quando_fatiador_deixa_null():
    # Zênite: fatiador deixa tudo None de propósito
    decisao = _decisao_fatiada()
    resultado = ResultadoTriagem(
        relevante=True, motivo="x", tema="dispensa", tribunal="TCE-SP",
        numero_acordao="123/2026", numero_identificador=None,
        relator="FULANA", data_julgamento="2026-01-05",
        impacto_estimado="alto",
    )

    metadados = mesclar_metadados(decisao, resultado)

    assert metadados == {
        "tribunal": "TCE-SP", "numero_acordao": "123/2026", "numero_processo": None,
        "relator": "FULANA", "data_julgamento": "2026-01-05",
    }


def test_mesclar_metadados_usa_numero_identificador_como_numero_processo():
    # achado real (2026-08-11): notícia da Zênite sobre licitação em
    # andamento — fatiador não identifica tribunal (Zênite), e a triagem
    # não tem acórdão pra reportar, só o número do edital/Concorrência.
    # Esse valor reaproveita o slot numero_processo, o mesmo padrão já
    # usado pro número de súmula do TCE-SP em numero_acordao.
    decisao = _decisao_fatiada()
    resultado = ResultadoTriagem(
        relevante=True, motivo="suspensão cautelar de licitação",
        tema="dispensa", tribunal=None, numero_acordao=None,
        numero_identificador="Concorrência Presencial n. 05/2026",
        relator=None, data_julgamento=None, impacto_estimado="medio",
    )

    metadados = mesclar_metadados(decisao, resultado)

    assert metadados["numero_acordao"] is None
    assert metadados["numero_processo"] == "Concorrência Presencial n. 05/2026"


def test_mesclar_metadados_sem_tribunal_em_nenhuma_fonte_usa_sentinela():
    # notícia da Zênite sobre tendência geral — nem o fatiador nem o LLM
    # identificam um tribunal específico; NOT NULL da coluna não pode quebrar
    decisao = _decisao_fatiada()
    resultado = ResultadoTriagem(
        relevante=False, motivo="tendência geral, sem tribunal específico",
        tema=None, tribunal=None, numero_acordao=None, numero_identificador=None,
        relator=None, data_julgamento=None, impacto_estimado=None,
    )

    metadados = mesclar_metadados(decisao, resultado)

    assert metadados["tribunal"] == TRIBUNAL_NAO_IDENTIFICADO


def test_mesclar_metadados_nao_deixa_llm_preencher_campo_isolado():
    # achado real (2026-08-10): TCE-SP e TCE-MG-própria têm tribunal
    # identificado pelo fatiador mas nunca citam por acórdão — antes, o
    # gate era campo a campo, e o LLM "preenchia" numero_acordao com o
    # próprio numero_processo (o único número visível no texto), porque
    # o fatiador tinha deixado só ESSE campo em None. 63% das decisões
    # relevantes do banco saíram com numero_acordao == numero_processo
    # por causa disso. O gate certo é por decisão inteira (tribunal),
    # não por campo: se o fatiador já identificou o tribunal, nenhum dos
    # 4 campos pode vir do LLM, nem os que ele deixou None de propósito.
    decisao = _decisao_fatiada(
        tribunal="TCE-SP", numero_acordao=None, numero_processo="012834.989.25-3",
        relator="Conselheiro Fulano", data_julgamento="2026-03-11",
    )
    resultado = ResultadoTriagem(
        relevante=True, motivo="x", tema=None, tribunal="TCE-SP",
        numero_acordao="012834.989.25-3",  # LLM "inventando" a partir do processo
        numero_identificador=None,
        relator="Conselheiro Fulano", data_julgamento="2026-03-11",
        impacto_estimado=None,
    )

    metadados = mesclar_metadados(decisao, resultado)

    assert metadados["numero_acordao"] is None
    # regressão: numero_processo vem do fatiador (passthrough), nunca de
    # resultado.numero_identificador — esse campo só é fonte de verdade
    # quando decisao.tribunal is None (Zênite), ver teste acima
    assert metadados["numero_processo"] == "012834.989.25-3"
