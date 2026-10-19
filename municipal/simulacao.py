"""simulação com hipóteses editáveis."""

from __future__ import annotations

import math


HORIZONTE_ANOS = 10

DEFAULTS = dict(
    recovery_pct=20.0,
    admin_pct=5.0, environment_pct=3.0, security_pct=2.0,
    das_cut_pct=15.0, attrition_pct=0.0, temp_saving_pct=0.0,
    refis_adhesion_pct=10.0, refis_discount_pct=30.0,
    collection_cost=500_000.0, savings_cost=100_000.0, das_cost=300_000.0, refis_cost=200_000.0)

# teto definido pelo grupo
LIMITES = dict(refis_discount_pct=50.0)

SECRETARIAS_REVISADAS = [
    ('SEC-ADM', 'admin_pct', 'Administração'),
    ('SEC-AMB', 'environment_pct', 'Meio Ambiente'),
    ('SEC-SEG', 'security_pct', 'Segurança'),
]

RESTRICOES_DO_CLIENTE = [
    dict(id='sem_aumento_de_tributos',
         text='Nenhuma proposta aumenta alíquota nem cria tributo (o cliente não quer aumentar impostos por questões eleitorais).'),
    dict(id='tributacao_exige_aprovacao_superior',
         text='Medidas que mexem em tributação exigem aprovação em nível superior; aprovar uma delas pede o nome do aprovador.'),
    dict(id='concursados_nao_sao_demitidos',
         text='Servidores concursados não podem ser demitidos; a redução de pessoal usa cargos de confiança e a saída natural de servidores.'),
    dict(id='horizonte_de_10_anos',
         text='O projeto tem horizonte de 10 anos. A projeção anual mantém valores constantes, sem inflação nem crescimento.'),
]


def validate_inputs(raw: dict) -> dict:
    """completa padrões e valida limites."""
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError('Premissas desconhecidas ou formato inválido.')
    premissas = {**DEFAULTS, **raw}
    for nome, valor in premissas.items():
        if isinstance(valor, bool) or not isinstance(valor, (float, int)) or not math.isfinite(valor):
            raise ValueError(f'{nome}: informe um número finito.')
        limite = LIMITES.get(nome, 100 if nome.endswith('_pct') else 1_000_000_000)
        if valor < 0 or valor > limite:
            raise ValueError(f'{nome}: valor deve estar entre 0 e {limite:g}.')
        premissas[nome] = float(valor)
    return premissas


def serie_anual(primeiro_ano: float, anos_seguintes) -> list[float]:
    """efeito anual; custo só no primeiro ano."""
    return [round(primeiro_ano, 2)] + [round(anos_seguintes(ano), 2) for ano in range(2, HORIZONTE_ANOS + 1)]


def proposta_recuperar_divida(dataset: dict, p: dict) -> dict:
    """recuperação de dívida em 12 meses."""
    base = dataset['metrics']['estimated_recovery']
    bruto = round(base * p['recovery_pct'] / 100, 2)
    liquido = round(bruto - p['collection_cost'], 2)
    return dict(
        id='recuperar_divida', title='Organizar a recuperação da dívida ativa',
        category='Receita extraordinária', base=base, rate=p['recovery_pct'],
        gross=bruto, cost=p['collection_cost'], net=liquido,
        yearly=serie_anual(liquido, lambda ano: 0),
        formula='Soma da recuperação estimada × percentual de realização − custo de cobrança',
        source=dataset['metric_sources']['estimated_recovery'],
        conditions=['Confirmar se a estimativa representa créditos ainda recuperáveis e evita dupla contagem com a arrecadação atual.',
                    'Validar prazo, capacidade de cobrança, custos e instrumentos com Finanças e Procuradoria.',
                    'Parte dos devedores não é localizada (entrevista, pergunta 3); a taxa de realização precisa considerar isso.'],
        assumption='Realização adicional em 12 meses. Estoque de dívida não é receita recorrente.')


