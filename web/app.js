'use strict';


const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ESCAPES[c]);

const brl = value => new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL', maximumFractionDigits: 0 }).format(value);
const num = value => new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 1 }).format(value);
const pct = value => value === null ? 'n.d.' : new Intl.NumberFormat('pt-BR', { style: 'percent', maximumFractionDigits: 1 }).format(value);
const date = value => value
  ? new Intl.DateTimeFormat('pt-BR', { dateStyle: 'short', timeStyle: 'short', timeZone: 'America/Sao_Paulo' }).format(new Date(value))
  : 'Ainda não consultada';

const PAGES = ['data', 'simulation', 'review', 'project'];
const DECISIONS = {
  aprovada: { label: 'Aprovada', kind: 'green' },
  rejeitada: { label: 'Rejeitada', kind: 'red' },
  revisar: { label: 'Revisão solicitada', kind: 'yellow' },
  pendente: { label: 'Pendente', kind: '' },
};
const SOURCE_STATUS = {
  consultada: { label: 'Consultada', kind: 'green' },
  nao_consultada: { label: 'Ainda não consultada', kind: '' },
  indisponivel: { label: 'Consulta indisponível', kind: 'red' },
};

const tag = (text, kind = '') => `<span class="tag ${kind}">${esc(text)}</span>`;
const money = (label, value, negative = false) =>
  `<div><small>${label}</small><strong class="${negative ? 'negative-text' : ''}">${brl(value)}</strong></div>`;


let state;
let scenario;            // simulação atual
let currentRun;          // análise aberta na tela de revisão
let running = false;
let simulationValid = true;
let simulationSequence = 0;
let toastTimer;
const openCards = new Set();   // propostas abertas na revisão; não fecham ao registrar uma decisão

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Não foi possível concluir a operação.');
  return result;
}

function toast(message, error = false) {
  clearTimeout(toastTimer);
  const el = $('#toast');
  el.textContent = message;
  el.className = 'toast' + (error ? ' error' : '');
  el.hidden = false;
  toastTimer = setTimeout(() => { el.hidden = true; }, error ? 9000 : 4500);
}

function showPage(page) {
  if (!PAGES.includes(page)) page = 'data';
  $$('.page').forEach(el => el.classList.toggle('active', el.id === 'page-' + page));
  $$('.nav-item').forEach(el => {
    const active = el.dataset.page === page;
    el.classList.toggle('active', active);
    if (active) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current');
  });
  $('#stepper-box').hidden = page === 'project';
  const passo = { data: 1, simulation: 2, review: 3 }[page];
  if (passo) $('#step-count').textContent = `Passo ${passo} de 3`;
  $$('#stepper button').forEach(el => {
    const current = el.dataset.page === page;
    el.classList.toggle('current', current);
    if (current) el.setAttribute('aria-current', 'step'); else el.removeAttribute('aria-current');
  });
  history.replaceState(null, '', '#' + page);
  window.scrollTo({ top: 0, behavior: 'instant' });
}


// Resumo do topo: o resultado em destaque e os principais problemas; os detalhes ficam nos cartões abaixo.
function renderMetrics() {
  const m = state.dataset.metrics;
  const findings = state.dataset.situation;
  const problemas = findings.filter(f => f.severity === 'alerta' && f.id !== 'resultado').slice(0, 4);
  const negativo = m.balance < 0;
  $('#summary').innerHTML = `
    <div class="summary-main ${negativo ? 'negative' : ''}">
      <div class="tile-label">RESULTADO DO ANO</div>
      <div class="summary-value">${brl(m.balance)}</div>
      <p>${negativo ? 'A despesa liquidada' : 'A receita arrecadada'} (${brl(negativo ? m.expenses : m.revenue)}) superou ${negativo ? 'a receita arrecadada' : 'a despesa liquidada'} (${brl(negativo ? m.revenue : m.expenses)}). É um saldo simples: não é caixa nem resultado primário.</p>
    </div>
    <div class="summary-side">
      <div class="tile-label">PRINCIPAIS PROBLEMAS</div>
      <ul class="summary-list">${problemas.map(f => `<li><span>${esc(f.title)}</span><strong class="${tone(f)}">${FORMAT[f.kind](f.value)}</strong></li>`).join('')}</ul>
      <p class="field-hint">Vermelho: déficit. Laranja: arrecadação abaixo do previsto. Preto: valor a receber, como a dívida ativa. Cada ponto está explicado nos cartões abaixo.</p>
    </div>`;
  const folha = findings.find(f => f.id === 'peso_da_folha');
  const tiles = [
    ['Receita arrecadada', brl(m.revenue), 'Em 12 meses'],
    ['Despesa liquidada', brl(m.expenses), 'Entregue, nem sempre já paga'],
    ['Dívida ativa', brl(m.debt_stock), 'A receber; não é caixa'],
    ['Folha sobre a receita', folha ? pct(folha.value) : '—', 'Razão simples, não é o teste da LRF'],
  ];
  $('#metrics').innerHTML = tiles.map(([label, value, note]) => `
    <div class="tile metric">
      <div class="tile-label">${esc(label)}</div>
      <div class="metric-value">${value}</div>
      <p class="field-hint">${esc(note)}</p>
    </div>`).join('');
}

const SEVERITY = {
  alerta: { label: 'Alerta', kind: 'red' },
  atencao: { label: 'Atenção', kind: 'yellow' },
  info: { label: 'Informativo', kind: 'blue' },
};
const FORMAT = { money: brl, percent: pct };
// Cor do valor pelo tipo de problema: déficit, arrecadação abaixo do previsto ou estoque a receber.
const TONE = { resultado: 'deficit', despesa_acima_da_dotacao: 'deficit', receita_abaixo_do_previsto: 'shortfall',
  convenios_e_obras: 'shortfall', divida_ativa: 'stock' };
const tone = f => 'tone-' + (TONE[f.id] || 'neutral');

function renderSituation() {
  const findings = state.dataset.situation;
  const alerts = findings.filter(f => f.severity === 'alerta').length;
  $('#situation-count').textContent = `${alerts} alertas em ${findings.length} pontos`;
  $('#situation').innerHTML = findings.filter(f => f.id !== 'resultado').map(f => {
    const severity = SEVERITY[f.severity];
    const items = f.items.length
      ? `<ul>${f.items.map(i => `<li><span>${esc(i.label)}</span><strong>${FORMAT[i.kind](i.value)}</strong></li>`).join('')}</ul>`
      : '';
    return `<div class="tile finding ${esc(f.severity)}">
      <div class="tile-header"><div class="tile-label">${esc(f.title).toUpperCase()}</div>${tag(severity.label, severity.kind)}</div>
      <div class="tile-value ${tone(f)}">${FORMAT[f.kind](f.value)}</div>
      <p>${esc(f.note)}</p>${items}
      <span class="cell-ref">${esc(f.source)}</span>
    </div>`;
  }).join('');
}

