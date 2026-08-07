"""nucleo/dedup.py — Camada 3: calcula a chave que impede a mesma decisão
de entrar duas vezes no boletim (a mesma decisão aparece na Zênite e no
boletim do próprio tribunal, por exemplo).

Só calcula a chave — não decide "já existe?". Isso já é resolvido pelo
índice único `idx_decisoes_dedup` mais o `ON CONFLICT DO NOTHING` de
`nucleo.banco.inserir_decisao` (Etapa 1): inserir com uma chave repetida
devolve `None`.

Fallback de 3 níveis (o documento original só previa 2 — número de acórdão
e, na ausência dele, hash de título+data — mas o fatiador real mostrou que
TCE-MG (decisão própria), TCE-SP (boletim) e STJ não citam por acórdão E TÊM
número de processo preenchido; se dois itens sem acórdão da mesma edição
caíssem direto no hash de título+data, colidiriam — mesmo título, mesma
data — e um dos dois se perderia silenciosamente via ON CONFLICT DO
NOTHING. `numero_processo` entra como nível intermediário pra evitar isso):

1. `numero_acordao` presente → `tribunal|numero_acordao|ano`.
2. Sem acórdão, com `numero_processo` → `tribunal|processo|numero_processo`.
3. Sem os dois (só a Zênite, hoje) → sha256(titulo_normalizado + data_publicacao).
"""

import hashlib
import re

_ANO_DESCONHECIDO = "0000"  # sentinela documentado: só ocorre se nem o
# número do acórdão nem a data de julgamento trouxerem um ano — não visto
# em nenhuma fonte real até agora


def calcular_chave_dedup(
    *,
    tribunal: str,
    numero_acordao: str | None,
    numero_processo: str | None,
    data_julgamento: str | None,
    titulo_item_bruto: str,
    data_publicacao_item_bruto: str | None,
) -> str:
    # normaliza cada componente ANTES de concatenar — normalizar a string
    # já junta com "|" deixaria espaço em volta de um componente (ex.:
    # tribunal="  tce-pr  ") sobrevivendo colado ao separador, porque
    # strip() só limpa as pontas da string inteira, não de cada pedaço
    tribunal_normalizado = _normalizar(tribunal)

    if numero_acordao:
        ano = (
            _extrair_ano_do_numero(numero_acordao)
            or _extrair_ano_de_data(data_julgamento)
            or _ANO_DESCONHECIDO
        )
        return f"{tribunal_normalizado}|{_normalizar(numero_acordao)}|{ano}"

    if numero_processo:
        return f"{tribunal_normalizado}|processo|{_normalizar(numero_processo)}"

    base = _normalizar(titulo_item_bruto) + (data_publicacao_item_bruto or "")
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _extrair_ano_do_numero(numero_acordao: str) -> str | None:
    """"3190/2025" -> "2025". Súmula do TCE-SP ("1", sem barra) não tem
    ano embutido — cai no fallback de data_julgamento."""
    if "/" not in numero_acordao:
        return None
    return numero_acordao.rsplit("/", 1)[-1].strip() or None


def _extrair_ano_de_data(data_iso: str | None) -> str | None:
    """ISO 8601 (data pura "AAAA-MM-DD" ou datetime completo) começa
    sempre com o ano nos 4 primeiros caracteres."""
    if not data_iso or len(data_iso) < 4:
        return None
    return data_iso[:4]


def _normalizar(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip().lower()
