"""coleta de dados fiscais em apis públicas."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
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
                note=note, valores=valores or {})


CHAVES_RESULTADO = {'id', 'title', 'url', 'status', 'checked_at', 'content_sha256', 'note', 'valores'}


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
              'Accept-Language': 'pt-BR,pt;q=0.9', 'Accept': 'application/json'}


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
        except HTTPError as e:
            # urllib levanta exceção em 4xx/5xx; o código decide a repetição, o corpo é só complemento.
            status = e.code
            try:
                conteudo = e.read(TETO_BYTES + 1)
            except (OSError, ValueError, http.client.HTTPException):
                conteudo = b''
            finally:
                e.close()
    except (OSError, http.client.HTTPException) as e:
        # OSError cobre timeout, conexão recusada e DNS; URLError é subclasse e guarda a causa em reason.
        causa = e.reason if isinstance(getattr(e, 'reason', None), BaseException) else e
        raise FalhaRede(f'falha de rede ({type(causa).__name__})', passageira=True) from e
    if len(conteudo) > TETO_BYTES:
        raise FalhaRede(f'resposta maior que {TETO_BYTES // 1_000_000} MB', passageira=False)
    return status, conteudo


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
VALIDADE = dict(siconfi_rgf_pessoal=timedelta(days=7), ibge_populacao=timedelta(days=30),
                ibge_populacao_municipios=timedelta(days=30))
CHAVES_ENTRADA = {'url', 'ultimo_bom', 'ultimo_bom_em', 'ultima_tentativa', 'ultima_tentativa_em'}
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
    if not isinstance(entrada, dict) or set(entrada) != CHAVES_ENTRADA or not isinstance(entrada['url'], str):
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


def coletar(cache: Path, fonte_id: str, url: str, obter: Callable[[str], dict],
            agora: Callable[[], datetime] = agora_utc) -> dict:
    """devolve o valor do cache se ainda vale; senão consulta com obter(checked_at) e grava."""
    with TRAVA_COLETA:
        dados = ler_cache(cache)
        entrada = dados.get(fonte_id)
        if entrada and entrada['url'] != url:
            # outra consulta (outro exercício ou município): o valor guardado não responde a esta.
            entrada = None
        momento = agora()
        bom = entrada['ultimo_bom'] if entrada else None
        bom_em = entrada['ultimo_bom_em'] if entrada else None
        if bom and timedelta(0) <= momento - instante(bom_em) < VALIDADE[fonte_id]:
            return dict(bom)
        novo = obter(momento.isoformat())
        if novo['status'] == 'consultada':
            bom, bom_em = novo, momento.isoformat()
        dados[fonte_id] = dict(url=url, ultimo_bom=bom, ultimo_bom_em=bom_em,
                               ultima_tentativa=novo, ultima_tentativa_em=momento.isoformat())
        gravar_cache(cache, dados)
        return novo


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
                        agora: Callable[[], datetime] = agora_utc) -> dict:
    """RGF-Anexo 01 do Executivo. Santa Leopoldina publica o simplificado semestral (ETAPA1_APIS.md)."""
    parametros = dict(an_exercicio=exercicio, in_periodicidade=periodicidade, nr_periodo=periodo,
                      co_tipo_demonstrativo=demonstrativo, no_anexo=ANEXO_PESSOAL, co_esfera='M',
                      co_poder='E', id_ente=id_ente)
    url = url_siconfi('rgf', parametros)
    obter = obter_por('siconfi_rgf_pessoal', url,
                      lambda: buscar_siconfi('rgf', parametros, esperar, relogio)[1],
                      lambda conteudo, checked_at: ler_rgf_pessoal(conteudo, url, checked_at))
    return coletar(cache, 'siconfi_rgf_pessoal', url, obter, agora)


def url_populacao(ano: int, localidades: str) -> str:
    return (f'https://servicodados.ibge.gov.br/api/v3/agregados/{AGREGADO_POPULACAO}/periodos/{ano}'
            f'/variaveis/{VARIAVEL_POPULACAO}?localidades={quote(localidades, safe="")}')


def coletar_populacao_municipio(cache: Path, ano: int, codigo_ibge: int = MUNICIPIO_PADRAO,
                                esperar: Callable[[float], None] = time.sleep,
                                agora: Callable[[], datetime] = agora_utc) -> dict:
    url = url_populacao(ano, f'N6[{codigo_ibge}]')
    obter = obter_por('ibge_populacao', url, lambda: buscar(url, esperar),
                      lambda conteudo, checked_at: ler_populacao_municipio(conteudo, url, codigo_ibge, ano, checked_at))
    return coletar(cache, 'ibge_populacao', url, obter, agora)


def coletar_populacao_municipios(cache: Path, ano: int, uf: int = UF_PADRAO,
                                 esperar: Callable[[float], None] = time.sleep,
                                 agora: Callable[[], datetime] = agora_utc) -> dict:
    url = url_populacao(ano, f'N6[N3[{uf}]]')
    obter = obter_por('ibge_populacao_municipios', url, lambda: buscar(url, esperar),
                      lambda conteudo, checked_at: ler_populacao_municipios(conteudo, url, ano, checked_at))
    return coletar(cache, 'ibge_populacao_municipios', url, obter, agora)