function renderSpend() {
  const departments = state.dataset.departments;
  const max = Math.max(...departments.map(d => d.actual), 1);
  const width = value => (100 * Math.max(value, 0) / max).toFixed(2);
  $('#spend-chart').innerHTML = departments.map(d => `
    <div class="stack-row"><span>${esc(d.name)}</span>
      <div class="stack-track">
        <span class="payroll" data-width="${width(d.payroll)}"></span>
        <span class="running" data-width="${width(d.eligible)}"></span>
        <span class="invest" data-width="${width(d.investment)}"></span>
      </div>
      <span class="bar-value">${num(d.actual / 1e6)}</span></div>`).join('');
  $$('#spend-chart [data-width]').forEach(el => el.style.setProperty('--w', el.dataset.width + '%'));
}

function renderWarnings() {
  const warnings = state.dataset.warnings;
  $('#warning-count').textContent = warnings.length + ' pontos de atenção';
  $('#all-warnings').innerHTML = warnings.map((w, i) => `
    <details class="accordion"${i === 0 ? ' open' : ''}>
      <summary><strong>${esc(w.title)}</strong></summary>
      <div class="body"><p>${esc(w.text)}</p><span class="cell-ref">${esc(w.source)}</span></div>
    </details>`).join('');
}

function renderTables() {
  const d = state.dataset;
  const label = (name, source) => `${esc(name)}<span class="cell-ref">${esc(source)}</span>`;
  $('#revenue-table').innerHTML = d.revenues.map(r => `
    <tr><td>${label(r.name, r.source)}</td><td class="num">${brl(r.planned)}</td>
        <td class="num">${brl(r.actual)}</td><td class="num">${pct(r.execution)}</td></tr>`).join('');
  $('#payroll-table').innerHTML = d.payroll_groups.map(g => `
    <tr><td>${esc(g.category)}</td><td>${esc(g.link)}</td>
        <td class="num">${g.count}</td><td class="num">${brl(g.monthly_cost)}</td></tr>`).join('');
  $('#expenses-table').innerHTML = d.departments.map(r => `
    <tr><td>${label(r.name, r.source)}</td><td class="num">${brl(r.actual)}</td>
        <td class="num">${brl(r.payroll)}</td><td class="num">${brl(r.investment)}</td></tr>`).join('');
}

function renderSources() {
  const d = state.dataset;
  $('#dataset-info').innerHTML = `<strong>Planilha</strong><p>${esc(d.filename)} · ${d.metrics.staff_count} servidores · SHA-256 ${esc(d.fingerprint.slice(0, 20))}… · cópia local, sem alterações no original.</p>`;
  $('#source-cards').innerHTML = state.sources.map(s => {
    const status = SOURCE_STATUS[s.status] || { label: s.status, kind: '' };
    return `<div class="tile">
      ${tag(status.label, status.kind)}
      <h3>${esc(s.title)}</h3>
      <p>${esc(s.purpose)}</p>
      <p class="field-hint">${esc(s.note)}</p>
      ${(s.dispositivos || []).length ? `<p class="field-hint">Trechos recuperados: ${s.dispositivos.map(d => esc(d.dispositivo)).join(', ')}.</p>` : ''}
      <p class="field-hint"><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">Abrir fonte oficial ↗</a>
        · ${date(s.checked_at)}${s.content_sha256 ? ' · hash ' + esc(s.content_sha256.slice(0, 12)) : ''}</p>
    </div>`;
  }).join('');
}

// Dados públicos do município real (SICONFI e IBGE). Vêm do cache do servidor; a tela não consulta a rede.
const EXTERNAL_STATUS = { ...SOURCE_STATUS, sem_dados: { label: 'Sem dados publicados', kind: 'yellow' } };
const reais = value => new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(value);
const pp = value => new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 2 }).format(value) + '%';
const day = value => new Intl.DateTimeFormat('pt-BR', { dateStyle: 'short', timeZone: 'America/Sao_Paulo' }).format(new Date(value));
const semUF = nome => String(nome || '').replace(/ - [A-Z]{2}$/, '');
const ENTREGA = { HO: 'homologado', RE: 'retificado' };

function externalHeader(label, r) {
  const status = EXTERNAL_STATUS[r.status] || { label: r.status, kind: '' };
  return `<div class="tile-header"><div class="tile-label">${esc(label)}</div>
    <span>${r.desatualizado ? tag('Desatualizado', 'yellow') + ' ' : ''}${tag(status.label, status.kind)}</span></div>
    ${r.desatualizado ? `<div class="notice warning"><strong>Dado desatualizado: não é o valor mais recente.</strong>
      <p>Coletado em ${date(r.checked_at)}. A última tentativa de atualizar não deu certo ou a validade venceu; veja a nota abaixo.</p></div>` : ''}`;
}

function externalFooter(r, periodo) {
  const quando = r.checked_at ? (r.status === 'consultada' ? 'Coletado em ' : 'Última tentativa em ') + date(r.checked_at) : '';
  const meta = [periodo, quando].filter(Boolean).join(' · ');
  return `${meta ? `<p class="field-hint">${meta}</p>` : ''}<p class="field-hint">${esc(r.note)}</p>`;
}

// Faixa em que a razão caiu, pelos limites do próprio demonstrativo. Descreve; não é parecer de conformidade.
function faixaPessoal(v) {
  const razao = v.despesa_pessoal_pct_rcl_ajustada;
  if (razao < v.limite_alerta_pct_rcl_ajustada) return `abaixo do limite de alerta publicado (${pp(v.limite_alerta_pct_rcl_ajustada)})`;
  if (razao < v.limite_prudencial_pct_rcl_ajustada) return `entre o limite de alerta (${pp(v.limite_alerta_pct_rcl_ajustada)}) e o limite prudencial (${pp(v.limite_prudencial_pct_rcl_ajustada)}) publicados`;
  if (razao < v.limite_maximo_pct_rcl_ajustada) return `entre o limite prudencial (${pp(v.limite_prudencial_pct_rcl_ajustada)}) e o limite máximo (${pp(v.limite_maximo_pct_rcl_ajustada)}) publicados`;
  return `no limite máximo publicado (${pp(v.limite_maximo_pct_rcl_ajustada)}) ou acima dele`;
}