def proposta_revisar_custeio(dataset: dict, p: dict) -> dict:
    """economia no custeio de três secretarias."""
    secretarias = {s['id']: s for s in dataset['departments']}
    economias = []
    for codigo, premissa, nome in SECRETARIAS_REVISADAS:
        if codigo not in secretarias:
            raise ValueError(f'Secretaria {codigo} não encontrada.')
        s = secretarias[codigo]
        economias.append(dict(department=nome, base=s['eligible'], rate=p[premissa],
                              gross=round(s['eligible'] * p[premissa] / 100, 2), source=s['source']))
    bruto = round(sum(e['gross'] for e in economias), 2)
    liquido = round(bruto - p['savings_cost'], 2)
    return dict(
        id='revisar_custeio', title='Revisar contratos e despesas de custeio',
        category='Economia potencial em 12 meses', base=round(sum(e['base'] for e in economias), 2),
        rate=None, gross=bruto, cost=p['savings_cost'], net=liquido,
        yearly=serie_anual(liquido, lambda ano: bruto),
        formula='Soma das bases aproximadas × percentual de cada secretaria − custo da revisão',
        source="'Despesas por Secretaria'!E8:G8;E11:G12", details=economias,
        conditions=['Confirmar a composição das despesas com os contratos originais.',
                    'Preservar serviços essenciais, vinculações e obrigações; submeter mudanças ao responsável.',
                    'A base exclui folha e investimentos, então não se sobrepõe à proposta de pessoal.'],
        assumption='Economias hipotéticas; não incluem cortes de pessoal, obras ou verbas vinculadas.')


def proposta_reduzir_folha(dataset: dict, p: dict) -> dict:
    """corte de cargos; temporários opcionais e não validados."""
    m = dataset['metrics']
    fonte = dataset['metric_sources']['payroll_groups']
    das_anual = round(m['das_monthly_cost'] * 12, 2)
    administrativos_anual = round(m['admin_staff_monthly_cost'] * 12, 2)
    economia_das = round(das_anual * p['das_cut_pct'] / 100, 2)
    troca_por_ano = administrativos_anual * p['attrition_pct'] / 100 * p['temp_saving_pct'] / 100

    def economia_temporarios(ano: int) -> float:
        return min(ano * p['attrition_pct'], 100) / p['attrition_pct'] * troca_por_ano if p['attrition_pct'] else 0

    bruto = round(economia_das + economia_temporarios(1), 2)
    liquido = round(bruto - p['das_cost'], 2)
    formula = 'Custo anual dos cargos de confiança × percentual de corte − custo de transição'
    if troca_por_ano:
        formula = ('Custo anual dos cargos de confiança × percentual de corte + economia da reposição por contrato '
                   'temporário (opcional, não validada) − custo de transição')
    detalhes = [dict(department=f'Cargos de confiança ({m["das_count"]} DAS): corte de {p["das_cut_pct"]:g}%',
                     base=das_anual, rate=p['das_cut_pct'], gross=economia_das, source=fonte)]
    if troca_por_ano:
        detalhes.append(dict(
            department=f'Reposição por contrato temporário ({m["admin_staff_count"]} administrativos): {p["attrition_pct"]:g}% saem por ano e {p["temp_saving_pct"]:g}% de economia, 1º ano',
            base=administrativos_anual, rate=round(p['attrition_pct'] * p['temp_saving_pct'] / 100, 4),
            gross=round(economia_temporarios(1), 2), source=fonte))
    return dict(
        id='reduzir_folha', title='Reduzir cargos de confiança',
        category='Economia recorrente de pessoal', base=das_anual,
        rate=p['das_cut_pct'], gross=bruto, cost=p['das_cost'], net=liquido,
        yearly=serie_anual(liquido, lambda ano: economia_das + economia_temporarios(ano)),
        formula=formula,
        source=fonte, details=detalhes,
        conditions=['Cargos de confiança são de livre exoneração, mas é preciso definir quais podem sair sem comprometer a instituição (anotação do entrevistador).',
                    'Concursados não podem ser demitidos, e esta proposta não mexe neles.',
                    'A reposição por contrato temporário está desligada no cenário padrão. O STF (Tema 612) exige necessidade temporária e interesse público excepcional e não admite temporário para serviço permanente; só ligue para testar e confirme o enquadramento com a Procuradoria.',
                    'Custo de transição (verbas proporcionais das exonerações) é uma hipótese para confirmar com Recursos Humanos.'],
        assumption='Só o corte de cargos de confiança conta no cenário padrão. A reposição por temporários é opcional, não está validada e não deve ser tratada como economia garantida.')


