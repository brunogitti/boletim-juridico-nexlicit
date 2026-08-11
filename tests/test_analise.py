"""Testes de nucleo/analise.py — cliente LLM falso (não chama a API real),
mesmo padrão de tests/test_triagem.py."""

from nucleo.analise import ResultadoAnalise, analisar, recuperar_ancora_do_texto, tem_ancora
from nucleo.llm import ClienteLLM


class _ClienteFalso(ClienteLLM):
    def __init__(self, resposta: dict):
        self.resposta = resposta
        self.ultima_chamada: dict | None = None

    def gerar_json(self, *, instrucoes, entrada, schema) -> dict:
        self.ultima_chamada = {"instrucoes": instrucoes, "entrada": entrada, "schema": schema}
        return self.resposta


# --- analisar() ---------------------------------------------------------

def test_analisar_monta_entrada_com_titulo_e_texto_completo():
    cliente = _ClienteFalso({"resumo": "resumo qualquer"})
    analisar(cliente, titulo="Acórdão 123/2026", texto_completo="Texto completo da decisão.")

    assert cliente.ultima_chamada is not None
    assert "Acórdão 123/2026" in cliente.ultima_chamada["entrada"]
    assert "Texto completo da decisão." in cliente.ultima_chamada["entrada"]


def test_analisar_aceita_resposta_so_com_resumo():
    # schema só exige "resumo" — mesma lógica da triagem (Etapa 5): campo
    # ausente do JSON representa "sem base = null" sem depender de
    # {"type": ["string", "null"]}, inconsistente no response_json_schema
    cliente = _ClienteFalso({"resumo": "Decisão trata de habilitação técnica."})
    resultado = analisar(cliente, titulo="x", texto_completo="y")

    assert resultado == ResultadoAnalise(
        o_que_foi_decidido=None, artigos_lei=None, impacto_pratico=None,
        mudanca_procedimento=None, impacto=None,
        resumo="Decisão trata de habilitação técnica.",
    )


def test_analisar_le_todos_os_campos_quando_presentes():
    cliente = _ClienteFalso({
        "o_que_foi_decidido": "O tribunal julgou irregular a exigência.",
        "artigos_lei": ["art. 67", "art. 92"],
        "impacto_pratico": "Fornecedores não podem mais ser exigidos assim.",
        "mudanca_procedimento": "Revisar editais antes de impugnar.",
        "impacto": "alto",
        "resumo": "Resumo final de até 200 palavras.",
    })
    resultado = analisar(cliente, titulo="x", texto_completo="y")

    assert resultado.artigos_lei == ["art. 67", "art. 92"]
    assert resultado.impacto == "alto"
    assert resultado.o_que_foi_decidido == "O tribunal julgou irregular a exigência."


def test_analisar_artigos_lei_vazio_vira_none():
    cliente = _ClienteFalso({"resumo": "x", "artigos_lei": []})
    resultado = analisar(cliente, titulo="x", texto_completo="y")

    assert resultado.artigos_lei is None


# --- tem_ancora() ---------------------------------------------------------

def test_tem_ancora_com_acordao_e_link():
    assert tem_ancora(
        numero_acordao="123/2026", numero_processo=None, url_inteiro_teor="https://x",
    ) is True


def test_tem_ancora_com_processo_e_link():
    # achado real: TCE-SP/STJ/TCE-MG-própria nunca têm acórdão, só
    # processo — exigir acórdão ao pé da letra excluiria a maioria do
    # boletim (63% das decisões relevantes hoje)
    assert tem_ancora(
        numero_acordao=None, numero_processo="012834.989.25-3", url_inteiro_teor="https://x",
    ) is True


def test_tem_ancora_sem_link_falha_mesmo_com_numero():
    # achado real: as citações de TCU embutidas no informativo do TCE-MG
    # têm número de acórdão mas nenhum link — exatamente o caso que a
    # âncora tem que barrar
    assert tem_ancora(
        numero_acordao="1370/2026", numero_processo=None, url_inteiro_teor=None,
    ) is False


def test_tem_ancora_sem_numero_falha_mesmo_com_link():
    assert tem_ancora(
        numero_acordao=None, numero_processo=None, url_inteiro_teor="https://x",
    ) is False


def test_tem_ancora_sem_os_dois_falha():
    assert tem_ancora(numero_acordao=None, numero_processo=None, url_inteiro_teor=None) is False


# --- recuperar_ancora_do_texto() --------------------------------------------
#
# Achado real (2026-08-11, dashboard de revisão): duas decisões da Zênite
# viraram sem_ancora porque o trecho de 1200 caracteres da triagem não
# alcança o rodapé "Serviço" — que só aparece no fim de artigos de
# ~7500 caracteres — mas o número está lá. Textos abaixo são recortes
# reais dos dois casos (item_bruto_id 119 e 123).

_RODAPE_REAL_119 = """\
A proposta de voto do conselheiro Amaral foi aprovada por unanimidade.

Serviço

Processos nº:
300136/26

Acórdãos nº:
1230/2026 – Tribunal Pleno

Assunto:
Homologação de Recomendações
"""

_RODAPE_REAL_123 = """\
O trânsito em julgado do processo ocorreu 22 de junho.

Serviço

Processo nº:
790460/24

Acórdão nº:
1055/26 – Tribunal Pleno

Assunto:
Representação da Lei de Licitações
"""


def test_recuperar_ancora_acha_os_dois_numeros_no_rodape_real_119():
    numero_acordao, numero_processo = recuperar_ancora_do_texto(_RODAPE_REAL_119)

    assert numero_acordao == "1230/2026"
    assert numero_processo == "300136/26"


def test_recuperar_ancora_acha_os_dois_numeros_no_rodape_real_123():
    numero_acordao, numero_processo = recuperar_ancora_do_texto(_RODAPE_REAL_123)

    assert numero_acordao == "1055/26"
    assert numero_processo == "790460/24"


def test_recuperar_ancora_nao_inclui_orgao_julgador_no_numero():
    # "1230/2026 – Tribunal Pleno" — só o número, o "– Tribunal Pleno" é
    # órgão julgador, não faz parte do numero_acordao em nenhum outro
    # lugar do projeto (padrão já estabelecido no fatiador)
    numero_acordao, _ = recuperar_ancora_do_texto(_RODAPE_REAL_119)
    assert "Tribunal Pleno" not in numero_acordao


def test_recuperar_ancora_sem_rodape_devolve_none_nos_dois():
    texto = "Notícia institucional qualquer, sem citação de processo nenhum."
    numero_acordao, numero_processo = recuperar_ancora_do_texto(texto)

    assert numero_acordao is None
    assert numero_processo is None