function renderRcl(r) {
  const v = r.valores || {};
  if (r.status !== 'consultada') return `<div class="tile">${externalHeader('RCL e limite de pessoal', r)}${externalFooter(r)}</div>`;
  const e = v.escolha_periodo || {};
  const periodo = `RGF ${v.exercicio}, ${v.periodo}º ${v.periodicidade === 'S' ? 'semestre' : 'quadrimestre'}${e.demonstrativo ? ' · ' + esc(e.demonstrativo) : ''}`
    + (e.status_relatorio ? ` · ${ENTREGA[e.status_relatorio] || esc(e.status_relatorio)} em ${day(e.data_status)}` : '');
  const c = v.conferencia_rreo;
  const conferencia = !c ? '' : c.situacao === 'confere' ? 'A RCL confere com a do RREO do mesmo período.'
    : c.situacao === 'diverge' ? `A RCL difere da do RREO do mesmo período (${reais(c.rcl_rreo)}); veja a nota.` : 'A conferência pelo RREO não pôde ser feita.';
  const limite = (nome, valor, pct) => `<li><span>${nome} (${pp(pct)} da RCL ajustada)</span><strong>${reais(valor)}</strong></li>`;
  return `<div class="tile">
    ${externalHeader('RCL e limite de pessoal', r)}
    <div class="tile-value">${pp(v.despesa_pessoal_pct_rcl_ajustada)}</div>
    <p>Despesa com pessoal sobre a RCL ajustada: ${faixaPessoal(v)}. Leitura dos números publicados, não parecer sobre a lei.</p>
    <ul class="summary-list">
      <li><span>RCL</span><strong>${reais(v.rcl)}</strong></li>
      <li><span>RCL ajustada</span><strong>${reais(v.rcl_ajustada)}</strong></li>
      <li><span>Despesa com pessoal</span><strong>${reais(v.despesa_pessoal)}</strong></li>
      ${limite('Limite máximo', v.limite_maximo, v.limite_maximo_pct_rcl_ajustada)}
      ${limite('Limite prudencial', v.limite_prudencial, v.limite_prudencial_pct_rcl_ajustada)}
      ${limite('Limite de alerta', v.limite_alerta, v.limite_alerta_pct_rcl_ajustada)}
    </ul>
    ${conferencia ? `<p class="field-hint">${conferencia}</p>` : ''}
    ${externalFooter(r, periodo)}
  </div>`;
}

function renderBenchmark(r) {
  const v = r.valores || {};
  if (r.status !== 'consultada') return `<div class="tile">${externalHeader('RCL por habitante entre municípios parecidos', r)}${externalFooter(r)}</div>`;
  const m = v.municipio;
  const p = v.posicao;
  const excluidos = v.excluidos.length
    ? `<p class="field-hint">Ficaram de fora por falta de dado: ${v.excluidos.map(e => esc(semUF(e.nome))).join(', ')}.</p>` : '';
  return `<div class="tile">
    ${externalHeader('RCL por habitante entre municípios parecidos', r)}
    <div class="tile-value">${reais(m.rcl_per_capita)}</div>
    <p>${esc(semUF(m.nome))}, ${v.exercicio}. Mediana de ${v.pares.length} municípios do ES com população semelhante: <strong>${reais(v.mediana_pares)}</strong>.
      ${pp(Math.abs(p.diferenca_mediana_pct))} ${p.diferenca_mediana_pct >= 0 ? 'acima' : 'abaixo'} da mediana; ${p.ordem}º de ${p.total}, do maior para o menor.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Município</th><th class="num">População</th><th class="num">RCL por habitante</th></tr></thead>
      <tbody>${v.pares.map(x => `<tr><td>${esc(semUF(x.nome))}</td><td class="num">${num(x.populacao)}</td><td class="num">${reais(x.rcl_per_capita)}</td></tr>`).join('')}</tbody>
    </table></div>
    ${excluidos}
    ${externalFooter({ ...r, note: r.note.split(' Aviso: ')[0] }, `População IBGE ${v.exercicio}`)}
  </div>`;
}

function renderExternal() {
  const { rcl, benchmark } = state.external;
  const c = (benchmark.valores || {}).comparacao_estudo;
  $('#external-notice').innerHTML = c ? `<div class="notice"><strong>Os números da planilha não são os do município real</strong>
    <p>${esc(c.aviso)}</p></div>` : '';
  $('#external').innerHTML = renderRcl(rcl) + renderBenchmark(benchmark);
  // "Tentar de novo agora" ignora o recuo de 5 minutos depois de uma falha; só aparece quando há falha ou dado velho.
  $('#retry-external').hidden = ![rcl, benchmark].some(r => ['indisponivel', 'sem_dados'].includes(r.status) || r.desatualizado);
  annotate($('#external'));
}


const SLIDERS = ['recovery_pct', 'admin_pct', 'environment_pct', 'security_pct',
  'das_cut_pct', 'attrition_pct', 'temp_saving_pct', 'refis_adhesion_pct', 'refis_discount_pct'];

const ONE_TIME = new Set(['recuperar_divida', 'refis_iptu']);

// Mostra no fim de cada bloco quanto a proposta rende com os valores escolhidos.
function renderLeverEffects() {
  for (const el of $$('[data-effect]')) {
    const a = scenario.actions.find(x => x.id === el.dataset.effect);
    if (!a) continue;
    const negative = a.net < 0;
    let text = ONE_TIME.has(a.id)
      ? `Rende ${brl(a.net)} uma vez, no ano 1, já descontado o custo.`
      : `Rende ${brl(a.yearly[1])} por ano a partir do ano 2 (${brl(a.net)} no ano 1, já descontado o custo).`;
    if (a.id === 'reduzir_folha') {
      const m = state.dataset.metrics;
      const cargos = Math.round(m.das_count * Number($('#das_cut_pct').value) / 100);
      text += ` Equivale a cerca de ${cargos} dos ${m.das_count} cargos, pelo custo médio.`;
    }
    el.textContent = text;
    el.classList.toggle('negative-text', negative);
  }
}

function renderLeverBases() {
  for (const el of $$('[data-base]')) {
    const d = state.dataset.departments.find(x => x.id === el.dataset.base);
    if (d) el.textContent = `(base ${brl(d.eligible)})`;
  }
}

// Resumo das opções avançadas, visível mesmo com a seção fechada.
function renderAdvancedSummary() {
  const costs = ['collection_cost', 'savings_cost', 'das_cost', 'refis_cost'].reduce((sum, k) => sum + (Number($('#' + k).value) || 0), 0);
  const attrition = Number($('#attrition_pct').value);
  const saving = Number($('#temp_saving_pct').value);
  const temporarios = attrition > 0 && saving > 0 ? `ligada (${num(attrition)}% saem por ano, ${num(saving)}% de economia)` : 'desligada';
  $('#advanced-summary').textContent = `Custos: ${brl(costs)} no total · Reposição por temporários: ${temporarios}`;
}

