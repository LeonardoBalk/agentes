"""coleta de dados fiscais em apis públicas."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
import statistics
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from municipal.dados import DataError
from municipal.fontes import now


# mesmo formato de fontes.py: tela, agente e testes não precisam saber de qual api veio o dado.
COLETAS = [
    dict(id='siconfi_rgf_pessoal', title='SICONFI - RGF Simplificado, Anexo 01: despesa com pessoal e RCL'),
    dict(id='ibge_populacao', title='IBGE - População residente estimada do município (agregado 6579)'),
    dict(id='ibge_populacao_municipios', title='IBGE - População residente estimada dos municípios (agregado 6579)'),
    dict(id='siconfi_extrato', title='SICONFI - Extrato de entregas: último RGF publicado pela Prefeitura'),
    dict(id='siconfi_rreo_rcl', title='SICONFI - RREO, Anexo 03: RCL dos últimos 12 meses (conferência)'),
    dict(id='benchmark_rcl_per_capita', title='Benchmark - RCL por habitante entre municípios de porte semelhante'),
]

# sem_dados é o único status novo: o SICONFI responde HTTP 200 com items vazio quando não há
# registro, e isso não é falha de consulta. Tratar como indisponivel esconderia a diferença entre
# "a api caiu" e "a api respondeu que não existe esse relatório".
STATUS = ('nao_consultada', 'consultada', 'sem_dados', 'indisponivel')

# lista de municípios: um valor ruim exclui só aquele município. Acima de 10% de perda o benchmark
# deixa de representar a UF (no ES seriam 8 de 78); abaixo de 10 municípios a comparação per capita
# vira anedota, mesmo que a perda seja pequena.
EXCLUSAO_MAXIMA = 0.10
MINIMO_MUNICIPIOS = 10


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
                note=note, valores=valores or {}, desatualizado=False)


# desatualizado=True só aparece em resultado consultada servido do cache depois de uma atualização que
# falhou. Um campo e não um quinto status: o dado continua válido para quem lê só o status, e a tela
# usa o campo para avisar que ele é antigo.
CHAVES_RESULTADO = {'id', 'title', 'url', 'status', 'checked_at', 'content_sha256', 'note', 'valores',
                    'desatualizado'}


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


# ---------------------------------------------------------------- siconfi rgf

ANEXO_PESSOAL = 'RGF-Anexo 01'
COLUNA_VALOR = 'Valor'
COLUNA_PCT = '% sobre a RCL Ajustada'

# campo -> (cod_conta, coluna), conforme a resposta real do RGF Simplificado 2025 (ETAPA1_APIS.md).
# Os limites da LRF são lidos da api, nunca fixados aqui. Os percentuais publicados têm como
# denominador a RCL ajustada, não a RCL: por isso o sufixo _pct_rcl_ajustada.
CAMPOS_RGF = dict(
    rcl=('ReceitaCorrenteLiquidaLimiteLegal', COLUNA_VALOR),
    # rcl menos as transferências que a LRF manda excluir; é a base dos limites de pessoal.
    rcl_ajustada=('ReceitaCorrenteLiquidaAjustada', COLUNA_VALOR),
    despesa_pessoal=('DespesaComPessoalTotal', COLUNA_VALOR),
    limite_maximo=('LimiteMaximoDespesaComPessoalTotal', COLUNA_VALOR),
    limite_maximo_pct_rcl_ajustada=('LimiteMaximoDespesaComPessoalTotal', COLUNA_PCT),
    limite_prudencial=('LimitePrudencialDespesaComPessoalTotal', COLUNA_VALOR),
    limite_prudencial_pct_rcl_ajustada=('LimitePrudencialDespesaComPessoalTotal', COLUNA_PCT),
    limite_alerta=('LimiteDeAlertaDespesaComPessoalTotal', COLUNA_VALOR),
    limite_alerta_pct_rcl_ajustada=('LimiteDeAlertaDespesaComPessoalTotal', COLUNA_PCT),
)
# percentual que o próprio demonstrativo publica; se vier, confere a razão calculada.
CONFERENCIA_RGF = ('DespesaComPessoalTotal', COLUNA_PCT)
TOLERANCIA_PCT = 0.01


def ler_rgf_pessoal(conteudo: bytes, url: str, checked_at: str | None = None) -> dict:
    """RCL, RCL ajustada, despesa com pessoal e limites a partir dos bytes do /rgf. Não baixa nada."""
    def falha(nota):
        return resultado('siconfi_rgf_pessoal', url, 'indisponivel', nota, conteudo=conteudo, checked_at=checked_at)
    try:
        payload = ler_json(conteudo, url)
        if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
            titulo = payload.get('title') if isinstance(payload, dict) else None
            return falha('Resposta sem a lista de itens do SICONFI'
                         + (f' (corpo de erro: {titulo}).' if isinstance(titulo, str) else '.'))
        itens = payload['items']
        if not itens or payload.get('count') == 0:
            return resultado('siconfi_rgf_pessoal', url, 'sem_dados',
                             'O SICONFI respondeu sem registros para estes parâmetros. Confira exercício, período e se o ente publica o demonstrativo simplificado.',
                             conteudo=conteudo, checked_at=checked_at)
        if payload.get('hasMore') is not False:
            # paginação fica para a etapa de rede; sem hasMore=false não dá para garantir a resposta inteira.
            return falha('Resposta truncada pela paginação do SICONFI ou sem o indicador hasMore. Os valores não foram lidos.')

        anexo = [i for i in itens if isinstance(i, dict) and i.get('anexo') == ANEXO_PESSOAL]
        valores, faltantes = {}, []
        for campo, (cod_conta, coluna) in CAMPOS_RGF.items():
            linhas = [i for i in anexo if i.get('cod_conta') == cod_conta and i.get('coluna') == coluna]
            if not linhas:
                faltantes.append(f'{cod_conta} ({coluna})')
                continue
            if len(linhas) > 1:
                return falha(f'{cod_conta} ({coluna}) aparece {len(linhas)} vezes no {ANEXO_PESSOAL}; valor ambíguo.')
            valores[campo] = valor_api(linhas[0].get('valor'), f'{url} {cod_conta} ({coluna})')
        if faltantes:
            return falha(f'Campos obrigatórios ausentes no {ANEXO_PESSOAL}: {", ".join(faltantes)}.')

        periodos = {(i.get('exercicio'), i.get('periodo'), i.get('periodicidade')) for i in anexo}
        if len(periodos) != 1:
            return falha(f'O {ANEXO_PESSOAL} mistura exercícios ou períodos: {sorted(map(str, periodos))}.')
        exercicio, periodo, periodicidade = periodos.pop()
        if (isinstance(exercicio, bool) or not isinstance(exercicio, int) or isinstance(periodo, bool)
                or not isinstance(periodo, int) or periodicidade not in ('Q', 'S')):
            return falha(f'Exercício, período ou periodicidade inválidos: {exercicio!r}, {periodo!r}, {periodicidade!r}.')

        if valores['rcl_ajustada'] == 0:
            return falha('RCL ajustada igual a zero; a razão da despesa com pessoal não pode ser calculada.')
        # denominador: RCL ajustada, a mesma base dos limites publicados no anexo.
        razao = round(valores['despesa_pessoal'] / valores['rcl_ajustada'] * 100, 2)
        publicadas = [i for i in anexo if (i.get('cod_conta'), i.get('coluna')) == CONFERENCIA_RGF]
        if len(publicadas) == 1:
            publicada = valor_api(publicadas[0].get('valor'), f'{url} {CONFERENCIA_RGF[0]} ({CONFERENCIA_RGF[1]})')
            if abs(publicada - razao) > TOLERANCIA_PCT:
                return falha(f'A razão calculada ({razao}%) diverge da publicada no demonstrativo ({publicada}%).')

        valores.update(despesa_pessoal_pct_rcl_ajustada=razao, exercicio=exercicio, periodo=periodo,
                       periodicidade=periodicidade)
        nota = (f'RGF {exercicio}, período {periodo} ({"semestral" if periodicidade == "S" else "quadrimestral"}). '
                'Limites e percentuais lidos do demonstrativo, sobre a RCL ajustada.')
        populacao = {i.get('populacao') for i in anexo}
        if len(populacao) == 1:
            # o SICONFI traz uma população própria, que diverge do IBGE (15830 x 13813 em 2025).
            nota += f' População informada pelo SICONFI ({populacao.pop()}) não é usada; a população vem do IBGE.'
        return resultado('siconfi_rgf_pessoal', url, 'consultada', nota, valores, conteudo, checked_at)
    except DataError as e:
        return falha(str(e))


# ---------------------------------------------------------------- ibge população

AGREGADO_POPULACAO = 6579
VARIAVEL_POPULACAO = '9324'


def series_populacao(payload: Any, rotulo: str) -> list[dict] | None:
    """séries da variável 9324; None quando a api respondeu lista vazia (período sem dado)."""
    if not isinstance(payload, list):
        raise DataError(f'{rotulo}: resposta não é a lista de variáveis do IBGE.')
    if not payload:
        return None
    variaveis = [v for v in payload if isinstance(v, dict) and v.get('id') == VARIAVEL_POPULACAO]
    if len(variaveis) != 1:
        raise DataError(f'{rotulo}: variável {VARIAVEL_POPULACAO} ausente ou repetida na resposta.')
    resultados = variaveis[0].get('resultados')
    if not isinstance(resultados, list):
        raise DataError(f'{rotulo}: variável {VARIAVEL_POPULACAO} sem resultados.')
    series = []
    for r in resultados:
        if not isinstance(r, dict) or not isinstance(r.get('series'), list):
            raise DataError(f'{rotulo}: resultado sem séries.')
        series.extend(r['series'])
    if not all(isinstance(s, dict) and isinstance(s.get('localidade'), dict) and isinstance(s.get('serie'), dict)
               for s in series):
        raise DataError(f'{rotulo}: série sem localidade ou valores.')
    return series or None


def ler_populacao_municipio(conteudo: bytes, url: str, codigo_ibge: int, ano: int,
                            checked_at: str | None = None) -> dict:
    """população de um município num ano, a partir da consulta N6[codigo]."""
    def sem_dados(nota):
        return resultado('ibge_populacao', url, 'sem_dados', nota, conteudo=conteudo, checked_at=checked_at)
    try:
        series = series_populacao(ler_json(conteudo, url), url)
        if series is None:
            return sem_dados('O IBGE respondeu sem resultados para este período.')
        # o nome muda de formato entre consultas ("(ES)" x "- ES"); o município é identificado só pelo id.
        do_municipio = [s for s in series if s['localidade'].get('id') == str(codigo_ibge)]
        if not do_municipio:
            return sem_dados(f'Município {codigo_ibge} ausente da resposta do IBGE.')
        if len(do_municipio) > 1:
            raise DataError(f'{url}: município {codigo_ibge} repetido na resposta.')
        serie = do_municipio[0]
        if str(ano) not in serie['serie']:
            return sem_dados(f'O IBGE não publicou estimativa de {ano} para o município {codigo_ibge}.')
        valores = dict(codigo_ibge=validar_codigo_ibge(serie['localidade']['id'], url), ano=int(ano),
                       populacao=validar_populacao(serie['serie'][str(ano)], f'{url} {codigo_ibge}/{ano}'),
                       nome=serie['localidade'].get('nome'))
        return resultado('ibge_populacao', url, 'consultada',
                         f'População residente estimada de {ano}, agregado {AGREGADO_POPULACAO}, variável {VARIAVEL_POPULACAO}.',
                         valores, conteudo, checked_at)
    except DataError as e:
        return resultado('ibge_populacao', url, 'indisponivel', str(e), conteudo=conteudo, checked_at=checked_at)


def ler_populacao_municipios(conteudo: bytes, url: str, ano: int, checked_at: str | None = None) -> dict:
    """código, nome e população de cada município, a partir de uma consulta como N6[N3[32]]."""
    try:
        series = series_populacao(ler_json(conteudo, url), url)
        if series is None:
            return resultado('ibge_populacao_municipios', url, 'sem_dados',
                             'O IBGE respondeu sem resultados para este período.', conteudo=conteudo, checked_at=checked_at)
        municipios, excluidos, repetidos = {}, [], set()
        for s in series:
            local = s['localidade']
            try:
                nivel = local.get('nivel')
                if not isinstance(nivel, dict) or nivel.get('id') != 'N6':
                    raise DataError(f'localidade {local.get("id")!r} não é município (N6).')
                codigo = validar_codigo_ibge(local.get('id'), url)
                if codigo in municipios or codigo in repetidos:
                    # não há como saber qual das linhas repetidas é a certa: saem todas.
                    if codigo in municipios:
                        del municipios[codigo]
                        excluidos.append(dict(codigo=codigo, motivo='código repetido na resposta.'))
                    repetidos.add(codigo)
                    raise DataError('código repetido na resposta.')
                if str(ano) not in s['serie']:
                    raise DataError(f'sem estimativa de {ano}.')
                municipios[codigo] = dict(codigo_ibge=codigo, nome=local.get('nome'),
                                          populacao=validar_populacao(s['serie'][str(ano)], f'{url} {codigo}/{ano}'))
            except DataError as e:
                excluidos.append(dict(codigo=local.get('id'), motivo=str(e)))
        total = len(municipios) + len(excluidos)
        valores = dict(ano=int(ano), municipios=[municipios[c] for c in sorted(municipios)],
                       total_excluidos=len(excluidos), excluidos=excluidos)
        nota = (f'{len(municipios)} municípios entraram e {len(excluidos)} '
                f'{"foi excluído" if len(excluidos) == 1 else "foram excluídos"} por valor ausente ou inválido, '
                f'população residente estimada de {ano}. Identifique pelo código, não pelo nome.')
        if len(excluidos) > total * EXCLUSAO_MAXIMA or len(municipios) < MINIMO_MUNICIPIOS:
            return resultado('ibge_populacao_municipios', url, 'indisponivel',
                             nota + f' Perda acima do aceitável (máximo de {EXCLUSAO_MAXIMA:.0%} excluídos e '
                             f'mínimo de {MINIMO_MUNICIPIOS} municípios).', conteudo=conteudo, checked_at=checked_at)
        return resultado('ibge_populacao_municipios', url, 'consultada', nota, valores, conteudo, checked_at)
    except DataError as e:
        return resultado('ibge_populacao_municipios', url, 'indisponivel', str(e), conteudo=conteudo, checked_at=checked_at)


# ---------------------------------------------------------------- rede

TIMEOUT = 20
# uma página cheia do SICONFI tem até 5.000 itens; no RREO real foram ~400 bytes por item, o que dá
# ~2 MB por página. 4 MB deixa o dobro de folga; mais que isso não é resposta que esperamos.
TETO_BYTES = 4_000_000
# o fontes.py se passa por navegador porque o planalto.gov.br exige. SICONFI e IBGE responderam ao
# curl na etapa 1, então aqui o app se identifica. Accept-Language é o mesmo do fontes.py.
CABECALHOS = {'User-Agent': 'LeopoldinAI/1.0 (coleta de dados fiscais)',
              'Accept-Language': 'pt-BR,pt;q=0.9', 'Accept': 'application/json',
              'Accept-Encoding': 'gzip'}


class FalhaRede(Exception):
    """falha antes de haver resposta HTTP utilizável."""
    def __init__(self, motivo: str, passageira: bool):
        super().__init__(motivo)
        self.passageira = passageira


def baixar(url: str) -> tuple[int, bytes]:
    """status HTTP e bytes brutos da resposta, inclusive em 4xx/5xx; falha de rede vira FalhaRede."""
    try:
        try:
            with urlopen(Request(url, headers=CABECALHOS), timeout=TIMEOUT) as resposta:
                status, conteudo = resposta.status, resposta.read(TETO_BYTES + 1)
                codificacao = (resposta.headers.get('Content-Encoding') or '').strip().lower()
        except HTTPError as e:
            # urllib levanta exceção em 4xx/5xx; o código decide a repetição, o corpo é só complemento.
            status = e.code
            try:
                conteudo = e.read(TETO_BYTES + 1)
            except (OSError, ValueError, http.client.HTTPException):
                conteudo = b''
            finally:
                e.close()
            # corpo de erro não é lido por ninguém; não vale descompactar.
            codificacao = ''
    except (OSError, http.client.HTTPException) as e:
        # OSError cobre timeout, conexão recusada e DNS; URLError é subclasse e guarda a causa em reason.
        causa = e.reason if isinstance(getattr(e, 'reason', None), BaseException) else e
        raise FalhaRede(f'falha de rede ({type(causa).__name__})', passageira=True) from e
    if len(conteudo) > TETO_BYTES:
        raise FalhaRede(f'resposta maior que {TETO_BYTES // 1_000_000} MB', passageira=False)
    return status, descompactar(conteudo, codificacao)


def descompactar(conteudo: bytes, codificacao: str) -> bytes:
    """o IBGE manda gzip mesmo sem Accept-Encoding (visto na lista da UF); o teto vale para o descompactado."""
    if codificacao in ('', 'identity'):
        return conteudo
    if codificacao != 'gzip':
        raise FalhaRede(f'codificação de resposta não suportada ({codificacao})', passageira=False)
    leitor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        bruto = leitor.decompress(conteudo, TETO_BYTES + 1)
    except zlib.error as e:
        raise FalhaRede('resposta gzip corrompida', passageira=False) from e
    if len(bruto) > TETO_BYTES or leitor.unconsumed_tail:
        raise FalhaRede(f'resposta descompactada maior que {TETO_BYTES // 1_000_000} MB', passageira=False)
    return bruto


# mesmo padrão e mesmos valores de agente.py (TENTATIVAS, ESPERAS), mas sem reaproveitar o código: lá a
# classificação lê o texto do erro do provedor de LLM e a espera não é injetável.
TENTATIVAS = 3
ESPERAS = (5, 15)


class FalhaColeta(Exception):
    """a busca desistiu; a nota já está pronta para o resultado."""
    def __init__(self, nota: str, tentativas: int):
        super().__init__(nota)
        self.tentativas = tentativas


def classificar_status(status: int) -> tuple[str, bool]:
    """mensagem e se vale tentar de novo. 5xx e 429 passam; o resto de 4xx não se resolve sozinho."""
    if status == 429:
        return 'HTTP 429 (limite de requisições do servidor)', True
    if status >= 500:
        return f'HTTP {status} (erro no servidor)', True
    return f'HTTP {status} (requisição recusada)', False


def buscar(url: str, esperar: Callable[[float], None] = time.sleep,
           antes: Callable[[], None] | None = None) -> bytes:
    """bytes de uma resposta 2xx, repetindo só falha passageira. antes() roda antes de cada tentativa."""
    for tentativa in range(1, TENTATIVAS + 1):
        if antes:
            antes()
        try:
            status, conteudo = baixar(url)
            if 200 <= status < 300:
                return conteudo
            motivo, passageira = classificar_status(status)
        except FalhaRede as e:
            motivo, passageira = str(e), e.passageira
        if not passageira or tentativa == TENTATIVAS:
            # o 500 do IBGE para município inexistente é permanente, mas só se sabe depois das tentativas.
            plural = 'tentativa' if tentativa == 1 else 'tentativas'
            raise FalhaColeta(f'Consulta não concluída após {tentativa} {plural}: {motivo}.', tentativa)
        esperar(ESPERAS[tentativa - 1])
    raise AssertionError('inalcançável')


# "Para fins de performance, o limite é de uma (01) requisição por segundo" (siconfi.yaml). O intervalo
# é medido entre inícios de requisição aqui; 0,1 s de folga cobre a variação de latência até o servidor.
INTERVALO_SICONFI = 1.1


class Ritmo:
    """intervalo mínimo entre requisições a um host, seguro entre threads do ThreadingHTTPServer."""
    def __init__(self, intervalo: float):
        self.intervalo = intervalo
        self.trava = threading.Lock()
        self.ultima = None

    def aguardar(self, relogio: Callable[[], float] = time.monotonic,
                 esperar: Callable[[float], None] = time.sleep) -> None:
        with self.trava:
            if self.ultima is not None:
                falta = self.intervalo - (relogio() - self.ultima)
                if falta > 0:
                    esperar(falta)
            self.ultima = relogio()


RITMO_SICONFI = Ritmo(INTERVALO_SICONFI)



# ---------------------------------------------------------------- paginação do siconfi

SICONFI_BASE = 'https://apidatalake.tesouro.gov.br/ords/cdwhprd/siconfi/tt/'
# o RGF inteiro de Santa Leopoldina tem 371 itens e cabe numa página de 5.000. Dez páginas são 50.000
# itens, muito acima de qualquer demonstrativo de um ente; passar disso indica hasMore preso em true.
TETO_PAGINAS = 10


def url_siconfi(recurso: str, parametros: dict) -> str:
    """sempre a partir da base pública; links[].href da resposta aponta para host interno da Oracle."""
    return SICONFI_BASE + recurso + '?' + urlencode(parametros, quote_via=quote)


def pagina_json(conteudo: bytes) -> Any:
    """envelope da página, ou None quando o corpo não é JSON (o leitor recusa depois, com a nota certa)."""
    try:
        return json.loads(conteudo.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return None


def tem_mais(pagina: Any) -> bool:
    return isinstance(pagina, dict) and pagina.get('hasMore') is True and isinstance(pagina.get('items'), list)


def inteiro_envelope(pagina: dict, nome: str, paginas: int) -> int:
    valor = pagina.get(nome)
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
        raise FalhaColeta(f'Paginação do SICONFI com {nome} inválido: {valor!r}.', paginas)
    return valor


def buscar_siconfi(recurso: str, parametros: dict, esperar: Callable[[float], None] = time.sleep,
                   relogio: Callable[[], float] = time.monotonic) -> tuple[str, bytes]:
    """url da primeira página e bytes de um envelope único com hasMore false.

    Página única: os bytes originais, sem alteração. Várias páginas: os items somados num envelope
    remontado (o sha256 do resultado passa a ser o desse envelope). offset e limit não estão no
    siconfi.yaml; os nomes vêm do link next da própria resposta (ver ETAPA3_REDE.md).
    """
    def antes():
        RITMO_SICONFI.aguardar(relogio, esperar)

    url = url_siconfi(recurso, parametros)
    conteudo = buscar(url, esperar, antes)
    pagina = pagina_json(conteudo)
    if not tem_mais(pagina):
        return url, conteudo

    itens, paginas = [], 1
    while tem_mais(pagina):
        if paginas >= TETO_PAGINAS:
            raise FalhaColeta(f'O SICONFI continuou indicando mais páginas depois de {TETO_PAGINAS}; '
                              'a consulta foi interrompida para não entrar em laço.', paginas)
        limite = inteiro_envelope(pagina, 'limit', paginas)
        if limite == 0:
            raise FalhaColeta('Paginação do SICONFI com limit zero.', paginas)
        proximo = inteiro_envelope(pagina, 'offset', paginas) + limite
        itens += pagina['items']
        pagina = pagina_json(buscar(url_siconfi(recurso, {**parametros, 'offset': proximo, 'limit': limite}),
                                    esperar, antes))
        paginas += 1
    if not isinstance(pagina, dict) or not isinstance(pagina.get('items'), list):
        raise FalhaColeta(f'A página {paginas} do SICONFI não trouxe a lista de itens.', paginas)
    itens += pagina['items']
    envelope = dict(items=itens, hasMore=False, limit=len(itens), offset=0, count=len(itens))
    return url, json.dumps(envelope, ensure_ascii=False).encode('utf-8')



# ---------------------------------------------------------------- cache

# o RGF é semestral e o publicado só muda por retificação: uma semana pega uma retificação sem
# repetir a consulta a cada análise. A estimativa do IBGE sai uma vez por ano (a de 2025 foi
# atualizada em 28/08/2026, segundo /agregados/6579/periodos); 30 dias basta.
# o extrato é o que mais muda: cada entrega ou retificação nova aparece nele. São 3 consultas
# pequenas; 1 dia faz a escolha do período acompanhar uma publicação nova no dia seguinte.
VALIDADE = dict(siconfi_rgf_pessoal=timedelta(days=7), ibge_populacao=timedelta(days=30),
                ibge_populacao_municipios=timedelta(days=30), siconfi_extrato=timedelta(days=1),
                siconfi_rreo_rcl=timedelta(days=7), benchmark_rcl_per_capita=timedelta(days=7))
# depois de uma falha, não tentar de novo antes disso. Cada falha completa custa até ~80 s (3 tentativas
# com timeout de 20 s mais esperas de 5 e 15 s): 5 min evita que dois cliques seguidos em "atualizar"
# paguem isso duas vezes, e é curto o bastante para a coleta voltar sozinha logo depois que a rede
# voltar. Para tentar na hora, o botão de atualizar passa forcar=True.
RECUO = timedelta(minutes=5)
CHAVES_ENTRADA = {'chave', 'ultimo_bom', 'ultimo_bom_em', 'ultima_tentativa', 'ultima_tentativa_em'}
MUNICIPIO_PADRAO = 3204500   # Santa Leopoldina, confirmado na API de localidades do IBGE (ETAPA1_APIS.md)
UF_PADRAO = 32               # Espírito Santo

# uma coleta por vez no processo: o servidor é ThreadingHTTPServer, e duas análises simultâneas não
# podem gravar o cache ao mesmo tempo nem disparar a mesma consulta duas vezes.
TRAVA_COLETA = threading.Lock()


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def instante(texto: Any) -> datetime:
    momento = datetime.fromisoformat(texto) if isinstance(texto, str) else None
    if momento is None or momento.tzinfo is None:
        raise ValueError(f'Instante inválido no cache: {texto!r}.')
    return momento


def entrada_valida(fonte_id: str, entrada: Any) -> bool:
    if not isinstance(entrada, dict) or set(entrada) != CHAVES_ENTRADA or not isinstance(entrada['chave'], str):
        return False
    for chave in ('ultimo_bom', 'ultima_tentativa'):
        r = entrada[chave]
        if r is not None and (not isinstance(r, dict) or set(r) != CHAVES_RESULTADO
                              or r['id'] != fonte_id or r['status'] not in STATUS):
            return False
    if entrada['ultima_tentativa'] is None or (entrada['ultimo_bom'] is None) != (entrada['ultimo_bom_em'] is None):
        return False
    if entrada['ultimo_bom'] is not None and entrada['ultimo_bom']['status'] != 'consultada':
        return False
    try:
        for chave in ('ultimo_bom_em', 'ultima_tentativa_em'):
            if entrada[chave] is not None:
                instante(entrada[chave])
    except ValueError:
        return False
    return True


def ler_cache(cache: Path) -> dict:
    """entradas do cache; arquivo ausente, corrompido ou com formato estranho vira cache vazio."""
    try:
        dados = json.loads(cache.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    # mesma ideia do source_status: qualquer id ou entrada fora do esperado invalida o arquivo todo.
    if not isinstance(dados, dict) or not set(dados) <= {f['id'] for f in COLETAS}:
        return {}
    if not all(entrada_valida(fonte_id, entrada) for fonte_id, entrada in dados.items()):
        return {}
    return dados


def gravar_cache(cache: Path, dados: dict) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    temp = cache.with_suffix('.tmp')
    temp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(cache)


def coletar(cache: Path, fonte_id: str, chave: str, obter: Callable[[str], dict],
            agora: Callable[[], datetime] = agora_utc, forcar: bool = False, somente_cache: bool = False) -> dict:
    """devolve o valor do cache se ainda vale; senão consulta com obter(checked_at) e grava.

    chave identifica a consulta: a URL, quando há uma só, ou a composição das consultas.
    somente_cache=True nunca consulta a rede nem espera a trava: é o caminho da tela inicial.
    Logo depois de uma falha (RECUO), não consulta: devolve o último valor bom como desatualizado,
    ou a própria falha. forcar=True ignora o recuo, para o botão de atualizar manual; ele não
    ignora a validade, porque valor dentro da validade não precisa de rede.
    """
    if somente_cache:
        return do_cache(cache, fonte_id, chave, agora())
    with TRAVA_COLETA:
        dados = ler_cache(cache)
        entrada = dados.get(fonte_id)
        if entrada and entrada['chave'] != chave:
            # outra consulta (outro exercício ou município): o valor guardado não responde a esta.
            entrada = None
        momento = agora()
        bom = entrada['ultimo_bom'] if entrada else None
        bom_em = entrada['ultimo_bom_em'] if entrada else None
        if bom and timedelta(0) <= momento - instante(bom_em) < VALIDADE[fonte_id]:
            return dict(bom)
        ultima = entrada['ultima_tentativa'] if entrada else None
        if not forcar and ultima and ultima['status'] != 'consultada':
            desde = momento - instante(entrada['ultima_tentativa_em'])
            if timedelta(0) <= desde < RECUO:
                aviso = (f'Nova tentativa automática em {descrever_idade(RECUO - desde)}; '
                         'o botão de atualizar tenta na hora.')
                if bom:
                    return valor_velho(bom, ultima, momento - instante(bom_em), aviso)
                # sem valor bom: a própria falha, sem rede. Se ela foi sem_dados, continua sem_dados.
                return {**ultima, 'note': f"{ultima['note']} {aviso}"}
        novo = obter(momento.isoformat())
        if novo['status'] == 'consultada':
            bom, bom_em = novo, momento.isoformat()
        dados[fonte_id] = dict(chave=chave, ultimo_bom=bom, ultimo_bom_em=bom_em,
                               ultima_tentativa=novo, ultima_tentativa_em=momento.isoformat())
        gravar_cache(cache, dados)
        if novo['status'] == 'consultada' or bom is None:
            return novo
        return valor_velho(bom, novo, momento - instante(bom_em))


def do_cache(cache: Path, fonte_id: str, chave: str, momento: datetime) -> dict:
    """o que o cache tem para esta consulta, sem rede e sem trava (a gravação é atômica)."""
    entrada = ler_cache(cache).get(fonte_id)
    if not entrada or entrada['chave'] != chave:
        return resultado(fonte_id, None, 'nao_consultada', 'Ainda não coletado. Use o botão de coletar.')
    bom, ultima = entrada['ultimo_bom'], entrada['ultima_tentativa']
    if not bom:
        return dict(ultima)
    idade = momento - instante(entrada['ultimo_bom_em'])
    if timedelta(0) <= idade < VALIDADE[fonte_id]:
        return dict(bom)
    if ultima['status'] != 'consultada':
        return valor_velho(bom, ultima, idade)
    return {**bom, 'desatualizado': True,
            'note': f"{bom['note']} Valor coletado há {descrever_idade(idade)}; a validade de "
                    f"{VALIDADE[fonte_id].days} dias venceu e a tela não consulta a rede sozinha. Use o botão de coletar."}


def descrever_idade(idade: timedelta) -> str:
    if idade.days >= 1:
        return f'{idade.days} dia' + ('s' if idade.days > 1 else '')
    horas = idade.seconds // 3600
    if horas:
        return f'{horas} hora' + ('s' if horas > 1 else '')
    return f'{max(idade.seconds // 60, 1)} min'


def valor_velho(bom: dict, falha: dict, idade: timedelta, aviso: str = '') -> dict:
    """último valor bom, marcado como desatualizado, com a idade e o motivo da falha na nota."""
    nota = (f"{bom['note']} Valor coletado há {descrever_idade(idade)}; a atualização falhou "
            f"({falha['status']}): {falha['note']}")
    return {**bom, 'desatualizado': True, 'note': f'{nota} {aviso}'.strip()}


def obter_por(fonte_id: str, url: str, baixar_bytes: Callable[[], bytes],
              ler: Callable[[bytes, str], dict]) -> Callable[[str], dict]:
    """liga a busca ao leitor; falha de rede vira indisponivel com a nota da tentativa."""
    def obter(checked_at: str) -> dict:
        try:
            conteudo = baixar_bytes()
        except FalhaColeta as e:
            return resultado(fonte_id, url, 'indisponivel', str(e), checked_at=checked_at)
        return ler(conteudo, checked_at)
    return obter


def coletar_rgf_pessoal(cache: Path, exercicio: int, periodo: int, id_ente: int = MUNICIPIO_PADRAO,
                        periodicidade: str = 'S', demonstrativo: str = 'RGF Simplificado',
                        esperar: Callable[[float], None] = time.sleep, relogio: Callable[[], float] = time.monotonic,
                        agora: Callable[[], datetime] = agora_utc, forcar: bool = False, somente_cache: bool = False) -> dict:
    """RGF-Anexo 01 do Executivo. Santa Leopoldina publica o simplificado semestral (ETAPA1_APIS.md)."""
    parametros = dict(an_exercicio=exercicio, in_periodicidade=periodicidade, nr_periodo=periodo,
                      co_tipo_demonstrativo=demonstrativo, no_anexo=ANEXO_PESSOAL, co_esfera='M',
                      co_poder='E', id_ente=id_ente)
    url = url_siconfi('rgf', parametros)
    obter = obter_por('siconfi_rgf_pessoal', url,
                      lambda: buscar_siconfi('rgf', parametros, esperar, relogio)[1],
                      lambda conteudo, checked_at: ler_rgf_pessoal(conteudo, url, checked_at))
    return coletar(cache, 'siconfi_rgf_pessoal', url, obter, agora, forcar, somente_cache)


def url_populacao(ano: int, localidades: str) -> str:
    return (f'https://servicodados.ibge.gov.br/api/v3/agregados/{AGREGADO_POPULACAO}/periodos/{ano}'
            f'/variaveis/{VARIAVEL_POPULACAO}?localidades={quote(localidades, safe="")}')


def coletar_populacao_municipio(cache: Path, ano: int, codigo_ibge: int = MUNICIPIO_PADRAO,
                                esperar: Callable[[float], None] = time.sleep,
                                agora: Callable[[], datetime] = agora_utc, forcar: bool = False, somente_cache: bool = False) -> dict:
    url = url_populacao(ano, f'N6[{codigo_ibge}]')
    obter = obter_por('ibge_populacao', url, lambda: buscar(url, esperar),
                      lambda conteudo, checked_at: ler_populacao_municipio(conteudo, url, codigo_ibge, ano, checked_at))
    return coletar(cache, 'ibge_populacao', url, obter, agora, forcar, somente_cache)


def coletar_populacao_municipios(cache: Path, ano: int, uf: int = UF_PADRAO,
                                 esperar: Callable[[float], None] = time.sleep,
                                 agora: Callable[[], datetime] = agora_utc, forcar: bool = False, somente_cache: bool = False) -> dict:
    url = url_populacao(ano, f'N6[N3[{uf}]]')
    obter = obter_por('ibge_populacao_municipios', url, lambda: buscar(url, esperar),
                      lambda conteudo, checked_at: ler_populacao_municipios(conteudo, url, ano, checked_at))
    return coletar(cache, 'ibge_populacao_municipios', url, obter, agora, forcar, somente_cache)



# ---------------------------------------------------------------- escolha do período

# nomes de entregável observados nos extratos reais: Santa Leopoldina entrega o simplificado
# semestral; Brejetuba, o RGF comum quadrimestral (ETAPA4_NEGOCIO.md).
ENTREGAVEIS_RGF = {'Relatório de Gestão Fiscal Simplificado': 'RGF Simplificado',
                   'Relatório de Gestão Fiscal': 'RGF'}
ULTIMO_PERIODO = dict(S=2, Q=3)
# a spec só define HO (homologado) e RE (retificado) em status_relatorio. Retificado continua publicado e
# costuma ser mais correto que o original, então os dois contam como entregues.
STATUS_ENTREGUE = ('HO', 'RE')
NOME_STATUS = dict(HO='homologado', RE='retificado')
# o exercício corrente costuma estar incompleto: consulta ele e os dois anteriores.
EXERCICIOS_ATRAS = 2


def itens_extrato(conteudo: bytes, url: str) -> list:
    payload = ler_json(conteudo, url)
    if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
        raise DataError(f'{url}: resposta sem a lista de itens do extrato.')
    if payload.get('hasMore') is not False:
        raise DataError(f'{url}: extrato truncado ou sem o indicador hasMore.')
    return [i for i in payload['items'] if isinstance(i, dict)]


def entregas_rgf(itens: list, id_ente: int) -> list[dict]:
    """RGFs homologados do Poder Executivo; o extrato não tem campo de poder, só o nome da instituição."""
    entregas = []
    for i in itens:
        exercicio, periodo, periodicidade = i.get('exercicio'), i.get('periodo'), i.get('periodicidade')
        if (i.get('entregavel') in ENTREGAVEIS_RGF and i.get('status_relatorio') in STATUS_ENTREGUE
                and i.get('cod_ibge') == id_ente and str(i.get('instituicao', '')).startswith('Prefeitura')
                and periodicidade in ULTIMO_PERIODO and not isinstance(exercicio, bool) and isinstance(exercicio, int)
                and not isinstance(periodo, bool) and isinstance(periodo, int)
                and 1 <= periodo <= ULTIMO_PERIODO[periodicidade]):
            entregas.append(dict(exercicio=exercicio, periodo=periodo, periodicidade=periodicidade,
                                 demonstrativo=ENTREGAVEIS_RGF[i['entregavel']], data_status=i.get('data_status'),
                                 status_relatorio=i['status_relatorio']))
    return entregas


def nome_periodo(periodo: int, periodicidade: str) -> str:
    return f'{periodo}º ' + ('semestre' if periodicidade == 'S' else 'quadrimestre')


def escolher_periodo(entregas: list[dict]) -> tuple[dict | None, dict | None]:
    """(último exercício fechado, período mais recente publicado).

    Exercício fechado = o último período do ano homologado. A RCL dele cobre o ano civil inteiro, o
    que permite comparar com os pares e com a planilha anual; um período parcial mais recente fica
    registrado para a tela, mas não é usado no cálculo.
    """
    if not entregas:
        return None, None
    ordem = lambda e: (e['exercicio'], e['periodo'] / ULTIMO_PERIODO[e['periodicidade']], e['data_status'] or '')
    fechados = [e for e in entregas if e['periodo'] == ULTIMO_PERIODO[e['periodicidade']]]
    return (max(fechados, key=ordem) if fechados else None), max(entregas, key=ordem)


def coletar_periodo_rgf(cache: Path, id_ente: int = MUNICIPIO_PADRAO, ano: int | None = None,
                        esperar: Callable[[float], None] = time.sleep, relogio: Callable[[], float] = time.monotonic,
                        agora: Callable[[], datetime] = agora_utc, forcar: bool = False, somente_cache: bool = False) -> dict:
    """último RGF de exercício fechado, homologado ou retificado, da Prefeitura, pelo /extrato_entregas."""
    ano = ano if ano is not None else agora().year
    anos = list(range(ano, ano - EXERCICIOS_ATRAS - 1, -1))
    urls = {a: url_siconfi('extrato_entregas', dict(id_ente=id_ente, an_referencia=a)) for a in anos}

    def obter(checked_at: str) -> dict:
        itens, falhas, corpos = [], [], []
        for a in anos:
            try:
                conteudo = buscar_siconfi('extrato_entregas', dict(id_ente=id_ente, an_referencia=a), esperar, relogio)[1]
                itens += itens_extrato(conteudo, urls[a])
                corpos.append(conteudo)
            except (FalhaColeta, DataError) as e:
                falhas.append(f'{a}: {e}')
        # sha256 da concatenação das respostas, na ordem dos exercícios consultados.
        conteudo = b''.join(corpos) if corpos else None
        if len(falhas) == len(anos):
            return resultado('siconfi_extrato', urls[ano], 'indisponivel',
                             'Nenhum extrato de entregas pôde ser lido. ' + ' '.join(falhas), checked_at=checked_at)
        escolhido, recente = escolher_periodo(entregas_rgf(itens, id_ente))
        aviso = f" Exercícios sem resposta: {' '.join(falhas)}" if falhas else ''
        if escolhido is None:
            return resultado('siconfi_extrato', urls[ano], 'sem_dados',
                             f'Nenhum RGF de exercício fechado homologado ou retificado pela Prefeitura em {anos[-1]}–{anos[0]}.' + aviso,
                             conteudo=conteudo, checked_at=checked_at)
        nota = (f"Último RGF de exercício fechado publicado pela Prefeitura: {escolhido['exercicio']}, "
                f"{nome_periodo(escolhido['periodo'], escolhido['periodicidade'])} ({escolhido['demonstrativo']}), "
                f"{NOME_STATUS[escolhido['status_relatorio']]} em {(escolhido['data_status'] or '')[:10]}.")
        if recente != escolhido:
            nota += (f" Há período mais recente publicado ({recente['exercicio']}, "
                     f"{nome_periodo(recente['periodo'], recente['periodicidade'])}), ainda sem o ano fechado; "
                     'ele não é usado para manter a comparação com os pares e com a planilha anual.')
        valores = dict(escolhido, mais_recente_publicado=recente, exercicios_consultados=anos)
        return resultado('siconfi_extrato', urls[ano], 'consultada', nota + aviso, valores, conteudo, checked_at)

    return coletar(cache, 'siconfi_extrato', ' '.join(urls[a] for a in anos), obter, agora, forcar, somente_cache)


ANEXO_RCL_RREO = 'RREO-Anexo 03'
CAMPO_RCL_RREO = ('RREO3ReceitaCorrenteLiquida', 'TOTAL (ÚLTIMOS 12 MESES)')
# bimestre do RREO que fecha no mesmo mês que o período do RGF; as duas RCL cobrem os mesmos 12 meses.
BIMESTRE_DO_RGF = {('S', 1): 3, ('S', 2): 6, ('Q', 1): 2, ('Q', 2): 4, ('Q', 3): 6}
# os dois demonstrativos saem da mesma contabilidade e da mesma janela de 12 meses: diferença acima de
# centavos de arredondamento indica que um deles foi retificado e o outro não.
TOLERANCIA_RCL = 1.00


def ler_rreo_rcl(conteudo: bytes, url: str, checked_at: str | None = None) -> dict:
    """RCL dos últimos 12 meses do RREO-Anexo 03, a partir dos bytes do /rreo."""
    def falha(nota):
        return resultado('siconfi_rreo_rcl', url, 'indisponivel', nota, conteudo=conteudo, checked_at=checked_at)
    try:
        payload = ler_json(conteudo, url)
        if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
            return falha('Resposta sem a lista de itens do SICONFI.')
        if not payload['items'] or payload.get('count') == 0:
            return resultado('siconfi_rreo_rcl', url, 'sem_dados', 'O SICONFI respondeu sem registros do RREO para estes parâmetros.',
                             conteudo=conteudo, checked_at=checked_at)
        if payload.get('hasMore') is not False:
            return falha('Resposta do RREO truncada ou sem o indicador hasMore.')
        linhas = [i for i in payload['items'] if isinstance(i, dict) and i.get('anexo') == ANEXO_RCL_RREO
                  and (i.get('cod_conta'), i.get('coluna')) == CAMPO_RCL_RREO]
        if len(linhas) != 1:
            return falha(f'{CAMPO_RCL_RREO[0]} ({CAMPO_RCL_RREO[1]}) aparece {len(linhas)} vezes no {ANEXO_RCL_RREO}.')
        linha = linhas[0]
        valores = dict(rcl=valor_api(linha.get('valor'), f'{url} {CAMPO_RCL_RREO[0]} ({CAMPO_RCL_RREO[1]})'),
                       exercicio=linha.get('exercicio'), periodo=linha.get('periodo'))
        return resultado('siconfi_rreo_rcl', url, 'consultada',
                         f"RREO {valores['exercicio']}, {valores['periodo']}º bimestre: RCL dos últimos 12 meses.",
                         valores, conteudo, checked_at)
    except DataError as e:
        return falha(str(e))


def coletar_rreo_rcl(cache: Path, exercicio: int, bimestre: int, id_ente: int = MUNICIPIO_PADRAO,
                     demonstrativo: str = 'RREO Simplificado', esperar: Callable[[float], None] = time.sleep,
                     relogio: Callable[[], float] = time.monotonic, agora: Callable[[], datetime] = agora_utc,
                     forcar: bool = False, somente_cache: bool = False) -> dict:
    parametros = dict(an_exercicio=exercicio, nr_periodo=bimestre, co_tipo_demonstrativo=demonstrativo,
                      no_anexo=ANEXO_RCL_RREO, co_esfera='M', id_ente=id_ente)
    url = url_siconfi('rreo', parametros)
    obter = obter_por('siconfi_rreo_rcl', url, lambda: buscar_siconfi('rreo', parametros, esperar, relogio)[1],
                      lambda conteudo, checked_at: ler_rreo_rcl(conteudo, url, checked_at))
    return coletar(cache, 'siconfi_rreo_rcl', url, obter, agora, forcar, somente_cache)


def reais(valor: float) -> str:
    """R$ no formato brasileiro, para notas que vão para a tela e para o relatório."""
    return 'R$ ' + f'{valor:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')


def conferir_rcl(rgf: dict, rreo: dict) -> dict:
    """acrescenta ao RGF a conferência da RCL pelo RREO, sem mudar o status nem os valores do RGF.

    Assimetria proposital com a conferência da razão (ler_rgf_pessoal): lá os dois números vêm da mesma
    tabela do mesmo demonstrativo, e divergência quer dizer que a leitura pegou a linha errada, então o
    dado é descartado. Aqui são duas entregas diferentes, homologadas em datas diferentes; uma pode ter
    sido retificada e a outra não. O RGF é o demonstrativo do limite de pessoal e continua sendo a
    fonte; a divergência fica registrada para o usuário decidir.
    """
    rcl = rgf['valores']['rcl']
    if rreo['status'] != 'consultada':
        conferencia = dict(situacao='nao_feita', rcl_rreo=None, diferenca=None)
        nota = f"A conferência da RCL pelo RREO não pôde ser feita ({rreo['status']}): {rreo['note']}"
    else:
        rcl_rreo = rreo['valores']['rcl']
        diferenca = round(rcl - rcl_rreo, 2)
        situacao = 'confere' if abs(diferenca) <= TOLERANCIA_RCL else 'diverge'
        conferencia = dict(situacao=situacao, rcl_rreo=rcl_rreo, diferenca=diferenca)
        if situacao == 'confere':
            nota = 'A RCL confere com a do RREO-Anexo 03 do mesmo período.'
        else:
            nota = (f'A RCL do RGF ({reais(rcl)}) diverge da do RREO-Anexo 03 do mesmo período ({reais(rcl_rreo)}), '
                    f'diferença de {reais(diferenca)}. O valor do RGF é mantido; confira se um dos demonstrativos foi retificado.')
    return {**rgf, 'valores': {**rgf['valores'], 'conferencia_rreo': conferencia}, 'note': f"{rgf['note']} {nota}"}


def coletar_rcl(cache: Path, id_ente: int = MUNICIPIO_PADRAO, esperar: Callable[[float], None] = time.sleep,
                relogio: Callable[[], float] = time.monotonic, agora: Callable[[], datetime] = agora_utc,
                forcar: bool = False, somente_cache: bool = False) -> dict:
    """RGF-Anexo 01 do último exercício fechado publicado, escolhido pelo extrato de entregas."""
    periodo = coletar_periodo_rgf(cache, id_ente, None, esperar, relogio, agora, forcar, somente_cache)
    if periodo['status'] == 'nao_consultada':
        return resultado('siconfi_rgf_pessoal', None, 'nao_consultada', periodo['note'])
    if periodo['status'] != 'consultada':
        return resultado('siconfi_rgf_pessoal', None, periodo['status'],
                         f"Período do RGF não determinado pelo extrato de entregas. {periodo['note']}",
                         checked_at=periodo['checked_at'])
    v = periodo['valores']
    rgf = coletar_rgf_pessoal(cache, v['exercicio'], v['periodo'], id_ente, v['periodicidade'], v['demonstrativo'],
                              esperar, relogio, agora, forcar, somente_cache)
    if rgf['status'] == 'consultada':
        # conferência opcional: o RREO só acrescenta uma nota, nunca muda o status do RGF.
        rreo = coletar_rreo_rcl(cache, v['exercicio'], BIMESTRE_DO_RGF[(v['periodicidade'], v['periodo'])], id_ente,
                                'RREO Simplificado' if v['demonstrativo'] == 'RGF Simplificado' else 'RREO',
                                esperar, relogio, agora, forcar, somente_cache)
        rgf = conferir_rcl(rgf, rreo)
    escolha = {k: v[k] for k in ('exercicio', 'periodo', 'periodicidade', 'demonstrativo', 'status_relatorio', 'data_status',
                                 'mais_recente_publicado')}
    return {**rgf, 'valores': {**rgf['valores'], 'escolha_periodo': escolha} if rgf['valores'] else {},
            'note': f"{rgf['note']} Período escolhido pelo extrato de entregas: {periodo['note']}"}



# ---------------------------------------------------------------- benchmark per capita

# pares: os municípios da UF mais próximos em população, medida pela razão (13.000 está tão perto de
# 13.813 quanto 14.677). Faixa percentual fixa daria 2 pares numa UF e 40 em outra; os N mais próximos
# dão sempre o mesmo tamanho de amostra. A faixa máxima só impede que uma UF pequena empurre para a
# lista municípios com o dobro ou a metade do porte.
MAXIMO_PARES = 10        # cada par custa 1 ou 2 requisições ao SICONFI (1,1 s cada); 10 fica perto de 11-22 s, em cache por 7 dias
MINIMO_PARES = 5         # com menos de 5 valores a mediana é dominada por um único município
FAIXA_PARES = 1.5        # até 1,5 vez maior ou menor que o município
# o SICONFI aceita um id_ente por consulta (ETAPA4_NEGOCIO.md): uma requisição por par. Cada par é
# tentado no simplificado semestral e, se não houver, no RGF comum quadrimestral.
OPCOES_RGF_PAR = (('S', 'RGF Simplificado', 2), ('Q', 'RGF', 3))


def centavos(valor: float) -> float:
    """arredonda meio centavo para cima, como no relatório; round() em float arredonda 7068,495 para baixo."""
    return float(Decimal(repr(valor)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def selecionar_pares(municipios: list[dict], codigo_ibge: int) -> list[dict]:
    """até MAXIMO_PARES municípios mais próximos em população, dentro da FAIXA_PARES."""
    alvo = next((m for m in municipios if m['codigo_ibge'] == codigo_ibge), None)
    if alvo is None:
        return []
    distancia = lambda m: abs(math.log(m['populacao'] / alvo['populacao']))
    candidatos = [m for m in municipios if m['codigo_ibge'] != codigo_ibge and distancia(m) <= math.log(FAIXA_PARES)]
    return sorted(candidatos, key=lambda m: (distancia(m), m['codigo_ibge']))[:MAXIMO_PARES]


def rgf_de_par(codigo_ibge: int, exercicio: int, esperar: Callable[[float], None],
               relogio: Callable[[], float]) -> dict:
    """RGF-Anexo 01 de um par, sem cache próprio: o benchmark inteiro é que fica em cache."""
    r = None
    for periodicidade, demonstrativo, periodo in OPCOES_RGF_PAR:
        parametros = dict(an_exercicio=exercicio, in_periodicidade=periodicidade, nr_periodo=periodo,
                          co_tipo_demonstrativo=demonstrativo, no_anexo=ANEXO_PESSOAL, co_esfera='M',
                          co_poder='E', id_ente=codigo_ibge)
        url = url_siconfi('rgf', parametros)
        try:
            r = ler_rgf_pessoal(buscar_siconfi('rgf', parametros, esperar, relogio)[1], url)
        except FalhaColeta as e:
            # falha de rede não melhora trocando o tipo de demonstrativo.
            return resultado('siconfi_rgf_pessoal', url, 'indisponivel', str(e))
        if r['status'] != 'sem_dados':
            return r
    return r


def coletar_pares(cache: Path, pares: list[dict], exercicio: int, chave: str, esperar: Callable[[float], None],
                  relogio: Callable[[], float], agora: Callable[[], datetime], forcar: bool,
                  somente_cache: bool = False) -> dict:
    """RCL por habitante de cada par; par sem dado sai da lista com registro."""
    def obter(checked_at: str) -> dict:
        validos, excluidos = [], []
        for par in pares:
            r = rgf_de_par(par['codigo_ibge'], exercicio, esperar, relogio)
            if r['status'] != 'consultada':
                excluidos.append(dict(codigo_ibge=par['codigo_ibge'], nome=par['nome'],
                                      motivo=f"{r['status']}: {r['note']}"))
                continue
            rcl = r['valores']['rcl']
            validos.append(dict(par, rcl=rcl, rcl_per_capita=centavos(rcl / par['populacao']),
                                periodicidade=r['valores']['periodicidade'], periodo=r['valores']['periodo']))
        valores = dict(exercicio=exercicio, pares=validos, excluidos=excluidos, total_excluidos=len(excluidos))
        nota = f'{len(validos)} de {len(pares)} pares com RCL de {exercicio}; {len(excluidos)} excluídos sem dado.'
        if len(validos) < MINIMO_PARES:
            return resultado('benchmark_rcl_per_capita', None, 'sem_dados',
                             f'{nota} São necessários pelo menos {MINIMO_PARES}; {len(excluidos)} falharam.',
                             dict(valores, pares=[]), checked_at=checked_at)
        return resultado('benchmark_rcl_per_capita', None, 'consultada', nota, valores, checked_at=checked_at)

    return coletar(cache, 'benchmark_rcl_per_capita', chave, obter, agora, forcar, somente_cache)


def aviso_estudo_de_caso(nome: str, exercicio: int, receita_estudo_hab: float, rcl_hab: float) -> str:
    """aviso neutro: os números do estudo não são os do município real. Vai para a tela e o relatório."""
    vezes = f'{receita_estudo_hab / rcl_hab:.1f}'.replace('.', ',')
    return ('Os valores do estudo de caso não correspondem aos valores publicados pelo município real. '
            f'A receita arrecadada da planilha equivale a {reais(receita_estudo_hab)} por habitante; a Receita '
            f'Corrente Líquida publicada por {nome} no RGF de {exercicio} equivale a {reais(rcl_hab)} por '
            f'habitante, cerca de {vezes} vezes menor. Os dois números também não medem exatamente a mesma '
            'coisa, porque a RCL exclui deduções previstas na Lei de Responsabilidade Fiscal. Por isso, este '
            'benchmark serve como referência de ordem de grandeza, não como comparação direta com a planilha.')


def coletar_benchmark(cache: Path, receita_estudo: float | None = None, id_ente: int = MUNICIPIO_PADRAO,
                      uf: int = UF_PADRAO, esperar: Callable[[float], None] = time.sleep,
                      relogio: Callable[[], float] = time.monotonic, agora: Callable[[], datetime] = agora_utc,
                      forcar: bool = False, somente_cache: bool = False) -> dict:
    """RCL por habitante do município ao lado da mediana dos pares de porte semelhante na UF.

    receita_estudo é a receita arrecadada da planilha (dataset['metrics']['revenue']); quando vem, o
    resultado traz o aviso de que o estudo de caso não usa os valores publicados do município.
    """
    def sem(status: str, nota: str, checked_at=None) -> dict:
        if status == 'nao_consultada':
            return resultado('benchmark_rcl_per_capita', None, status, 'Ainda não coletado. Use o botão de coletar.')
        return resultado('benchmark_rcl_per_capita', None, status if status != 'consultada' else 'sem_dados',
                         nota, checked_at=checked_at)

    rcl = coletar_rcl(cache, id_ente, esperar, relogio, agora, forcar, somente_cache)
    if rcl['status'] != 'consultada':
        return sem(rcl['status'], f"Sem a RCL do município. {rcl['note']}", rcl['checked_at'])
    exercicio = rcl['valores']['exercicio']
    lista = coletar_populacao_municipios(cache, exercicio, uf, esperar, agora, forcar, somente_cache)
    if lista['status'] != 'consultada':
        return sem(lista['status'], f"Sem a população dos municípios em {exercicio}. {lista['note']}", lista['checked_at'])
    municipio = next((m for m in lista['valores']['municipios'] if m['codigo_ibge'] == id_ente), None)
    if municipio is None:
        return sem('sem_dados', f'Município {id_ente} sem população de {exercicio} na lista do IBGE.')
    pares = selecionar_pares(lista['valores']['municipios'], id_ente)
    if len(pares) < MINIMO_PARES:
        return sem('sem_dados', f'Só {len(pares)} municípios da UF têm porte semelhante; são necessários {MINIMO_PARES}.')

    chave = (f"benchmark rcl_per_capita uf={uf} ente={id_ente} exercicio={exercicio} "
             f"pares={','.join(str(p['codigo_ibge']) for p in pares)}")
    r = coletar_pares(cache, pares, exercicio, chave, esperar, relogio, agora, forcar, somente_cache)
    if r['status'] != 'consultada':
        return r

    # a parte do município é recalculada a cada chamada, com a RCL e a população mais recentes do cache.
    valor = centavos(rcl['valores']['rcl'] / municipio['populacao'])
    por_capita = [p['rcl_per_capita'] for p in r['valores']['pares']]
    mediana = centavos(statistics.median(por_capita))
    ordem = 1 + sum(v > valor for v in por_capita)
    diferenca = round((valor / mediana - 1) * 100, 1)
    valores = dict(r['valores'], municipio=dict(municipio, rcl=rcl['valores']['rcl'], rcl_per_capita=valor),
                   mediana_pares=mediana,
                   posicao=dict(ordem=ordem, total=len(por_capita) + 1, diferenca_mediana_pct=diferenca))
    pct = f'{abs(diferenca):.1f}'.replace('.', ',')
    nota = (f"RCL por habitante de {municipio['nome']} em {exercicio}: {reais(valor)}. Mediana de "
            f"{len(por_capita)} municípios da UF com população semelhante: {reais(mediana)}. O município está "
            f"{pct}% {'acima' if diferenca >= 0 else 'abaixo'} da mediana, "
            f"{ordem}º de {len(por_capita) + 1} do maior para o menor valor. {r['note']}")
    if receita_estudo is not None:
        receita = valor_api(receita_estudo, 'receita do estudo de caso')
        receita_hab = centavos(receita / municipio['populacao'])
        aviso = aviso_estudo_de_caso(municipio['nome'], exercicio, receita_hab, valor)
        valores['comparacao_estudo'] = dict(receita_estudo=receita, receita_estudo_per_capita=receita_hab,
                                            rcl_per_capita=valor, razao=round(receita_hab / valor, 2), aviso=aviso)
        nota += ' Aviso: ' + aviso
    # desatualizado se qualquer dado de entrada estiver; o extrato fica de fora (validade de 1 dia, só escolhe o período).
    return {**r, 'valores': valores, 'note': nota,
            'desatualizado': r['desatualizado'] or rcl['desatualizado'] or lista['desatualizado']}



# ---------------------------------------------------------------- estado para a tela e para o agente

def estado_coleta(cache: Path, receita_estudo: float | None = None,
                  agora: Callable[[], datetime] = agora_utc) -> dict:
    """RCL e benchmark só do cache: nunca consulta a rede nem espera uma coleta em andamento."""
    return dict(rcl=coletar_rcl(cache, agora=agora, somente_cache=True),
                benchmark=coletar_benchmark(cache, receita_estudo, agora=agora, somente_cache=True))


def atualizar_coleta(cache: Path, receita_estudo: float | None = None, forcar: bool = False,
                     esperar: Callable[[float], None] = time.sleep, relogio: Callable[[], float] = time.monotonic,
                     agora: Callable[[], datetime] = agora_utc) -> dict:
    """coleta o que faltar ou tiver vencido (o benchmark puxa a RCL) e devolve o estado do cache."""
    coletar_benchmark(cache, receita_estudo, esperar=esperar, relogio=relogio, agora=agora, forcar=forcar)
    return estado_coleta(cache, receita_estudo, agora)


def coleta_vazia(estado: dict) -> bool:
    return all(r['status'] == 'nao_consultada' for r in estado.values())
