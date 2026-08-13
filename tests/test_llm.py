"""Testes de nucleo/llm.py — nenhum chama a API real do Gemini (custaria
quota e não seria reprodutível). O SDK (`google.genai.Client`) é
substituído por um dublê que devolve respostas ou erros pré-definidos, na
ordem em que `generate_content` é chamado."""

import pytest
from google.genai import errors

import nucleo.llm as llm


class _RespostaFalsa:
    def __init__(self, texto):
        self.text = texto


class _ModelsFalso:
    """Substitui `client.models`: cada chamada consome o próximo item da
    fila — uma string vira resposta de sucesso, uma Exception é levantada."""

    def __init__(self, fila):
        self._fila = list(fila)
        self.chamadas = 0

    def generate_content(self, *, model, contents, config):
        self.chamadas += 1
        item = self._fila.pop(0)
        if isinstance(item, Exception):
            raise item
        return _RespostaFalsa(item)


class _ClienteFalso:
    def __init__(self, fila):
        self.models = _ModelsFalso(fila)


def _cliente_gemini(monkeypatch, fila, **kwargs) -> tuple[llm.ClienteGeminiFlash, _ModelsFalso]:
    monkeypatch.setattr(llm.genai, "Client", lambda api_key: _ClienteFalso(fila))
    cliente = llm.ClienteGeminiFlash(
        chave_api="fake", espera_inicial=0.01, **kwargs,
    )
    return cliente, cliente._cliente.models


# --- ClienteGeminiFlash.gerar_json ------------------------------------------

def test_gerar_json_sucesso_na_primeira_tentativa(monkeypatch):
    cliente, modelos = _cliente_gemini(monkeypatch, ['{"relevante": true, "motivo": "x"}'])

    resultado = cliente.gerar_json(instrucoes="i", entrada="e", schema={"type": "object"})

    assert resultado == {"relevante": True, "motivo": "x"}
    assert modelos.chamadas == 1


def test_gerar_json_tenta_de_novo_em_erro_429(monkeypatch):
    erro_429 = errors.ClientError(429, {"error": {"message": "quota excedida"}})
    cliente, modelos = _cliente_gemini(
        monkeypatch, [erro_429, '{"relevante": false, "motivo": "y"}'],
    )

    resultado = cliente.gerar_json(instrucoes="i", entrada="e", schema={})

    assert resultado == {"relevante": False, "motivo": "y"}
    assert modelos.chamadas == 2


def test_gerar_json_tenta_de_novo_em_erro_de_servidor(monkeypatch):
    erro_503 = errors.ServerError(503, {"error": {"message": "indisponível"}})
    cliente, modelos = _cliente_gemini(
        monkeypatch, [erro_503, '{"relevante": true, "motivo": "z"}'],
    )

    resultado = cliente.gerar_json(instrucoes="i", entrada="e", schema={})

    assert resultado["motivo"] == "z"
    assert modelos.chamadas == 2


def test_gerar_json_esgota_tentativas_e_levanta_erro_llm(monkeypatch):
    erro_429 = errors.ClientError(429, {"error": {"message": "quota excedida"}})
    cliente, modelos = _cliente_gemini(
        monkeypatch, [erro_429, erro_429, erro_429], tentativas_max=3,
    )

    with pytest.raises(llm.ErroLLM):
        cliente.gerar_json(instrucoes="i", entrada="e", schema={})

    assert modelos.chamadas == 3


def test_gerar_json_erro_400_nao_transitorio_falha_na_hora(monkeypatch):
    erro_400 = errors.ClientError(400, {"error": {"message": "schema inválido"}})
    cliente, modelos = _cliente_gemini(monkeypatch, [erro_400, "não deveria chegar aqui"])

    with pytest.raises(llm.ErroLLM):
        cliente.gerar_json(instrucoes="i", entrada="e", schema={})

    assert modelos.chamadas == 1  # não tentou de novo


def test_gerar_json_resposta_vazia_levanta_erro_llm(monkeypatch):
    cliente, modelos = _cliente_gemini(monkeypatch, ["", '{"relevante": true, "motivo": "x"}'])

    with pytest.raises(llm.ErroLLM):
        cliente.gerar_json(instrucoes="i", entrada="e", schema={})


# --- valor_ou_none() ---------------------------------------------------------

def test_valor_ou_none_passa_valor_real_sem_tocar():
    assert llm.valor_ou_none("Conselheiro Fulano de Tal") == "Conselheiro Fulano de Tal"


def test_valor_ou_none_none_continua_none():
    assert llm.valor_ou_none(None) is None


def test_valor_ou_none_string_vazia_vira_none():
    assert llm.valor_ou_none("   ") is None


@pytest.mark.parametrize("placeholder", [
    "null", "NULL", "Null",
    "__NULL__",  # achado real (2026-08-13): decisão do TCE/SC no boletim
    # mostrou "Acórdão __NULL__" e "Rel. __NULL__" — numero_acordao,
    # numero_processo e relator vieram todos com essa string do LLM
    "none", "None",
    "n/a", "N/A", "na",
    "ausente", "(ausente)", "Ausente",
    "indisponível", "indisponivel",
])
def test_valor_ou_none_reconhece_variantes_de_placeholder(placeholder):
    # achado real: o modelo já inventou 3 variantes diferentes da mesma
    # ideia ("null", "(ausente)", "__NULL__") em rodadas distintas — em
    # vez de colecionar string exata por string exata, o padrão casa
    # qualquer pontuação/sublinhado em volta de uma palavra-símbolo
    # conhecida de ausência
    assert llm.valor_ou_none(placeholder) is None


def test_valor_ou_none_nao_falso_positivo_em_nome_real_com_na():
    # "na" sozinho é placeholder, mas não pode confundir um nome real que
    # contenha esses caracteres como substring
    assert llm.valor_ou_none("Conselheira Ana Paula") == "Conselheira Ana Paula"


# --- criar_cliente_llm() ------------------------------------------------------

def test_criar_cliente_llm_falta_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    with pytest.raises(llm.ErroConfiguracaoLLM):
        llm.criar_cliente_llm()


def test_criar_cliente_llm_provedor_desconhecido(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "outro-provedor-qualquer")

    with pytest.raises(llm.ErroConfiguracaoLLM):
        llm.criar_cliente_llm()


def test_criar_cliente_llm_usa_modelo_padrao_quando_nao_configurado(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr(llm.genai, "Client", lambda api_key: _ClienteFalso([]))

    cliente = llm.criar_cliente_llm()

    assert isinstance(cliente, llm.ClienteGeminiFlash)
    assert cliente._modelo == llm.MODELO_PADRAO


def test_criar_cliente_llm_respeita_gemini_model_do_ambiente(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setattr(llm.genai, "Client", lambda api_key: _ClienteFalso([]))

    cliente = llm.criar_cliente_llm()

    assert cliente._modelo == "gemini-3.5-flash-lite"