function updateOutputs() {
  for (const key of SLIDERS) $('#out-' + key).textContent = num(Number($('#' + key).value)) + '%';
  renderAdvancedSummary();
}

function getInputs() {
  const entries = [...new FormData($('#scenario-form')).entries()];
  if (entries.some(([, value]) => value.trim() === '' || !Number.isFinite(Number(value)))) {
    throw new Error('Preencha todos os custos com números válidos.');
  }
  return Object.fromEntries(entries.map(([key, value]) => [key, Number(value)]));
}

function renderEquilibrium(s) {
  const e = s.equilibrium;
  if (e.status === 'covered') return '';
  if (e.status === 'unavailable') return `<div class="tile negative">
    <h2>O ajuste de cargos não fecha a meta</h2><p>${esc(e.reason)}</p></div>`;
  return `<div class="tile">
    <h2>Alternativa de equilíbrio para estudar</h2>
    <p>O menor corte inteiro dos gastos com cargos de confiança que fecha os dez anos, mantendo as outras premissas, é <strong>${num(e.das_cut_pct)}%</strong>. Equivale a cerca de ${e.equivalent_positions} cargos pelo custo médio; depende de quais cargos saem.</p>
    <p class="field-hint">${esc(e.method)}</p>
    <div class="money-row">
      ${money('Saldo no primeiro ano', e.first_year_balance)}
      ${money('Menor saldo dos anos seguintes', e.recurring_balance)}
      ${money('Saldo acumulado em 10 anos', e.cumulative_balance)}
    </div>
    <p>${esc(e.condition)}</p>
    <p class="field-hint">Com metade dos percentuais de ganho, o menor saldo anual seria ${brl(e.conservative_minimum_balance)}. ${e.conservative_meets ? 'A meta continua atendida nesse teste.' : 'A meta não é atendida nesse teste; o resultado depende de realizar as economias simuladas.'}</p>
    <button type="button" class="button secondary" data-equilibrium>Carregar alternativa para estudar</button>
    <p class="field-hint">Depois de carregar, gere uma nova análise e revise cada proposta. Nenhuma aprovação é automática.</p>
  </div>`;
}

function renderScenario() {
  const s = scenario;
  const covers = s.projected_balance >= 0;
  $('#scenario-results').innerHTML = `
    <div class="tile ${s.objective.meets ? '' : 'negative'}">
      <h2>Meta do cliente: equilíbrio nos dez anos</h2>
      <p><strong>${s.objective.meets ? 'Atendida nas contas desta simulação.' : `Ainda não atendida: há déficit a partir do ano ${s.objective.first_deficit_year}.`}</strong></p>
      <p class="field-hint">O critério é ter saldo não negativo em cada um dos dez anos. O menor saldo anual é ${brl(s.objective.minimum_balance)}. Um acumulado positivo, sozinho, não basta. A viabilidade depende da validação das medidas e dos dados.</p>
    </div>
    <div class="tile ${covers ? '' : 'negative'}">
      <div class="tile-label">RESULTADO PROJETADO EM 12 MESES</div>
      <div class="tile-value">${brl(s.projected_balance)}</div>
      <p class="field-hint">${covers ? 'As hipóteses superam a diferença negativa do arquivo.' : 'As hipóteses ainda não cobrem a diferença negativa do arquivo.'} Resultado condicionado à validação.</p>
      <div class="money-row">
        ${money('Benefício bruto', s.gross_benefit)}
        ${money('Custos estimados', s.costs)}
        ${money('Benefício líquido', s.net_benefit, s.net_benefit < 0)}
      </div>
    </div>
    <div class="tile">
      <h2>Composição do impacto no primeiro ano</h2>
      ${s.actions.map(a => `<p><strong>${esc(a.title)}</strong>: ${a.qualitative ? 'sem valor estimado' : brl(a.net)} <small>(${esc(a.category)}${a.qualitative ? '' : ', líquido de custos'})</small></p>`).join('')}
      <p class="field-hint">Saldo inicial ${brl(s.baseline)} + benefício líquido ${brl(s.net_benefit)}. Recuperação extraordinária não resolve, sozinha, um desequilíbrio recorrente.</p>
    </div>
    <div class="tile ${s.recurring.gap > 0 ? 'negative' : ''}">
      <h2>O plano cobre o déficit que se repete todo ano?</h2>
      <div class="tile-value">${s.recurring.coverage === null ? 'n.d.' : pct(s.recurring.coverage)}</div>
      <p>Depois do ano 1, as medidas que se repetem rendem ${brl(s.recurring.annual_benefit)} por ano, contra um déficit anual de ${brl(s.recurring.annual_deficit)}. ${s.recurring.gap > 0 ? `Faltam ${brl(s.recurring.gap)} por ano.` : 'O déficit anual fica coberto.'}</p>
      ${s.recurring.gap > 0 && s.recurring.breakeven_das_cut_pct !== null ? `<p class="field-hint">Só como referência: mantendo a economia simulada nos gastos de funcionamento, o corte dos gastos com cargos de confiança precisaria chegar a cerca de ${num(s.recurring.breakeven_das_cut_pct)}% (equivale a uns ${s.recurring.breakeven_das_positions} cargos pelo custo médio; a economia real depende de quais cargos forem cortados)${s.recurring.breakeven_custeio_pct !== null ? `. Mantendo o corte de cargos simulado, a redução uniforme dos gastos de funcionamento nas três secretarias precisaria chegar a cerca de ${num(s.recurring.breakeven_custeio_pct)}%` : ''}. Não são propostas e não incluem custos de transição.</p>` : ''}
      <p class="field-hint">Dívida ativa e Refis rendem uma vez só e não entram nessa conta.</p>
    </div>
    <div class="tile">
      <h2>Projeção em ${s.horizon_years} anos</h2>
      <p class="field-hint">Valores constantes, sem inflação nem crescimento. Receitas pontuais (dívida e Refis) só entram no ano 1; gastos de funcionamento e pessoal se repetem.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Ano</th><th class="num">Benefício líquido</th><th class="num">Saldo do ano</th><th class="num">Saldo acumulado</th></tr></thead>
        <tbody>${s.horizon.map(r => `<tr><td>${r.year}</td><td class="num">${brl(r.net)}</td>
          <td class="num ${r.balance < 0 ? 'negative-text' : ''}">${brl(r.balance)}</td>
          <td class="num ${r.cumulative_balance < 0 ? 'negative-text' : ''}">${brl(r.cumulative_balance)}</td></tr>`).join('')}</tbody>
      </table></div>
    </div>
    ${renderEquilibrium(s)}
    <div class="tile">
      <h2>E se as hipóteses forem menores ou maiores?</h2>
      <p class="field-hint">Os percentuais são hipóteses do grupo, então o resultado aparece em três cenários. O conservador usa metade dos percentuais de ganho; o otimista, 50% a mais. Os custos e o desconto do Refis não mudam.</p>
      <div class="table-wrap"><table>
        <thead><tr><th></th>${s.scenarios.map(c => `<th class="num">${esc(c.label)}</th>`).join('')}</tr></thead>
        <tbody>
          <tr><td>Resultado em 12 meses</td>${s.scenarios.map(c => `<td class="num ${c.projected_balance < 0 ? 'negative-text' : ''}">${brl(c.projected_balance)}</td>`).join('')}</tr>
          <tr><td>Resultado no ano 10</td>${s.scenarios.map(c => `<td class="num ${c.balance_year10 < 0 ? 'negative-text' : ''}">${brl(c.balance_year10)}</td>`).join('')}</tr>
          <tr><td>Saldo acumulado em 10 anos</td>${s.scenarios.map(c => `<td class="num ${c.cumulative_balance < 0 ? 'negative-text' : ''}">${brl(c.cumulative_balance)}</td>`).join('')}</tr>
        </tbody>
      </table></div>
    </div>
    <div class="notice"><strong>Restrições do cliente</strong>
      <ul class="detail-list">${s.constraints.map(c => `<li>${esc(c.text)}</li>`).join('')}</ul></div>`;
  renderSticky();
  renderLeverEffects();
  annotate($('#scenario-results'));
}

