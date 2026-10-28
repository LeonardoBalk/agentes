"""testes de dados, cálculos e revisão."""

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from municipal import agente
from municipal.agente import AgentError, ai_status, classificar_erro, demo_analysis
from municipal.dados import DataError, load_dataset, money
from municipal.fontes import CATALOG, extrair_dispositivos, refresh_sources, source_status
from municipal.simulacao import DEFAULTS, approved_projection, simulate
from municipal.storage import Store

ROOT=Path(__file__).resolve().parents[1]


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data=load_dataset(ROOT/'data'/'estudo_municipal.xlsx')

    def test_totals_reconcile_to_source_inputs(self):
        m=self.data['metrics']
        self.assertEqual(m['revenue'],577_300_000)
        self.assertEqual(m['expenses'],584_500_000)
        self.assertEqual(m['balance'],-7_200_000)
        self.assertEqual(m['debt_stock'],100_100_000)
        self.assertEqual(m['estimated_recovery'],36_230_000)
        self.assertEqual(m['staff_count'],2409)
        self.assertEqual(m['monthly_payroll'],18_983_267.83)

    def test_default_projection_accounts_for_costs_without_counting_debt_stock(self):
        s=simulate(self.data)
        self.assertEqual(s['actions'][0]['gross'],7_246_000)
        self.assertEqual(s['actions'][1]['gross'],918_000)
        self.assertEqual(s['costs'],1_100_000)
        self.assertEqual(s['net_benefit'],12_451_012.98)
        self.assertEqual(s['projected_balance'],5_251_012.98)

    def test_zero_recovery_preserves_costs_and_hundred_pct_caps_at_estimate(self):
        zero={k:0 for k in DEFAULTS}
        self.assertEqual(simulate(self.data,zero)['projected_balance'],-7_200_000)
        s=simulate(self.data,{**zero,'collection_cost':10_000})
        self.assertEqual(s['projected_balance'],-7_210_000)
        s=simulate(self.data,{**zero,'recovery_pct':100})
        self.assertEqual(s['net_benefit'],36_230_000)

    def test_current_situation_is_computed_from_the_sheet(self):
        achados={a['id']:a for a in self.data['situation']}
        self.assertEqual(list(achados),['resultado','receita_abaixo_do_previsto','despesa_acima_da_dotacao',
                                        'peso_da_folha','divida_ativa','convenios_e_obras','restos_a_pagar'])
        self.assertEqual(achados['resultado']['value'],-7_200_000)
        self.assertEqual(achados['receita_abaixo_do_previsto']['value'],65_200_000)
        self.assertEqual(achados['receita_abaixo_do_previsto']['items'][0]['value'],15_500_000)
        self.assertEqual(achados['despesa_acima_da_dotacao']['value'],3_500_000)
        self.assertEqual(achados['divida_ativa']['value'],100_100_000)
        self.assertEqual(achados['restos_a_pagar']['value'],18_200_000)
        self.assertAlmostEqual(achados['peso_da_folha']['value'],0.6655,places=4)
        self.assertIn('não o teste legal',achados['peso_da_folha']['note'])

    def test_client_requests_become_proposals(self):
        s=simulate(self.data)
        by_id={a['id']:a for a in s['actions']}
        self.assertEqual(list(by_id),['recuperar_divida','revisar_custeio','reduzir_folha','refis_iptu','ordenar_pagamentos'])
        self.assertEqual(self.data['metrics']['das_count'],181)
        self.assertEqual(by_id['reduzir_folha']['gross'],3_035_012.98)
        self.assertEqual(len(by_id['reduzir_folha']['details']),1)
        self.assertEqual(by_id['refis_iptu']['base'],33_600_000)
        self.assertEqual(by_id['refis_iptu']['gross'],2_352_000)
        self.assertTrue(by_id['refis_iptu']['senior_approval'])
        self.assertTrue(by_id['ordenar_pagamentos']['qualitative'])
        self.assertEqual(by_id['ordenar_pagamentos']['net'],0)

    def test_no_proposal_raises_taxes_and_constraints_are_exposed(self):
        s=simulate(self.data)
        for a in s['actions']:
            self.assertNotIn('aument',(a['id']+a['title']+a['category']).lower())
        self.assertIn('sem_aumento_de_tributos',{c['id'] for c in s['constraints']})
        self.assertIn('tributacao_exige_aprovacao_superior',{c['id'] for c in s['constraints']})

    def test_refis_discount_is_capped_to_keep_collection_meaningful(self):
        simulate(self.data,{'refis_discount_pct':50})
        with self.assertRaises(ValueError):
            simulate(self.data,{'refis_discount_pct':51})

    def test_three_scenarios_scale_only_the_percentages(self):
        s=simulate(self.data)
        ids=[c['id'] for c in s['scenarios']]
        self.assertEqual(ids,['conservador','base','otimista'])
        conservador,base,otimista=s['scenarios']
        self.assertEqual(base['net_benefit'],s['net_benefit'])
        self.assertEqual(base['projected_balance'],s['projected_balance'])
        self.assertLess(conservador['net_benefit'],base['net_benefit'])
        self.assertLess(base['net_benefit'],otimista['net_benefit'])
        # resultado depende das hipóteses
        self.assertLess(conservador['projected_balance'],0)
        self.assertGreater(otimista['projected_balance'],0)
        with self.assertRaises(ValueError):
            simulate(self.data,{'refis_discount_pct':51})
        zero={k:0 for k in DEFAULTS}
        resultados={c['net_benefit'] for c in simulate(self.data,zero)['scenarios']}
        self.assertEqual(len(resultados),1)

    def test_optimistic_is_never_worse_because_the_refis_discount_is_fixed(self):
        # regressão: adesão 100%, desconto 40%
        sem_outras={k:0 for k in ('recovery_pct','admin_pct','environment_pct','security_pct','das_cut_pct',
                                  'attrition_pct','temp_saving_pct','collection_cost','savings_cost','das_cost','refis_cost')}
        for adesao in (20,50,100):
            r=simulate(self.data,{**sem_outras,'refis_adhesion_pct':adesao,'refis_discount_pct':40})
            conservador,base,otimista=[c['net_benefit'] for c in r['scenarios']]
            self.assertLessEqual(conservador,base)
            self.assertLessEqual(base,otimista)
        self.assertEqual(simulate(self.data,{**sem_outras,'refis_adhesion_pct':100,'refis_discount_pct':40})['scenarios'][1]['net_benefit'],20_160_000)

    def test_temporary_replacement_is_off_by_default_and_optional(self):
        self.assertEqual(DEFAULTS['attrition_pct'],0)
        self.assertEqual(DEFAULTS['temp_saving_pct'],0)
        ligado=simulate(self.data,{'attrition_pct':5,'temp_saving_pct':25})
        folha=next(a for a in ligado['actions'] if a['id']=='reduzir_folha')
        self.assertEqual(folha['gross'],3_256_741.94)
        self.assertEqual(len(folha['details']),2)
        self.assertIn('contrato temporário',folha['formula'])
        desligado=next(a for a in simulate(self.data)['actions'] if a['id']=='reduzir_folha')
        self.assertNotIn('temporário',desligado['formula'])
        self.assertIn('Tema 612',' '.join(folha['conditions']))

    def test_recurring_coverage_of_the_annual_deficit(self):
        r=simulate(self.data)['recurring']
        self.assertEqual(r['annual_benefit'],3_953_012.98)
        self.assertEqual(r['annual_deficit'],7_200_000)
        self.assertAlmostEqual(r['coverage'],0.549,places=3)
        self.assertEqual(r['gap'],3_246_987.02)
        self.assertAlmostEqual(r['breakeven_das_cut_pct'],31.0,places=1)
        self.assertEqual(r['breakeven_das_positions'],56)
        self.assertAlmostEqual(r['breakeven_custeio_pct'],15.9,places=1)
        sem_deficit=dict(self.data,metrics={**self.data['metrics'],'balance':1_000_000})
        self.assertIsNone(simulate(sem_deficit)['recurring']['coverage'])
        self.assertIsNone(simulate(sem_deficit)['recurring']['breakeven_das_cut_pct'])

    def test_refis_text_does_not_promise_interest_and_fine_split(self):
        refis=next(a for a in simulate(self.data)['actions'] if a['id']=='refis_iptu')
        texto=' '.join(refis['conditions']+[refis['assumption']])
        self.assertIn('não separa principal, juros e multa',texto)
        self.assertIn('não prova que são devedores diferentes',texto)
        # teto definido pelo grupo
        self.assertIn('hipótese do grupo',texto)
        self.assertNotIn('limitado a 50% para a cobrança',texto.lower())

    def test_laws_are_cut_by_article_not_by_page_start(self):
        texto=('Art. 13. Algo antes. Art. 14. A concessão de benefício tributário com renúncia de receita '
               'deverá estar acompanhada de estimativa. Art. 15. Outro artigo. Art. 19. Limite de pessoal. '
               'Art. 20. Repartição.')
        achados={d['dispositivo']:d['trecho'] for d in extrair_dispositivos(texto,[14,19,99])}
        self.assertEqual(list(achados),['Art. 14','Art. 19'])
        self.assertIn('renúncia de receita',achados['Art. 14'])
        self.assertNotIn('Outro artigo',achados['Art. 14'])
        self.assertTrue(achados['Art. 19'].endswith('Limite de pessoal.'))

    def test_equilibrium_requires_each_year_not_only_a_positive_total(self):
        s=simulate(self.data,{'das_cut_pct':32})
        self.assertTrue(s['objective']['meets'])
        self.assertIsNone(s['objective']['first_deficit_year'])
        self.assertEqual(s['equilibrium']['status'],'covered')
        initial=simulate(self.data)
        self.assertGreater(initial['projected_balance'],0)
        self.assertFalse(initial['objective']['meets'])
        self.assertEqual(initial['objective']['first_deficit_year'],2)
        from municipal.simulacao import objetivo_equilibrio
        self.assertFalse(objetivo_equilibrio([
            {'year':1,'balance':100}, {'year':2,'balance':-1}])['meets'])

    def test_equilibrium_candidate_keeps_costs_and_closes_each_year(self):
        s=simulate(self.data)
        e=s['equilibrium']
        self.assertEqual(e['status'],'candidate')
        self.assertFalse(e['validated'])
        self.assertEqual(e['das_cut_pct'],32)
        self.assertEqual(e['inputs'],{**s['inputs'],'das_cut_pct':32.0})
        candidate=simulate(self.data,e['inputs'])
        self.assertTrue(all(y['balance']>=0 for y in candidate['horizon']))
        self.assertEqual(candidate['costs'],s['costs'])
        self.assertEqual(candidate['projected_balance'],e['first_year_balance'])
        self.assertEqual(candidate['horizon'][-1]['cumulative_balance'],e['cumulative_balance'])
        self.assertFalse(simulate(self.data,{**e['inputs'],'das_cut_pct':31})['objective']['meets'])
        self.assertFalse(e['conservative_meets'])
        self.assertLess(e['conservative_minimum_balance'],0)

    def test_equilibrium_candidate_excludes_temporary_replacement(self):
        s=simulate(self.data,{'attrition_pct':1,'temp_saving_pct':1})
        e=s['equilibrium']
        self.assertEqual(e['status'],'candidate')
        self.assertEqual(e['inputs']['attrition_pct'],0)
        self.assertEqual(e['inputs']['temp_saving_pct'],0)
        self.assertTrue(simulate(self.data,e['inputs'])['objective']['meets'])

    def test_equilibrium_does_not_claim_a_solution_when_costs_prevent_it(self):
        s=simulate(self.data,{'das_cost':1_000_000_000})
        self.assertFalse(s['objective']['meets'])
        self.assertEqual(s['equilibrium']['status'],'unavailable')
        self.assertNotIn('inputs',s['equilibrium'])

    def test_equilibrium_candidate_still_requires_individual_human_decisions(self):
        s=simulate(self.data)
        chosen=simulate(self.data,s['equilibrium']['inputs'])
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'db.sqlite3')
            run=store.save_run(dict(scenario=chosen,analysis=demo_analysis(self.data,chosen,[])))
            self.assertEqual(run['reviews'],{})
            self.assertFalse(run['approved_projection']['objective']['meets'])
            for a in chosen['actions']:
                run=store.review(run['id'],a['id'],'aprovada','Teste',
                                 'Conferido apenas para teste.','Aprovador de teste' if a.get('senior_approval') else '')
            self.assertTrue(run['approved_projection']['objective']['meets'])

    def test_ten_year_projection(self):
        s=simulate(self.data)
        h=s['horizon']
        self.assertEqual(len(h),10)
        self.assertEqual(h[0]['net'],s['net_benefit'])
        # receitas pontuais não se repetem
        self.assertLess(h[1]['net'],h[0]['net'])
        self.assertEqual(h[1]['net'],round(918_000+3_035_012.98,2))
        self.assertEqual(h[-1]['cumulative_net'],round(sum(x['net'] for x in h),2))
        zero={k:0 for k in DEFAULTS}
        self.assertTrue(all(x['net']==0 for x in simulate(self.data,zero)['horizon']))

    def test_invalid_inputs_rejected(self):
        for raw in [{'recovery_pct':101},{'recovery_pct':-1},{'collection_cost':None},
                    {'admin_pct':float('nan')},{'admin_pct':float('inf')},{'admin_pct':True},{'unknown':1}]:
            with self.subTest(raw=raw),self.assertRaises(ValueError):
                simulate(self.data,raw)
        for value in [None,True,'=SUM(A1:A2)',float('nan'),-1]:
            with self.subTest(value=value),self.assertRaises(DataError):
                money(value,'teste')

    def test_human_decisions_are_versioned_and_never_auto_approved(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'db.sqlite3')
            scenario=simulate(self.data)
            payload=dict(scenario=scenario,analysis=demo_analysis(self.data,scenario,[]))
            r1=store.save_run(payload)
            self.assertEqual(r1['approved_projection']['projected_balance'],-7_200_000)
            self.assertEqual(r1['reviews'],{})
            r1=store.review(r1['id'],'recuperar_divida','aprovada','Aluno Teste','Estimativa conferida para estudo.')
            self.assertEqual(r1['approved_projection']['projected_balance'],-454_000)
            r1=store.review(r1['id'],'revisar_custeio','aprovada','Aluno Teste','Base sujeita a entrevista.')
            self.assertEqual(r1['approved_projection']['projected_balance'],364_000)
            r2=store.save_run(payload)
            self.assertEqual(r2['reviews'],{})
            r1=store.review(r1['id'],'recuperar_divida','revisar','Aluno Teste','Prazo precisa ser confirmado.')
            self.assertEqual(r1['approved_projection']['projected_balance'],-6_382_000)
            self.assertEqual(len(r1['audit']),3)
            self.assertEqual(store.get_run(r2['id'])['reviews'],{})
            with self.assertRaises(ValueError):
                store.review(r1['id'],'inventada','aprovada','Teste','nao existe')
            with self.assertRaises(ValueError):
                store.review(r1['id'],'recuperar_divida','aprovada','','sem nome')

    def test_tax_proposal_needs_senior_approver_to_be_approved(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'db.sqlite3')
            scenario=simulate(self.data)
            run=store.save_run(dict(scenario=scenario,analysis=demo_analysis(self.data,scenario,[])))
            with self.assertRaises(ValueError):
                store.review(run['id'],'refis_iptu','aprovada','Aluno Teste','Estudo conferido com o cliente.')
            store.review(run['id'],'refis_iptu','revisar','Aluno Teste','Aguardando a Procuradoria.')
            run=store.review(run['id'],'refis_iptu','aprovada','Aluno Teste','Estudo conferido com o cliente.','Secretário de Finanças')
            self.assertEqual(run['reviews']['refis_iptu']['senior_approver'],'Secretário de Finanças')
            store.review(run['id'],'revisar_custeio','aprovada','Aluno Teste','Base sujeita a entrevista.')

    def test_default_model_is_gemini_and_needs_only_a_key(self):
        with patch.dict('os.environ',{},clear=True):
            status=ai_status()
            self.assertEqual(status['model'],'gemini/gemini-3.5-flash')
            self.assertFalse(status['configured'])
            self.assertIsNone(status['base_url'])
        with patch.dict('os.environ',{'LLM_API_KEY':'chave-de-teste'},clear=True):
            status=ai_status()
            self.assertTrue(status['configured'])
            self.assertEqual(status['ready'],status['installed'])
            self.assertIsNone(status['base_url'])

    def test_provider_errors_get_a_clear_message_and_transient_ones_are_retried(self):
        mensagem,temporario=classificar_erro(RuntimeError('Task execution failed: 503 UNAVAILABLE. high demand'),'gemini/x')
        self.assertTrue(temporario)
        self.assertIn('alta demanda',mensagem)
        self.assertFalse(classificar_erro(RuntimeError('404 NOT_FOUND no longer available'),'gemini/x')[1])
        self.assertFalse(classificar_erro(RuntimeError('401 API key not valid'),'gemini/x')[1])
        chamadas=[]
        def instavel(*args):
            chamadas.append(1)
            if len(chamadas)<3:
                raise RuntimeError('503 UNAVAILABLE')
            return dict(mode='ai')
        pronto=dict(ready=True,model='gemini/x',base_url=None)
        with patch.object(agente,'ai_status',return_value=pronto),patch.object(agente,'rodar_uma_vez',side_effect=instavel),\
             patch.object(agente,'reservas_configuradas',return_value=[]),patch.object(agente.time,'sleep') as espera:
            self.assertEqual(agente.run_agent({},{},[],None),dict(mode='ai',engine='CrewAI / gemini/x'))
            self.assertEqual(len(chamadas),3)
            self.assertEqual([c.args[0] for c in espera.call_args_list],[5,15])
        with patch.object(agente,'ai_status',return_value=pronto),patch.object(agente,'reservas_configuradas',return_value=[]),\
             patch.object(agente,'rodar_uma_vez',side_effect=RuntimeError('404 NOT_FOUND segredo-do-provedor')):
            with self.assertRaises(AgentError) as ctx:
                agente.run_agent({},{},[],None)
            self.assertNotIn('segredo-do-provedor',str(ctx.exception))
            self.assertIn('não está disponível',str(ctx.exception))

    def test_fallback_chain_uses_the_next_model_and_records_why(self):
        pronto=dict(ready=True,model='gemini/principal',base_url=None)
        reservas=[dict(tipo='crewai',model='gemini/lite',label='CrewAI / gemini/lite (reserva)'),
                  dict(tipo='groq',model='openai/gpt-oss-120b',label='Groq / openai/gpt-oss-120b (reserva, dados resumidos)')]
        modelos=[]
        def crewai(dataset,scenario,sources,project,status,publicos=None):
            modelos.append(status['model'])
            raise RuntimeError('503 UNAVAILABLE high demand')
        def groq(dataset,scenario,sources,project,modelo,label,publicos=None):
            modelos.append(modelo)
            return dict(mode='ai',summary='ok',priorities=[],questions=[],source_ids=[],tool_log=[])
        with patch.object(agente,'ai_status',return_value=pronto),patch.object(agente,'reservas_configuradas',return_value=reservas),\
             patch.object(agente,'rodar_uma_vez',side_effect=crewai),patch.object(agente,'rodar_groq',side_effect=groq),\
             patch.object(agente.time,'sleep'):
            r=agente.run_agent({},{},[],None)
        # O principal e o Gemini reserva tentam 3 vezes cada (erro passageiro); o Groq responde.
        self.assertEqual(modelos,['gemini/principal']*3+['gemini/lite']*3+['openai/gpt-oss-120b'])
        self.assertIn('Groq',r['engine'])
        self.assertEqual([f['engine'] for f in r['fallback']['failures']],['CrewAI / gemini/principal','CrewAI / gemini/lite (reserva)'])
        self.assertIn('alta demanda',r['fallback']['failures'][0]['reason'])

    def test_daily_quota_skips_retries_and_goes_straight_to_the_reserve(self):
        pronto=dict(ready=True,model='gemini/principal',base_url=None)
        reservas=[dict(tipo='groq',model='openai/gpt-oss-120b',label='Groq / reserva')]
        cota=RuntimeError('429 RESOURCE_EXHAUSTED Quota exceeded ... Please retry in 10h10m50.3s.')
        with patch.object(agente,'ai_status',return_value=pronto),patch.object(agente,'reservas_configuradas',return_value=reservas),\
             patch.object(agente,'rodar_uma_vez',side_effect=cota) as principal,\
             patch.object(agente,'rodar_groq',return_value=dict(mode='ai',tool_log=[])),\
             patch.object(agente.time,'sleep') as espera:
            r=agente.run_agent({},{},[],None)
        self.assertEqual(principal.call_count,1)   # não gasta mais requisições de uma cota que acabou
        espera.assert_not_called()
        self.assertIn('10 h 10 min',r['fallback']['failures'][0]['reason'])
        mensagem,temporario=classificar_erro(RuntimeError('429 rate limit. Please try again in 7.6s'),'groq/x')
        self.assertTrue(temporario)   # limite por minuto continua sendo passageiro

    def test_all_models_failing_raises_without_leaking_provider_text(self):
        pronto=dict(ready=True,model='gemini/principal',base_url=None)
        reservas=[dict(tipo='groq',model='openai/gpt-oss-120b',label='Groq / reserva')]
        with patch.object(agente,'ai_status',return_value=pronto),patch.object(agente,'reservas_configuradas',return_value=reservas),\
             patch.object(agente,'rodar_uma_vez',side_effect=RuntimeError('401 API key not valid segredo')),\
             patch.object(agente,'rodar_groq',side_effect=RuntimeError('404 model_not_found segredo')):
            with self.assertRaises(AgentError) as ctx:
                agente.run_agent({},{},[],None)
        texto=str(ctx.exception)
        self.assertIn('Nenhum modelo concluiu',texto)
        self.assertIn('Groq / reserva',texto)
        self.assertNotIn('segredo',texto)

    def test_reserves_come_from_env(self):
        with patch.dict('os.environ',{},clear=True):
            self.assertEqual(agente.reservas_configuradas(),[])
        with patch.dict('os.environ',{'LLM_RESERVA_MODEL':'gemini/gemini-3.5-flash-lite','GROQ_API_KEY':'gsk_teste'},clear=True):
            r=agente.reservas_configuradas()
            self.assertEqual([x['tipo'] for x in r],['crewai','groq'])
            self.assertEqual(r[1]['model'],'openai/gpt-oss-120b')
            self.assertEqual(agente.redigir('chave gsk_teste no log'),'chave *** no log')

    def test_compact_tool_outputs_fit_the_groq_limit(self):
        scenario=simulate(self.data)
        fontes=[dict(id='lrf',title='LRF',status='consultada',
                     dispositivos=[dict(dispositivo=f'Art. {n}',trecho='x'*2500) for n in (14,19,20,22,42)])]
        projeto=dict(answers=['resposta longa '*40]*11)
        tamanho=sum(len(json.dumps(x,ensure_ascii=False)) for x in (
            agente.resumir_indicadores(self.data,projeto),agente.resumir_simulacao(scenario),agente.resumir_fontes(fontes)))
        # Cerca de 3,2 caracteres por token: precisa ficar bem abaixo dos 7.000 tokens por minuto.
        self.assertLess(tamanho/3.2,5000)
        ids={a['id'] for a in agente.resumir_simulacao(scenario)['actions']}
        self.assertEqual(ids,{a['id'] for a in scenario['actions']})

    @unittest.skipUnless(importlib.util.find_spec('openai'),'openai vem de requirements-ai.txt')
    def test_groq_loop_forces_the_four_tools_and_a_json_answer(self):
        scenario=simulate(self.data)
        fontes=[dict(id='lrf',title='LRF',status='consultada',dispositivos=[])]
        pedidos=[]
        class Uso: total_tokens=100
        class Chamada:
            def __init__(self,i,nome,argumentos='{}'):
                self.id=f'c{i}'; self.function=type('F',(),{'name':nome,'arguments':argumentos})()
            def model_dump(self):
                return dict(id=self.id,type='function',function=dict(name=self.function.name,arguments=self.function.arguments))
        def resposta(mensagem):
            return type('R',(),{'choices':[type('C',(),{'message':mensagem})()],'usage':Uso()})()
        final=json.dumps(dict(summary='Resumo da análise com propostas.',
            priorities=[dict(action_id=a['id'],reason='Conferir premissas e custos.') for a in scenario['actions']],
            questions=['Quais cargos podem sair?'],source_ids=['lrf']))
        class Cliente:
            def __init__(self,**kw): self.chat=self; self.completions=self
            def create(self,**kw):
                pedidos.append(kw)
                if kw['tool_choice']=='required':
                    nomes=['consultar_indicadores','consultar_simulacao','consultar_fontes_oficiais']
                    chamadas=[Chamada(i,n) for i,n in enumerate(nomes)]+[Chamada(3,'consultar_dados_publicos','{"indicador":"tudo"}')]
                    return resposta(type('M',(),{'content':None,'tool_calls':chamadas})())
                return resposta(type('M',(),{'content':final,'tool_calls':None})())
        import openai
        with patch.object(openai,'OpenAI',Cliente),patch.dict('os.environ',{'GROQ_API_KEY':'gsk_teste'}):
            r=agente.rodar_groq(self.data,scenario,fontes,None,'openai/gpt-oss-120b','Groq / teste')
        self.assertEqual([p['tool_choice'] for p in pedidos],['required','none'])
        self.assertEqual(pedidos[-1]['response_format'],{'type':'json_object'})
        self.assertEqual({c['tool'] for c in r['tool_log']},agente.FERRAMENTAS_OBRIGATORIAS)
        self.assertEqual(r['engine'],'Groq / teste')

    def test_missing_external_sources_remain_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'sources.json'
            self.assertTrue(all(s['status']=='nao_consultada' for s in source_status(path)))
            with patch('municipal.fontes.urlopen',side_effect=TimeoutError('sem rede')):
                result=refresh_sources(path)
            self.assertTrue(all(s['status']=='indisponivel' for s in result))
            self.assertTrue(all(s['excerpt']=='' for s in result))
            self.assertEqual(source_status(path),result)


if __name__=='__main__':
    unittest.main()