def proposta_refis_iptu(dataset: dict, p: dict) -> dict:
    """refis com desconto sobre o valor devido."""
    iptu = next((d for d in dataset['debts'] if d['name'].startswith('IPTU')), None)
    if iptu is None:
        raise ValueError('Linha de IPTU não encontrada na dívida ativa.')
    base = round(iptu['stock'] - iptu['recovery'], 2)
    bruto = round(base * p['refis_adhesion_pct'] / 100 * (1 - p['refis_discount_pct'] / 100), 2)
    liquido = round(bruto - p['refis_cost'], 2)
    return dict(
        id='refis_iptu', title='Programa de Refis do IPTU com desconto controlado',
        category='Receita extraordinária com renúncia limitada', base=base,
        rate=round(p['refis_adhesion_pct'] * (1 - p['refis_discount_pct'] / 100), 4),
        gross=bruto, cost=p['refis_cost'], net=liquido, senior_approval=True,
        yearly=serie_anual(liquido, lambda ano: 0),
        formula='(Estoque de IPTU − recuperação já estimada) × adesão × (1 − desconto) − custo do programa',
        source=iptu['source'],
        conditions=['Não é aumento de tributo: reduz o valor cobrado de quem já deve, sem mudar a alíquota do IPTU.',
                    'A planilha não separa principal, juros e multa. Por isso o desconto é aplicado ao valor total devido de quem adere, uma hipótese prudente: se ele incidisse só sobre juros e multa, o ganho seria maior.',
                    'Hipótese a confirmar: quem adere ao Refis não é quem pagaria pela cobrança normal. Se houver sobreposição, o ganho é menor.',
                    'O teto de 50% no desconto é hipótese do grupo: o cliente pediu cuidado para a cobrança não ficar irrisória (entrevista, pergunta 3), mas não definiu esse percentual.',
                    'Mexe em tributação: exige aprovação em nível superior antes de aprovar o estudo.',
                    'Confirmar com a Procuradoria o instrumento legal necessário e a regra do Refis (a LRF, art. 14, trata de renúncia de receita).'],
        assumption='Adesão e desconto são hipóteses do grupo. A base exclui a parte já estimada como recuperável na proposta 1, mas a planilha não prova que são devedores diferentes.')


def proposta_ordenar_pagamentos(dataset: dict, p: dict) -> dict:
    """prioridades de pagamento, sem ganho estimado."""
    return dict(
        id='ordenar_pagamentos', title='Definir ordem de prioridade para os pagamentos',
        category='Governança financeira', base=0, rate=None, gross=0, cost=0, net=0,
        qualitative=True, yearly=[0.0] * HORIZONTE_ANOS,
        formula='Sem cálculo financeiro: medida de governança, sem valor estimado.',
        source="Entrevista, pergunta 5",
        conditions=['Hoje não há ordem definida e o pagamento segue FIFO: quem pede primeiro recebe primeiro.',
                    'Propor uma ordem (obrigações legais e folha, depois vinculações e contratos essenciais) e quem autoriza cada faixa.',
                    'Não há dado de contas a pagar na planilha, então o ganho não é estimado.'],
        assumption='Nenhum valor entra na projeção. O benefício esperado é previsibilidade de caixa e menos restos a pagar.')


PROPOSTAS = [proposta_recuperar_divida, proposta_revisar_custeio, proposta_reduzir_folha,
             proposta_refis_iptu, proposta_ordenar_pagamentos]