// Painel fixo: resultado, meta e cobertura continuam visíveis enquanto os controles rolam.
function renderSticky() {
  const s = scenario;
  const o = s.objective;
  const r = s.recurring;
  const box = $('#sim-sticky');
  box.className = 'sim-sticky ' + (o ? (o.meets ? 'good' : 'bad') : '');
  box.innerHTML = `
    <div><span class="tile-label">Resultado em 12 meses</span><span class="sticky-value ${s.projected_balance < 0 ? 'tone-deficit' : ''}">${brl(s.projected_balance)}</span></div>
    <div><span class="tile-label">Meta dos 10 anos</span><span class="sticky-value">${o ? (o.meets ? 'Atendida' : `Não atendida (déficit no ano ${o.first_deficit_year})`) : '—'}</span></div>
    <div><span class="tile-label">Déficit recorrente coberto</span><span class="sticky-value">${r && r.coverage !== null ? pct(r.coverage) : '—'}</span></div>`;
}

async function updateScenario() {
  const sequence = ++simulationSequence;
  simulationValid = false;
  $('#run-analysis').disabled = true;
  updateOutputs();
  try {
    if (!$('#scenario-form').checkValidity()) {
      throw new Error('Confira os campos: percentuais e custos devem estar dentro dos limites.');
    }
    const result = await api('/api/simulate', getInputs());
    if (sequence !== simulationSequence) return;  // ignora resposta antiga
    scenario = result;
    simulationValid = true;
    renderScenario();
  } catch (e) {
    if (sequence === simulationSequence) toast(e.message, true);
  } finally {
    if (sequence === simulationSequence) $('#run-analysis').disabled = running || !simulationValid;
  }
}

function renderAI() {
  const a = state.ai;
  $('#ai-status').textContent = a.ready
    ? `Agente de IA disponível (${a.model}).${a.fallbacks && a.fallbacks.length ? ` Se ele falhar, o app tenta: ${a.fallbacks.join(' → ')}.` : ''} Os números agregados, as respostas da entrevista e os trechos das fontes oficiais são enviados ao provedor do modelo que responder.`
    : 'O agente de IA ainda não está ligado. Preencha LLM_API_KEY (chave do Gemini) no arquivo .env e reinicie o app. Enquanto isso, use as regras fixas.';
  $('#run-mode option[value="ai"]').disabled = !a.ready;
}

async function runAnalysis() {
  if (running || !simulationValid) return;
  const progress = $('#run-progress');
  const mode = $('#run-mode').value;
  running = true;
  $('#run-analysis').disabled = true;
  progress.hidden = false;
  progress.className = 'notice';
  progress.textContent = mode === 'ai'
    ? 'O agente de IA está trabalhando. Costuma levar cerca de 1 minuto e pode chegar a alguns.'
    : 'Calculando e salvando...';
  try {
    const { job_id } = await api('/api/runs', { mode, inputs: getInputs() });
    let result;
    for (;;) {
      await new Promise(resolve => setTimeout(resolve, 900));
      result = await api('/api/jobs/' + job_id);
      if (result.status !== 'running') break;
    }
    if (result.status === 'error') throw new Error(result.message);
    currentRun = result.run;
    state.history = [{ id: currentRun.id, created_at: currentRun.created_at, mode: currentRun.analysis.mode }, ...state.history].slice(0, 30);
    renderHistory();
    renderReview();
    progress.textContent = 'Análise salva. Agora é a sua vez de decidir.';
    showPage('review');
    toast('Análise salva. Decida cada proposta abaixo.');
  } catch (e) {
    progress.className = 'notice danger';
    progress.textContent = e.message;
    toast(e.message, true);
  } finally {
    running = false;
    $('#run-analysis').disabled = !simulationValid;
  }
}


function renderHistory() {
  $('#run-history').innerHTML = state.history.length
    ? state.history.map(r => `<option value="${esc(r.id)}">${date(r.created_at)} · ${r.mode === 'ai' ? 'Agente de IA' : 'Regras fixas'} · ${r.id.slice(0, 8)}</option>`).join('')
    : '<option value="">Nenhuma análise ainda</option>';
  if (currentRun) $('#run-history').value = currentRun.id;
}

function renderAnalysisCard(run) {
  const analysis = run.analysis;
  const isAI = analysis.mode === 'ai';
  const steps = analysis.tool_log.length
    ? analysis.tool_log.map((t, i) => `${i + 1}. ${esc(t.tool)}: ${esc(t.result)}`).join('<br>')
    : 'Nenhuma ferramenta de agente executada. Os cálculos foram feitos diretamente em Python.';
  const sources = analysis.source_ids.length ? analysis.source_ids.map(esc).join(', ') : 'nenhuma';
  return `
    <div class="tile analysis">
      <div class="tile-header"><h2>${isAI ? 'O que o agente de IA concluiu' : 'Leitura pelas regras fixas'}</h2>${tag(analysis.engine, 'blue')}</div>
      ${analysis.fallback ? `<div class="notice warning"><strong>Feita por um modelo reserva</strong>
        <p>${analysis.fallback.failures.map(f => `${esc(f.engine)}: ${esc(f.reason)}`).join('<br>')}</p>
        <p>Quem respondeu: ${esc(analysis.engine)}.${analysis.engine.includes('resumidos') ? ' Esse modelo recebe uma versão resumida dos dados, então o texto tende a ser menos detalhado. Os números e a validação são os mesmos.' : ''}</p></div>` : ''}
      <p class="analysis-summary">${esc(analysis.summary)}</p>
      <details class="accordion analysis-more"><summary>Ler análise completa</summary>
        <div class="body">
          <h3>Pontos em aberto para a implementação</h3>
          <ul class="detail-list">${analysis.questions.map(q => `<li>${esc(q)}</li>`).join('')}</ul>
          <h3>Passos técnicos do agente</h3>
          <p>${steps}</p>
          <p class="field-hint">Fontes citadas: ${sources}. O registro completo das fontes está no JSON desta análise.</p>
        </div>
      </details>
    </div>`;
}

