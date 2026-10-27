"""coleta de dados fiscais em apis públicas."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from municipal.dados import DataError
from municipal.fontes import now


# mesmo formato de fontes.py: tela, agente e testes não precisam saber de qual api veio o dado.
COLETAS = [
    dict(id='siconfi_rgf_pessoal', title='SICONFI - RGF Simplificado, Anexo 01: despesa com pessoal e RCL'),
    dict(id='ibge_populacao', title='IBGE - População residente estimada do município (agregado 6579)'),
    dict(id='ibge_populacao_municipios', title='IBGE - População residente estimada dos municípios (agregado 6579)'),
]

# sem_dados é o único status novo: o SICONFI responde HTTP 200 com items vazio quando não há
# registro, e isso não é falha de consulta. Tratar como indisponivel esconderia a diferença entre
# "a api caiu" e "a api respondeu que não existe esse relatório".
STATUS = ('nao_consultada', 'consultada', 'sem_dados', 'indisponivel')


def fonte(fonte_id: str) -> dict:
    for f in COLETAS:
        if f['id'] == fonte_id:
            return f
    raise KeyError(f'Coleta desconhecida: {fonte_id}.')


def resultado(fonte_id: str, url: str | None, status: str, note: str, valores: dict | None = None,
              conteudo: bytes | None = None, checked_at: str | None = None) -> dict:
    """monta o resultado no contrato único."""
    if status not in STATUS:
        raise ValueError(f'Status de coleta inválido: {status}.')
    f = fonte(fonte_id)
    return dict(id=f['id'], title=f['title'], url=url, status=status,
                checked_at=checked_at if checked_at is not None else (None if status == 'nao_consultada' else now()),
                content_sha256=hashlib.sha256(conteudo).hexdigest() if conteudo is not None else None,
                note=note, valores=valores or {})


def nao_consultadas() -> list[dict]:
    return [resultado(f['id'], None, 'nao_consultada', 'Coleta cadastrada; ainda não houve tentativa.') for f in COLETAS]


def valor_api(valor: Any, rotulo: str) -> float:
    """exige número finito e não negativo vindo da api; não converte texto."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise DataError(f'{rotulo}: esperado número; encontrado {valor!r}.')
    if not math.isfinite(valor) or valor < 0:
        raise DataError(f'{rotulo}: valor deve ser finito e não negativo.')
    return round(float(valor), 2)


def validar_populacao(valor: Any, rotulo: str) -> int:
    """o IBGE devolve a população como texto; aceita só dígitos e exige valor positivo."""
    # [0-9] e não str.isdigit(): isdigit aceita '²' e dígitos de outros alfabetos.
    if not isinstance(valor, str) or not re.fullmatch(r'[0-9]+', valor):
        raise DataError(f'{rotulo}: esperada população em dígitos; encontrado {valor!r}.')
    populacao = int(valor)
    if populacao <= 0:
        raise DataError(f'{rotulo}: população deve ser positiva.')
    return populacao


def validar_codigo_ibge(valor: Any, rotulo: str) -> int:
    """código de município: sete dígitos, em texto (agregados) ou inteiro (siconfi)."""
    texto = str(valor) if isinstance(valor, int) and not isinstance(valor, bool) else valor
    if not isinstance(texto, str) or not re.fullmatch(r'[0-9]{7}', texto):
        raise DataError(f'{rotulo}: código IBGE inválido {valor!r}.')
    return int(texto)


def ler_json(conteudo: bytes, rotulo: str) -> Any:
    """decodifica bytes de uma resposta json; falha vira DataError."""
    if not isinstance(conteudo, bytes):
        raise DataError(f'{rotulo}: esperados bytes da resposta.')
    try:
        return json.loads(conteudo.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as e:
        raise DataError(f'{rotulo}: resposta não é JSON válido ({type(e).__name__}).') from e