def projecao_anual(acoes: list[dict], saldo_inicial: float) -> list[dict]:
    """projeção com saldo inicial constante."""
    linhas = []
    acumulado_beneficio = 0.0
    acumulado_saldo = 0.0
    for i in range(HORIZONTE_ANOS):
        beneficio = round(sum(a['yearly'][i] for a in acoes), 2)
        acumulado_beneficio = round(acumulado_beneficio + beneficio, 2)
        saldo = round(saldo_inicial + beneficio, 2)
        acumulado_saldo = round(acumulado_saldo + saldo, 2)
        linhas.append(dict(year=i + 1, net=beneficio, cumulative_net=acumulado_beneficio,
                           balance=saldo, cumulative_balance=acumulado_saldo))
    return linhas


# só percentuais de ganho variam
CENARIOS = [
    ('conservador', 'Conservador', 0.5),
    ('base', 'Base', 1.0),
    ('otimista', 'Otimista', 1.5),
]


NAO_ESCALAR = {'refis_discount_pct'}


def escalar_percentuais(p: dict, fator: float) -> dict:
    """escala ganhos; mantém o desconto do refis."""
    escalado = dict(p)
    for nome, valor in p.items():
        if nome.endswith('_pct') and nome not in NAO_ESCALAR:
            escalado[nome] = round(min(valor * fator, LIMITES.get(nome, 100.0)), 4)
    return escalado


def calcular(dataset: dict, p: dict) -> dict:
    """propostas e projeção de 10 anos."""
    acoes = [proposta(dataset, p) for proposta in PROPOSTAS]
    saldo_inicial = dataset['metrics']['balance']
    beneficio = round(sum(a['net'] for a in acoes), 2)
    return dict(actions=acoes, baseline=saldo_inicial, net_benefit=beneficio,
                projected_balance=round(saldo_inicial + beneficio, 2),
                horizon=projecao_anual(acoes, saldo_inicial))


def comparar_cenarios(dataset: dict, p: dict) -> list[dict]:
    resultado = []
    for id_, rotulo, fator in CENARIOS:
        r = calcular(dataset, p if fator == 1.0 else escalar_percentuais(p, fator))
        resultado.append(dict(id=id_, label=rotulo, factor=fator, net_benefit=r['net_benefit'],
                              projected_balance=r['projected_balance'],
                              balance_year10=r['horizon'][-1]['balance'],
                              cumulative_balance=r['horizon'][-1]['cumulative_balance']))
    return resultado


def cobertura_recorrente(horizon: list[dict], saldo_inicial: float, acoes: list[dict], metrics: dict) -> dict:
    """cobertura e equilíbrio mantendo as outras medidas; sem custos de transição."""
    beneficio = horizon[1]['net']
    deficit = round(max(-saldo_inicial, 0), 2)
    por_id = {a['id']: a for a in acoes}
    folha, custeio = por_id['reduzir_folha'], por_id['revisar_custeio']

    def percentual_necessario(base: float, outras_medidas: float) -> float | None:
        if not deficit or not base:
            return None
        necessario = (deficit - outras_medidas) / base * 100
        return round(max(necessario, 0), 1) if necessario <= 100 else None

    economia_cargos = round(folha['base'] * folha['rate'] / 100, 2)
    cargos = percentual_necessario(folha['base'], beneficio - economia_cargos)
    return dict(annual_benefit=beneficio, annual_deficit=deficit,
                coverage=round(beneficio / deficit, 4) if deficit else None,
                gap=round(max(deficit - beneficio, 0), 2),
                breakeven_das_cut_pct=cargos,
                breakeven_das_positions=round(cargos / 100 * metrics['das_count']) if cargos is not None else None,
                breakeven_custeio_pct=percentual_necessario(custeio['base'], beneficio - custeio['gross']))


def objetivo_equilibrio(horizon: list[dict]) -> dict:
    """confere o saldo de todos os anos."""
    negativos = [r['year'] for r in horizon if r['balance'] < 0]
    return dict(meets=not negativos, minimum_balance=min(r['balance'] for r in horizon),
                first_deficit_year=negativos[0] if negativos else None,
                year10_balance=horizon[-1]['balance'])