function renderActionCard(run, priority, actionsById) {
  const a = actionsById[priority.action_id];
  const review = run.reviews[a.id];
  const decision = DECISIONS[review ? review.decision : 'pendente'];
  const seniorBadge = a.senior_approval ? ` ${tag('Exige aprovação superior', 'yellow')}` : '';
  const detail = a.details
    ? `<ul class="detail-list">${a.details.map(d => `<li>${esc(d.department)}: ${brl(d.base)} × ${num(d.rate)}% = ${brl(d.gross)}</li>`).join('')}</ul>`
    : `<p>Base ${brl(a.base)} × ${num(a.rate)}% − ${brl(a.cost)}.</p>`;
  const lastDecision = review
    ? `<div class="notice"><strong>Última decisão: ${esc(review.reviewer)} · ${date(review.created)}${review.senior_approver ? ' · aprovador superior: ' + esc(review.senior_approver) : ''}</strong><p>${esc(review.reason)}</p></div>`
    : '';
  return `
    <details class="accordion proposal" data-id="${esc(a.id)}"${openCards.has(a.id) ? ' open' : ''}>
      <summary>
        <span class="proposal-title"><strong>${esc(a.title)}</strong><small>${esc(a.category)}</small></span>
        <span class="proposal-impact">${a.qualitative ? 'sem valor estimado' : brl(a.net)}</span>
        ${tag(decision.label, decision.kind)}${seniorBadge}
      </summary>
      <div class="body">
        <p>${esc(priority.reason)}</p>
        ${a.qualitative
          ? '<div class="notice"><strong>Sem valor financeiro estimado</strong><p>Medida de governança: não entra na projeção.</p></div>'
          : `<div class="money-row">${money('Impacto bruto', a.gross)}${money('Custo estimado', a.cost)}${money('Impacto líquido', a.net, a.net < 0)}</div>`}
        <p><strong>Como foi calculado:</strong> ${esc(a.formula)}</p>
        <p>${esc(a.assumption)}</p>
        ${a.qualitative ? '' : detail}
        <h3>O que conferir antes de decidir</h3>
        <ul class="detail-list">${a.conditions.map(c => `<li>${esc(c)}</li>`).join('')}</ul>
        <span class="cell-ref">${esc(a.source)}</span>
        ${lastDecision}
        <form class="review-form form-grid section" data-action="${esc(a.id)}">
          <div class="field"><label for="reviewer-${a.id}">Quem está decidindo</label>
            <input id="reviewer-${a.id}" name="reviewer" type="text" minlength="2" maxlength="100" required placeholder="Seu nome"></div>
          <div class="field"><label for="decision-${a.id}">Decisão</label>
            <select id="decision-${a.id}" name="decision">
              <option value="revisar">Pedir revisão</option>
              <option value="aprovada">Aprovar o estudo desta proposta</option>
              <option value="rejeitada">Recusar</option>
            </select></div>
          ${a.senior_approval ? `<div class="field full"><label for="senior-${a.id}">Quem aprova em nível superior (obrigatório para aprovar uma medida de tributação)</label>
            <input id="senior-${a.id}" name="senior_approver" type="text" maxlength="100" placeholder="Nome e cargo de quem aprova"></div>` : ''}
          <div class="field full"><label for="reason-${a.id}">Por que decidiu assim</label>
            <textarea id="reason-${a.id}" name="reason" rows="2" minlength="5" maxlength="2000" required placeholder="O que foi conferido e o que ainda precisa ser confirmado?"></textarea></div>
          <div class="full"><button type="submit" class="button secondary">Registrar decisão</button></div>
        </form>
      </div>
    </details>`;
}

function renderProgress(run) {
  const actions = run.scenario.actions;
  const decided = actions.filter(a => ['aprovada', 'rejeitada'].includes((run.reviews[a.id] || {}).decision)).length;
  const missing = actions.length - decided;
  return `<div class="tile">
    <div class="tile-header"><h2>${decided} de ${actions.length} propostas decididas</h2>${tag(missing ? `faltam ${missing}` : 'tudo decidido', missing ? 'yellow' : 'green')}</div>
    <div class="bar-track"><div class="bar" data-width="${100 * decided / actions.length}"></div></div>
    <p class="field-hint">${missing ? 'Clique em uma proposta abaixo para ler e decidir.' : 'Todas as propostas têm decisão. Você pode baixar o resultado no alto da página.'}</p>
  </div>`;
}

function renderAudit(run) {
  const titles = Object.fromEntries(run.scenario.actions.map(a => [a.id, a.title]));
  const rows = run.audit.length
    ? run.audit.map(e => `<div class="audit-row">${date(e.created)} ${tag(DECISIONS[e.decision].label, DECISIONS[e.decision].kind)}
        <strong>${esc(e.reviewer)}</strong>${e.senior_approver ? ` · aprovador superior: <strong>${esc(e.senior_approver)}</strong>` : ''} <span class="cell-ref">${esc(titles[e.action_id] || e.action_id)}</span><p>${esc(e.reason)}</p></div>`).join('')
    : '<p class="field-hint">Nenhuma decisão registrada. As propostas permanecem pendentes.</p>';
  return `<div class="tile"><div class="tile-header"><h2>Histórico de decisões desta versão</h2>${tag(run.audit.length === 1 ? '1 registro' : run.audit.length + ' registros')}</div>${rows}</div>`;
}

function renderReview() {
  const run = currentRun;
  for (const kind of ['json', 'csv']) {
    const el = $('#export-' + kind);
    el.hidden = !run;
    if (run) el.href = `/api/runs/${run.id}/export.${kind}`;
  }
  if (!run) {
    $('#pending-count').textContent = '0';
    $('#review-content').innerHTML = `<div class="tile empty"><h2>Nenhuma proposta para revisar ainda.</h2>
      <p>Ajuste as premissas na simulação e gere uma análise. Cada versão terá seu próprio histórico de decisões.</p>
      <button class="button" data-goto="simulation">Abrir simulação</button></div>`;
    return;
  }

  const pending = run.scenario.actions.filter(a => !run.reviews[a.id] || run.reviews[a.id].decision === 'revisar').length;
  $('#pending-count').textContent = String(pending);

  const approved = run.approved_projection;
  const actionsById = Object.fromEntries(run.scenario.actions.map(a => [a.id, a]));

  $('#review-content').innerHTML = `
    <div class="meta">${tag(date(run.created_at))} ${tag('Versão ' + run.id.slice(0, 8))} ${tag('Arquivo ' + run.dataset.fingerprint.slice(0, 12))}</div>
    ${renderProgress(run)}
    <div class="notice compact"><strong>Os percentuais são hipóteses do grupo.</strong>
      <span>Não foram confirmados pelo cliente. Aprovar registra o aceite do estudo; não autoriza a medida.</span></div>
    ${run.roles_complete ? '' : `<div class="notice compact warning"><strong>Papéis da equipe pendentes nesta versão.</strong>
      <span>Preencha antes de preparar a entrega; esta análise guarda o cadastro da data em que foi gerada.</span>
      <button type="button" class="button ghost" data-goto="project">Preencher equipe</button></div>`}
    ${renderAnalysisCard(run)}
    <h2 class="proposals-title">Propostas para decidir</h2>
    ${run.analysis.priorities.map(p => renderActionCard(run, p, actionsById)).join('')}
    <div class="tile approved">
      <div class="tile-header"><div><h3>Resultado se só as propostas aprovadas valerem</h3>
        <p>${approved.approved_count} de ${approved.total_count} aprovadas para estudo · já desconta os custos · valor simulado</p></div>
        <div class="tile-value ${approved.projected_balance < 0 ? 'negative-text' : ''}">${brl(approved.projected_balance)}</div></div>
      ${approved.horizon ? `<p class="field-hint">Em ${approved.horizon.length} anos, o saldo acumulado com as propostas aprovadas seria ${brl(approved.horizon[approved.horizon.length - 1].cumulative_balance)} (valores constantes).</p>` : ''}
      ${approved.objective ? `<p><strong>${approved.objective.meets ? 'As propostas aprovadas cobrem a meta nos dez anos, nas contas da simulação.' : `As propostas aprovadas ainda deixam déficit no ano ${approved.objective.first_deficit_year}.`}</strong> O menor saldo anual seria ${brl(approved.objective.minimum_balance)}.</p>` : ''}
    </div>
    ${renderAudit(run)}`;
  $$('#review-content [data-width]').forEach(el => el.style.setProperty('--w', el.dataset.width + '%'));
  annotate($('#review-content'));
}


const GLOSSARY = [
  { term: 'LRF', exact: true, text: 'Lei de Responsabilidade Fiscal. Lei federal com as regras para as contas do município, como os limites de gasto com pessoal.' },
  { term: 'RCL ajustada', text: 'RCL depois de tirar as transferências que a LRF manda excluir. É a base dos limites de gasto com pessoal.' },
  { term: 'limite prudencial', text: '95% do limite máximo de gasto com pessoal. Acima dele, a LRF impõe restrições ao município.' },
  { term: 'limite de alerta', text: '90% do limite máximo de gasto com pessoal. O Tribunal de Contas avisa o município quando ele é passado.' },
  { term: 'RGF', exact: true, text: 'Relatório de Gestão Fiscal. Demonstrativo oficial que o município publica com a despesa com pessoal e os limites da LRF.' },
  { term: 'SICONFI', exact: true, text: 'Sistema do Tesouro Nacional onde estados e municípios publicam seus relatórios fiscais.' },
  { term: 'mediana', text: 'Valor do meio quando os números são postos em ordem. Metade dos municípios fica acima e metade abaixo.' },
  { term: 'RCL', exact: true, text: 'Receita Corrente Líquida. Base oficial usada para calcular os limites de gasto com pessoal. A planilha não traz o valor oficial.' },
  { term: 'DAS', exact: true, text: 'Cargos de confiança (comissionados). São nomeados sem concurso e podem ser exonerados livremente.' },
  { term: 'FIFO', exact: true, text: 'Sigla em inglês para "o primeiro que entra é o primeiro que sai". Na prefeitura: quem pede primeiro, recebe primeiro.' },
  { term: 'Refis', text: 'Programa de parcelamento com desconto para quem tem dívida. Aqui, aplicado ao IPTU; a planilha não separa juros e multa, então o desconto vale para o valor total devido.' },
  { term: 'IPTU', exact: true, text: 'Imposto sobre imóveis urbanos, cobrado pela prefeitura.' },
  { term: 'dívida ativa', text: 'Dinheiro que a prefeitura tem a receber de quem não pagou no prazo. Fica inscrito e pode ser cobrado na Justiça.' },
  { term: 'dotação', text: 'Valor reservado no orçamento para uma secretaria gastar no ano.' },
  { term: 'liquidada', text: 'Despesa já reconhecida como devida, depois que o serviço ou a compra foi entregue. Não significa que já foi paga: o pagamento é a etapa seguinte.' },
  { term: 'prescrição', text: 'Perda do direito de cobrar uma dívida quando passa o prazo legal (5 anos na planilha).' },
  { term: 'custeio', text: 'Gastos de funcionamento da prefeitura: contratos, serviços e manutenção, sem folha de pagamento nem obras.' },
  { term: 'restos a pagar', text: 'Despesas assumidas que não foram pagas até o fim do ano.' },
];
const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'INPUT', 'TEXTAREA', 'SELECT', 'OPTION', 'BUTTON', 'ABBR', 'CODE', 'LABEL', 'OUTPUT', 'SUMMARY', 'H1', 'H2', 'H3']);
const termPattern = g => new RegExp(`(?<![\\p{L}])${g.term}(?![\\p{L}])`, g.exact ? 'u' : 'iu');

function renderGlossary() {
  $('#glossary').innerHTML = GLOSSARY.map(g => `<dt>${esc(g.term)}</dt><dd>${esc(g.text)}</dd>`).join('');
}

function annotate(root) {
  if (!root) return;
  for (const g of GLOSSARY) {
    if (root.querySelector(`abbr.term[data-term="${g.term}"]`)) continue;
    const pattern = termPattern(g);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        for (let el = node.parentElement; el && el !== root.parentElement; el = el.parentElement) {
          if (SKIP_TAGS.has(el.tagName) || el.classList.contains('cell-ref') || el.id === 'glossary') return NodeFilter.FILTER_REJECT;
        }
        return pattern.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      },
    });
    const node = walker.nextNode();
    if (!node) continue;
    const match = pattern.exec(node.nodeValue);
    const abbr = document.createElement('abbr');
    abbr.className = 'term';
    abbr.dataset.term = g.term;
    abbr.dataset.tip = g.text;
    abbr.tabIndex = 0;
    abbr.textContent = match[0];
    const after = node.splitText(match.index);
    after.nodeValue = after.nodeValue.slice(match[0].length);
    node.parentNode.insertBefore(abbr, after);
  }
}