def alternativa_equilibrio(dataset: dict, p: dict, atual: dict) -> dict:
    """busca equilíbrio sem temporários, mantendo as outras premissas."""
    if objetivo_equilibrio(atual['horizon'])['meets']:
        return dict(status='covered')
    for corte in range(math.ceil(p['das_cut_pct']), 101):
        premissas = {**p, 'das_cut_pct': float(corte), 'attrition_pct': 0.0, 'temp_saving_pct': 0.0}
        resultado = calcular(dataset, premissas)
        if not objetivo_equilibrio(resultado['horizon'])['meets']:
            continue
        conservador = calcular(dataset, escalar_percentuais(premissas, 0.5))
        return dict(
            status='candidate', inputs=premissas, das_cut_pct=float(corte),
            equivalent_positions=round(dataset['metrics']['das_count'] * corte / 100),
            first_year_balance=resultado['projected_balance'],
            recurring_balance=min(r['balance'] for r in resultado['horizon'][1:]),
            year10_balance=resultado['horizon'][-1]['balance'],
            cumulative_balance=resultado['horizon'][-1]['cumulative_balance'],
            conservative_minimum_balance=objetivo_equilibrio(conservador['horizon'])['minimum_balance'],
            conservative_meets=objetivo_equilibrio(conservador['horizon'])['meets'],
            validated=False,
            method='Menor corte inteiro dos gastos com cargos de confiança, igual ou superior ao atual, '
                   'que deixa os dez saldos anuais não negativos. Mantém as outras premissas e os custos; '
                   'desliga a reposição por temporários.',
            condition='Conferir com RH e responsáveis quais cargos podem sair, os custos de transição '
                      'e a continuidade dos serviços. Equilíbrio matemático não comprova viabilidade.')
    return dict(status='unavailable',
                reason='Mesmo cortando 100% dos gastos com cargos de confiança, as demais premissas '
                       'não permitem dez saldos anuais não negativos. Rever o plano; não há alternativa '
                       'calculada com esse ajuste.')


def simulate(dataset: dict, inputs: dict | None = None) -> dict:
    p = validate_inputs(inputs or {})
    r = calcular(dataset, p)
    acoes = r['actions']
    return dict(
        inputs=p, actions=acoes, baseline=r['baseline'],
        gross_benefit=round(sum(a['gross'] for a in acoes), 2),
        costs=round(sum(a['cost'] for a in acoes), 2), net_benefit=r['net_benefit'],
        projected_balance=r['projected_balance'],
        period='12 meses (hipótese a confirmar)',
        horizon=r['horizon'], horizon_years=HORIZONTE_ANOS,
        objective=objetivo_equilibrio(r['horizon']),
        equilibrium=alternativa_equilibrio(dataset, p, r),
        recurring=cobertura_recorrente(r['horizon'], r['baseline'], acoes, dataset['metrics']),
        scenarios=comparar_cenarios(dataset, p),
        constraints=RESTRICOES_DO_CLIENTE,
        interpretation='Cenário simulado. Não representa arrecadação realizada nem economia contratada.')


def approved_projection(run: dict, reviews: dict) -> dict:
    """projeção das propostas aprovadas."""
    acoes = run['scenario']['actions']
    # aprovar o estudo não executa a medida
    aprovadas = [a for a in acoes if reviews.get(a['id'], {}).get('decision') == 'aprovada']
    beneficio = round(sum(a['net'] for a in aprovadas), 2)
    resultado = dict(approved_count=len(aprovadas), total_count=len(acoes), net_benefit=beneficio,
                     projected_balance=round(run['scenario']['baseline'] + beneficio, 2), horizon=None, objective=None)
    if all('yearly' in a for a in acoes):
        resultado['horizon'] = projecao_anual(aprovadas, run['scenario']['baseline'])
        resultado['objective'] = objetivo_equilibrio(resultado['horizon'])
    return resultado