function annotateAll() {
  $$('.page').forEach(annotate);
}

function renderModeTag() {
  const tagEl = $('#mode-tag');
  const ready = state.ai.ready;
  tagEl.textContent = ready ? 'Agente de IA ligado' : 'Modo demonstração';
  tagEl.className = 'tag ' + (ready ? 'blue' : '');
}


function renderProject() {
  for (const key of ['consultant', 'scrum_master', 'team']) $('#' + key).value = state.project[key];
  const missing = [['consultant', 'consultor'], ['scrum_master', 'Scrum Master'], ['team', 'equipe técnica']]
    .filter(([key]) => !state.project[key].trim()).map(([, label]) => label);
  $('#project-status').textContent = missing.length
    ? 'Falta registrar: ' + missing.join(', ') + '. Use os nomes e as responsabilidades reais.'
    : 'Papéis preenchidos. Confira se os 6 a 7 integrantes e suas responsabilidades estão identificados.';
  $('#interview-questions').innerHTML = state.questions.map((q, i) => `
    <div class="field">
      <label for="answer-${i}">${i + 1}. ${esc(q)}</label>
      <textarea id="answer-${i}" rows="2" maxlength="4000" placeholder="Resposta do cliente na entrevista">${esc(state.project.answers[i] || '')}</textarea>
    </div>`).join('');
}


document.addEventListener('click', event => {
  const nav = event.target.closest('[data-page],[data-goto]');
  if (nav) showPage(nav.dataset.page || nav.dataset.goto);
});
window.addEventListener('hashchange', () => showPage(location.hash.slice(1)));

$('#scenario-form').addEventListener('submit', event => event.preventDefault());
$('#scenario-form').addEventListener('input', updateScenario);
$('#reset-inputs').addEventListener('click', () => {
  for (const [key, value] of Object.entries(state.defaults)) $('#' + key).value = value;
  updateScenario();
});
$('#scenario-results').addEventListener('click', event => {
  if (!event.target.closest('[data-equilibrium]')) return;
  if (!simulationValid || scenario.equilibrium.status !== 'candidate') return;
  for (const [key, value] of Object.entries(scenario.equilibrium.inputs)) $('#' + key).value = value;
  updateScenario();
  toast('Alternativa carregada para estudo. Confira as premissas e gere uma nova análise.');
});
$('#run-analysis').addEventListener('click', runAnalysis);

$('#refresh-sources').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = 'Consultando fontes...';
  try {
    state.sources = await api('/api/sources/refresh', {});
    renderSources();
    const count = state.sources.filter(s => s.status === 'consultada').length;
    toast(`${count} de ${state.sources.length} fontes consultadas. Confira o status de cada referência.`, count === 0);
  } catch (e) {
    toast(e.message, true);
  } finally {
    button.disabled = false;
    button.textContent = 'Consultar fontes oficiais';
  }
});

async function refreshExternal(forcar) {
  const buttons = [$('#refresh-external'), $('#retry-external')];
  const label = buttons.map(b => b.textContent);
  buttons.forEach(b => { b.disabled = true; });
  (forcar ? buttons[1] : buttons[0]).textContent = 'Coletando... (cerca de 20 s na primeira vez)';
  try {
    state.external = await api('/api/external/refresh', forcar ? { forcar: true } : {});
    renderExternal();
    const partes = [state.external.rcl, state.external.benchmark];
    const falhas = partes.filter(r => r.status !== 'consultada' || r.desatualizado).length;
    toast(falhas === 0 ? 'Dados públicos coletados.'
      : falhas === partes.length ? 'Não foi possível coletar agora. O motivo está em cada cartão.'
      : 'Coleta concluída com pendências. Confira o status de cada cartão.', falhas > 0);
  } catch (e) {
    toast(e.message, true);
  } finally {
    buttons.forEach((b, i) => { b.disabled = false; b.textContent = label[i]; });
  }
}

$('#refresh-external').addEventListener('click', () => refreshExternal(false));
$('#retry-external').addEventListener('click', () => refreshExternal(true));

$('#run-history').addEventListener('change', async event => {
  const select = event.currentTarget;
  select.disabled = true;
  try {
    currentRun = await api('/api/runs/' + select.value);
    renderReview();
  } catch (e) {
    toast(e.message, true);
  } finally {
    select.disabled = false;
  }
});

$('#review-content').addEventListener('toggle', event => {
  const card = event.target.closest && event.target.closest('details.proposal');
  if (!card) return;
  if (card.open) openCards.add(card.dataset.id); else openCards.delete(card.dataset.id);
}, true);

$('#review-content').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target.closest('.review-form');
  if (!form || !form.reportValidity()) return;
  const button = form.querySelector('button');
  button.disabled = true;
  try {
    const data = Object.fromEntries(new FormData(form));
    currentRun = await api('/api/reviews', { ...data, run_id: currentRun.id, action_id: form.dataset.action });
    const scrollY = window.scrollY;
    renderReview();
    window.scrollTo(0, scrollY);
    toast('Decisão registrada no histórico desta versão.');
  } catch (e) {
    button.disabled = false;
    toast(e.message, true);
  }
});

$('#project-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.target.querySelector('button[type="submit"]');
  button.disabled = true;
  const data = {
    consultant: $('#consultant').value,
    scrum_master: $('#scrum_master').value,
    team: $('#team').value,
    answers: state.questions.map((_, i) => $('#answer-' + i).value),
  };
  try {
    state.project = await api('/api/project', data);
    renderProject();
    $('#project-feedback').textContent = 'Salvo localmente. Novas análises usarão estas respostas.';
    toast('Equipe e entrevista salvas.');
  } catch (e) {
    toast(e.message, true);
  } finally {
    button.disabled = false;
  }
});


async function init() {
  try {
    state = await api('/api/state');
    scenario = state.scenario;
    currentRun = state.latest;
    for (const [key, value] of Object.entries(state.defaults)) $('#' + key).value = value;
    renderLeverBases();
    updateOutputs();
    renderMetrics();
    renderSituation();
    renderSpend();
    renderWarnings();
    renderTables();
    renderSources();
    renderExternal();
    renderScenario();
    renderAI();
    renderHistory();
    renderReview();
    renderProject();
    renderGlossary();
    renderModeTag();
    annotateAll();
    showPage(location.hash.slice(1) || 'data');
  } catch (e) {
    $('#load-error').hidden = false;
    $('#load-error').textContent = 'Não foi possível carregar o projeto: ' + e.message;
    $('#run-analysis').disabled = true;
  }
}

init();
