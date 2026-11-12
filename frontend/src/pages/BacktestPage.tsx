import { useEffect, useMemo, useState } from 'react';
import {
  Activity, AlertTriangle, ArrowDownToLine, BarChart3, CalendarDays, ChevronDown, ChevronUp,
  CircleDollarSign, Clock3, FlaskConical, Gauge, Layers3, Play, RotateCcw, ShieldAlert, TrendingDown, WalletCards,
} from 'lucide-react';
import {
  Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { api, downloadBacktestCsv } from '../lib/api';
import {
  dateTime, displaySymbol, integer, lower, money, number, orderTypeLabel, percent, purposeLabel, shortDate, sideLabel,
  signedMoney, signedPercent, statusLabel,
} from '../lib/format';
import FeeConfigPanel from '../components/FeeConfigPanel';
import { Badge, EmptyState, ErrorBanner, Field, Loading, Metric, PageHeader, Panel, SyntheticNotice, TableEmpty } from '../components/Ui';
import { defaultFeeConfig, feeBreakdownTitle, strategyToFeeConfig, stripFeeFields } from '../lib/feeConfig';
import type { BacktestSummary, Dataset, FeeConfig, Report, Round, StrategyConfig } from '../types';

const fallbackConfig: StrategyConfig = {
  enable_t: true,
  commission_rate: 0.00005,
  min_commission: 5,
  stamp_tax_rate: 0.0005,
  handling_fee_rate: 0.0000341,
  regulatory_fee_rate: 0.00002,
  transfer_fee_rate: 0.00001,
  slippage_bps: 1,
  volume_participation: 0.2,
};

const roundValue = (round: Round, keys: string[]): unknown => {
  for (const key of keys) if (round[key] !== undefined && round[key] !== null) return round[key];
  return undefined;
};
const roundNumber = (round: Round, keys: string[]): number | undefined => {
  const value = roundValue(round, keys);
  return typeof value === 'number' ? value : value === undefined ? undefined : Number(value);
};

export default function BacktestPage() {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [history, setHistory] = useState<BacktestSummary[]>([]);
  const [config, setConfig] = useState<StrategyConfig>(fallbackConfig);
  const [fees, setFees] = useState<FeeConfig>(defaultFeeConfig);
  const [datasetId, setDatasetId] = useState('');
  const [intradayId, setIntradayId] = useState('');
  const [indexId, setIndexId] = useState('');
  const [name, setName] = useState('分层双账本策略回测');
  const [initialCash, setInitialCash] = useState(4000000);
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [advanced, setAdvanced] = useState(false);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState('');
  const [tableTab, setTableTab] = useState<'rounds' | 'trades' | 'orders'>('rounds');
  const [exporting, setExporting] = useState<'trades' | 'orders' | null>(null);

  const loadInitial = async () => {
    setLoading(true);
    setError('');
    try {
      const [datasetResult, defaults, reports] = await Promise.all([api.datasets(), api.strategyDefaults(), api.backtests()]);
      setDatasets(datasetResult);
      const defaultConfig = defaults.config || defaults;
      setConfig((current) => ({ ...current, ...defaultConfig }));
      setFees(defaults.fees || strategyToFeeConfig(defaultConfig));
      if (defaults.initial_cash) setInitialCash(defaults.initial_cash);
      const daily = datasetResult.find((item) => item.timeframe === 'daily' && item.source === 'baostock')
        || datasetResult.find((item) => item.timeframe === 'daily');
      if (daily) {
        setDatasetId((current) => current || daily.id);
        setStartDate((current) => current || daily.start?.slice(0, 10));
        setEndDate((current) => current || daily.end?.slice(0, 10));
        const minute = datasetResult.find((item) => item.timeframe === '1m' && item.symbol === daily.symbol);
        if (minute) setIntradayId((current) => current || minute.id);
        const index = datasetResult.find((item) => item.timeframe === 'daily' && ['sh.000688', 'sz.399006'].includes(item.symbol));
        if (index) setIndexId((current) => current || index.id);
      }
      setHistory(reports);
      if (reports.length > 0) setReport(await api.backtest(reports[0].id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '初始化失败，请检查后端服务。');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void loadInitial(); }, []);

  const selectedDataset = datasets.find((item) => item.id === datasetId);
  const intradayOptions = datasets.filter((item) => item.id !== datasetId && item.timeframe === '1m');
  const indexOptions = datasets.filter((item) => item.id !== datasetId && item.timeframe === 'daily' && ['sh.000688', 'sz.399006'].includes(item.symbol));

  const run = async () => {
    if (!datasetId) { setError('请选择主数据集后再运行回测。'); return; }
    if (startDate && endDate && startDate > endDate) { setError('开始日期不能晚于结束日期，请调整回测区间。'); return; }
    if (initialCash <= 0) { setError('初始资金必须大于 0。'); return; }
    setRunning(true);
    setError('');
    try {
      const result = await api.runBacktest({
        name: name.trim() || '未命名回测', dataset_id: datasetId,
        intraday_dataset_id: intradayId || undefined, index_dataset_id: indexId || undefined,
        initial_cash: initialCash, start_date: startDate || undefined, end_date: endDate || undefined,
        config: stripFeeFields(config), fees,
      });
      setReport(result);
      setTableTab('rounds');
      const reports = await api.backtests().catch(() => history);
      setHistory(reports);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '回测运行失败，请稍后重试。');
    } finally {
      setRunning(false);
    }
  };

  const loadReport = async (id: string) => {
    if (!id) return;
    setLoading(true);
    setError('');
    try { setReport(await api.backtest(id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '报告读取失败。'); }
    finally { setLoading(false); }
  };

  const exportCsv = async (kind: 'trades' | 'orders') => {
    if (!report) return;
    setExporting(kind);
    setError('');
    try { await downloadBacktestCsv(report.id, kind); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '导出失败。'); }
    finally { setExporting(null); }
  };

  const updateConfig = <K extends keyof StrategyConfig>(key: K, value: StrategyConfig[K]) => setConfig((current) => ({ ...current, [key]: value }));
  const metrics = report?.metrics;
  const closedRounds = useMemo(() => report?.rounds.filter((round) => !round.status || ['closed', 'completed', '已结束'].includes(lower(round.status))).length || 0, [report]);

  return (
    <div className="page backtest-page">
      <PageHeader
        eyebrow="RESEARCH / BACKTEST"
        title="回测工作台"
        description="用可复核的数据与费用假设，检验分层双账本策略的完整生命周期。"
        actions={history.length > 0 ? (
          <select className="history-select" value={report?.id || ''} onChange={(event) => void loadReport(event.target.value)} aria-label="读取历史报告">
            <option value="">读取历史报告</option>
            {history.map((item) => <option key={item.id} value={item.id}>{item.name} · {shortDate(item.created_at)}</option>)}
          </select>
        ) : undefined}
      />

      {error && <ErrorBanner message={error} onRetry={!report && !loading ? () => void loadInitial() : undefined} onClose={() => setError('')} />}

      <Panel className="config-panel">
        <form onSubmit={(event) => { event.preventDefault(); void run(); }}>
          <div className="config-topline">
            <div className="config-title"><span className="step-index">01</span><div><h2>实验配置</h2><p>定义数据边界与交易摩擦，再启动本地回测。</p></div></div>
            <label className="switch-row"><span><strong>启用做 T</strong><small>可选，不占用 16 笔序列</small></span><input type="checkbox" checked={config.enable_t} onChange={(event) => updateConfig('enable_t', event.target.checked)} /><span className="switch" /></label>
          </div>
          <div className="form-grid backtest-form-grid">
            <Field label="实验名称" className="span-2"><input value={name} onChange={(event) => setName(event.target.value)} placeholder="输入回测名称" /></Field>
            <Field label="主数据集" hint="baostock 日线" className="span-2">
              <select value={datasetId} onChange={(event) => {
                const next = datasets.find((item) => item.id === event.target.value);
                setDatasetId(event.target.value);
                if (next) {
                  setStartDate(next.start?.slice(0, 10));
                  setEndDate(next.end?.slice(0, 10));
                  const minute = datasets.find((item) => item.timeframe === '1m' && item.symbol === next.symbol);
                  if (minute) setIntradayId(minute.id);
                }
              }} required>
                <option value="">请选择数据集</option>
                {datasets.filter((item) => item.timeframe === 'daily').map((item) => <option key={item.id} value={item.id}>{item.name} · {displaySymbol(item.symbol)} · {item.source}</option>)}
              </select>
            </Field>
            <Field label="开始日期"><div className="input-icon"><CalendarDays size={15} /><input type="date" value={startDate} min={selectedDataset?.start?.slice(0, 10)} max={endDate || selectedDataset?.end?.slice(0, 10)} onChange={(event) => setStartDate(event.target.value)} /></div></Field>
            <Field label="结束日期"><div className="input-icon"><CalendarDays size={15} /><input type="date" value={endDate} min={startDate || selectedDataset?.start?.slice(0, 10)} max={selectedDataset?.end?.slice(0, 10)} onChange={(event) => setEndDate(event.target.value)} /></div></Field>
            <Field label="初始资金"><div className="input-with-unit"><input type="number" min="1" step="10000" value={initialCash} onChange={(event) => setInitialCash(Number(event.target.value))} required /><span>CNY</span></div></Field>
            <Field label="分钟数据集" hint="腾讯 1 分钟"><select value={intradayId} onChange={(event) => setIntradayId(event.target.value)}><option value="">不使用（日线降级）</option>{intradayOptions.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.timeframe} · {item.source}</option>)}</select></Field>
            <Field label="指数数据集" hint="baostock 大盘门"><select value={indexId} onChange={(event) => setIndexId(event.target.value)}><option value="">不使用</option>{indexOptions.map((item) => <option key={item.id} value={item.id}>{item.name} · {displaySymbol(item.symbol)}</option>)}</select></Field>
          </div>
          <button type="button" className="advanced-toggle" onClick={() => setAdvanced(!advanced)} aria-expanded={advanced}>
            <span><Gauge size={15} />交易摩擦与撮合参数</span>{advanced ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </button>
          {advanced && (
            <>
              <FeeConfigPanel value={fees} onChange={setFees} />
              <div className="form-grid advanced-grid">
                <Field label="滑点" className="span-3"><div className="input-with-unit"><input type="number" min="0" step="1" value={config.slippage_bps} onChange={(event) => updateConfig('slippage_bps', Number(event.target.value))} /><span>bps</span></div></Field>
                <Field label="成交量参与上限" className="span-3"><div className="input-with-unit"><input type="number" min="0" max="100" step="1" value={config.volume_participation * 100} onChange={(event) => updateConfig('volume_participation', Number(event.target.value) / 100)} /><span>%</span></div></Field>
              </div>
            </>
          )}
          <div className="config-footer">
            <div className="dataset-hint">
              {selectedDataset ? <><span>{selectedDataset.rows.toLocaleString('zh-CN')} 行</span><i /> <span>{selectedDataset.timeframe} 粒度</span><i /><span>{shortDate(selectedDataset.start)} — {shortDate(selectedDataset.end)}</span>{selectedDataset.is_synthetic && <SyntheticNotice compact />}</> : <span>请选择主数据集以查看数据边界</span>}
            </div>
            <button className="button primary run-button" type="submit" disabled={running || !datasetId}>
              {running ? <><span className="button-spinner" />正在运行回测</> : <><Play size={16} fill="currentColor" />运行回测</>}
            </button>
          </div>
        </form>
      </Panel>

      {loading && !report ? <Panel><Loading label="正在连接研究环境" /></Panel> : !report ? (
        <Panel className="report-empty-panel">
          <EmptyState
            icon={<BarChart3 size={28} />}
            title="等待第一组实验结果"
            description="选择数据集并运行回测后，这里将呈现权益曲线、风险诊断与逐笔成交。未连接后端时不会生成替代结果。"
            action={<div className="empty-steps"><span><b>1</b>配置数据</span><i /><span><b>2</b>运行实验</span><i /><span><b>3</b>复核风险</span></div>}
          />
        </Panel>
      ) : metrics && (
        <div className="report-content">
          <div className="report-titlebar">
            <div><div className="report-kicker"><Badge tone="indigo">{statusLabel(report.status)}</Badge><span>REPORT / {report.id.slice(0, 8).toUpperCase()}</span></div><h2>{report.name}</h2><p>生成于 {dateTime(report.created_at)} · 初始资金 {money(metrics.initial_cash, 0)}</p></div>
            <div className="report-export"><button className="button secondary" onClick={() => void exportCsv('trades')} disabled={exporting !== null}><ArrowDownToLine size={15} />{exporting === 'trades' ? '导出中' : '导出成交'}</button><button className="button secondary" onClick={() => void exportCsv('orders')} disabled={exporting !== null}><ArrowDownToLine size={15} />{exporting === 'orders' ? '导出中' : '导出委托'}</button></div>
          </div>

          <div className="metrics-grid six">
            <Metric label="总收益率" value={signedPercent(metrics.total_return)} tone={metrics.total_return >= 0 ? 'positive' : 'negative'} sub={`期末权益 ${money(metrics.final_equity)}`} />
            <Metric label="年化收益率" value={signedPercent(metrics.annualized_return)} tone={metrics.annualized_return >= 0 ? 'positive' : 'negative'} sub={`买入持有 ${signedPercent(metrics.buy_hold_return)}`} />
            <Metric label="最大回撤" value={percent(Math.abs(metrics.max_drawdown))} tone="negative" sub={`Calmar ${number(metrics.calmar_ratio)}`} />
            <Metric label="夏普比率" value={number(metrics.sharpe_ratio)} sub={`波动率 ${percent(metrics.volatility)}`} />
            <Metric label="轮次胜率" value={percent(metrics.win_rate)} sub={`${metrics.closed_rounds} / ${metrics.total_rounds} 轮已结束`} />
            <Metric label="净利润" value={signedMoney(metrics.net_profit)} tone={metrics.net_profit >= 0 ? 'positive' : 'negative'} sub={`${integer(metrics.total_trades)} 笔成交 · PF ${number(metrics.profit_factor)}`} />
          </div>

          <div className="report-main-grid">
            <Panel className="chart-panel" title="权益与风险轨迹" description="账户权益、实际投入与回撤随时间的变化" action={<div className="chart-legend"><span className="legend-equity">账户权益</span><span className="legend-invested">实际投入</span><span className="legend-drawdown">回撤</span></div>}>
              <div className="equity-chart">
                {report.equity_curve.length > 0 ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <ComposedChart data={report.equity_curve} margin={{ top: 12, right: 4, left: 4, bottom: 0 }}>
                      <defs><linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#4f46e5" stopOpacity={0.14} /><stop offset="100%" stopColor="#4f46e5" stopOpacity={0} /></linearGradient></defs>
                      <CartesianGrid stroke="#ececf1" strokeDasharray="3 4" vertical={false} />
                      <XAxis dataKey="date" tickFormatter={(value: string) => value.slice(5, 10)} tick={{ fill: '#85858f', fontSize: 11 }} axisLine={false} tickLine={false} minTickGap={48} dy={8} />
                      <YAxis yAxisId="money" tickFormatter={(value: number) => `${(value / 10000).toFixed(0)}万`} tick={{ fill: '#85858f', fontSize: 11 }} axisLine={false} tickLine={false} width={52} domain={['auto', 'auto']} />
                      <YAxis yAxisId="drawdown" orientation="right" tickFormatter={(value: number) => `${(value * 100).toFixed(0)}%`} tick={{ fill: '#9a9aa3', fontSize: 11 }} axisLine={false} tickLine={false} width={42} domain={['auto', 0]} />
                      <Tooltip content={({ active, payload, label }) => active && payload?.length ? <div className="chart-tooltip"><strong>{label}</strong><div><span>账户权益</span><b>{money(payload[0].payload.equity)}</b></div><div><span>实际投入</span><b>{money(payload[0].payload.invested)}</b></div><div><span>可用现金</span><b>{money(payload[0].payload.cash)}</b></div><div><span>持仓市值</span><b>{money(payload[0].payload.market_value)}</b></div><div><span>回撤</span><b className="negative">{percent(payload[0].payload.drawdown)}</b></div><div><span>持仓数量</span><b>{integer(payload[0].payload.position_qty)}</b></div></div> : null} />
                      <Area yAxisId="money" type="monotone" dataKey="equity" stroke="#4f46e5" strokeWidth={2.2} fill="url(#equityFill)" activeDot={{ r: 4, fill: '#4f46e5', stroke: '#fff', strokeWidth: 2 }} />
                      <Line yAxisId="money" type="monotone" dataKey="invested" stroke="#8b8b98" strokeWidth={1.5} dot={false} strokeDasharray="5 4" />
                      <Line yAxisId="drawdown" type="stepAfter" dataKey="drawdown" stroke="#159a77" strokeWidth={1.4} dot={false} />
                    </ComposedChart>
                  </ResponsiveContainer>
                ) : <EmptyState compact title="暂无权益曲线" description="报告未返回可绘制的时序数据。" />}
              </div>
              <div className="chart-footnote"><span><i className="line-swatch indigo" />左轴：资金（万元）</span><span><i className="line-swatch green" />右轴：回撤（%）</span><span>期末 {report.equity_curve[report.equity_curve.length - 1]?.date || '—'}</span></div>
            </Panel>

            <Panel className="risk-panel" title="本轮风险诊断" description="从资金占用到尾部风险的横向检查" action={<ShieldAlert size={18} />}>
              <div className="risk-list">
                <div><span className="risk-icon"><WalletCards size={17} /></span><div><small>最大资金投入</small><strong>{money(metrics.max_invested)}</strong></div><span className="risk-note">峰值占用</span></div>
                <div><span className="risk-icon"><TrendingDown size={17} /></span><div><small>最大浮亏</small><strong className="negative">{money(metrics.max_float_loss)}</strong></div><span className="risk-note">未实现口径</span></div>
                <div><span className="risk-icon"><RotateCcw size={17} /></span><div><small>做 T 净贡献</small><strong className={metrics.t_net_profit >= 0 ? 'positive' : 'negative'}>{signedMoney(metrics.t_net_profit)}</strong></div><span className="risk-note">{integer(metrics.t_trade_count)} 笔</span></div>
                <div><span className="risk-icon"><CircleDollarSign size={17} /></span><div><small>累计交易费用</small><strong>{money(metrics.total_fees)}</strong></div><span className="risk-note">五项分项相加，滑点另计</span></div>
                <div><span className="risk-icon"><Activity size={17} /></span><div><small>平均资金敞口</small><strong>{percent(metrics.exposure)}</strong></div><span className="risk-note">权益占比</span></div>
              </div>
              <div className="risk-summary"><Gauge size={16} /><span>风险表现需结合数据粒度、成交假设与样本区间共同判断。</span></div>
            </Panel>
          </div>

          <Panel className="data-lineage-panel">
            <div className="data-lineage">
              <div className="lineage-title"><Layers3 size={17} /><div><strong>数据血缘与口径</strong><span>本报告基于后端实际返回的数据元信息</span></div></div>
              <div className="lineage-tags"><Badge>{String(report.data_info.daily_source || selectedDataset?.source || 'baostock')} 日线</Badge>{intradayId && <Badge tone="indigo">{String(report.data_info.intraday_source || 'tencent')} 1分钟</Badge>}{indexId && <Badge tone="green">指数大盘门</Badge>}{selectedDataset?.is_synthetic && <SyntheticNotice compact />}</div>
            </div>
            {Object.keys(report.data_info || {}).length > 0 && <div className="data-info-grid">{Object.entries(report.data_info).map(([key, value]) => <div key={key}><span>{key}</span><strong title={typeof value === 'object' ? JSON.stringify(value) : String(value)}>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</strong></div>)}</div>}
            {report.warnings.length > 0 ? <div className="warning-box"><AlertTriangle size={18} /><div><strong>数据与近似口径提醒</strong><ul>{report.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div></div> : <div className="no-warning"><FlaskConical size={15} />后端未返回额外警告；仍请留意日线无法还原精确 tick 成交路径。</div>}
          </Panel>

          <Panel className="records-panel">
            <div className="records-header">
              <div className="tabs" role="tablist">
                <button className={tableTab === 'rounds' ? 'active' : ''} onClick={() => setTableTab('rounds')} role="tab">已结束轮次<span>{closedRounds}</span></button>
                <button className={tableTab === 'trades' ? 'active' : ''} onClick={() => setTableTab('trades')} role="tab">成交明细<span>{report.trades.length}</span></button>
                <button className={tableTab === 'orders' ? 'active' : ''} onClick={() => setTableTab('orders')} role="tab">委托记录<span>{report.orders.length}</span></button>
              </div>
              <span className="records-caption"><Clock3 size={14} />按时间顺序记录</span>
            </div>
            <div className="table-scroll">
              {tableTab === 'rounds' && <table><thead><tr><th>轮次</th><th>开始时间</th><th>结束时间</th><th className="num">最大投入</th><th className="num">净盈亏</th><th className="num">收益率</th><th className="num">成交数</th><th>状态</th></tr></thead><tbody>
                {report.rounds.length === 0 ? <TableEmpty colSpan={8} message="本次回测没有轮次记录" /> : report.rounds.map((round, index) => {
                  const pnl = roundNumber(round, ['net_profit', 'pnl', 'realized_pnl']);
                  const returns = roundNumber(round, ['return_rate', 'total_return', 'return']);
                  return <tr key={String(round.id ?? index)}><td><strong className="round-id">#{String(roundValue(round, ['round', 'index']) ?? index + 1).padStart(2, '0')}</strong></td><td>{shortDate(String(roundValue(round, ['start_date', 'start', 'opened_at']) || ''))}</td><td>{shortDate(String(roundValue(round, ['end_date', 'end', 'closed_at']) || ''))}</td><td className="num">{money(roundNumber(round, ['max_invested', 'invested']))}</td><td className={`num ${pnl !== undefined ? pnl >= 0 ? 'positive' : 'negative' : ''}`}>{signedMoney(pnl)}</td><td className={`num ${returns !== undefined ? returns >= 0 ? 'positive' : 'negative' : ''}`}>{signedPercent(returns)}</td><td className="num">{integer(roundNumber(round, ['trade_count', 'trades_count', 'total_trades']))}</td><td><Badge tone={lower(round.status) === 'closed' ? 'green' : 'neutral'}>{statusLabel(round.status || 'closed')}</Badge></td></tr>;
                })}
              </tbody></table>}
              {tableTab === 'trades' && <table><thead><tr><th>成交时间</th><th>标的</th><th>方向</th><th>用途</th><th className="num">价格</th><th className="num">数量</th><th className="num">成交额</th><th className="num">费用</th><th className="num">已实现盈亏</th></tr></thead><tbody>
                {report.trades.length === 0 ? <TableEmpty colSpan={9} message="本次回测没有成交记录" /> : report.trades.map((trade, index) => <tr key={String(trade.id ?? `${trade.timestamp}-${index}`)}><td>{dateTime(trade.timestamp)}</td><td><strong title={trade.symbol}>{displaySymbol(trade.symbol)}</strong></td><td><span className={`side-label ${lower(trade.side) === 'buy' ? 'buy' : 'sell'}`}>{sideLabel(trade.side)}</span></td><td>{purposeLabel(trade.purpose)}</td><td className="num">{money(trade.price)}</td><td className="num">{integer(trade.quantity)}</td><td className="num">{money(trade.amount ?? trade.notional)}</td><td className="num" title={feeBreakdownTitle(trade)}>{money(trade.total_fee ?? trade.fee)}</td><td className={`num ${(trade.realized_pnl || 0) >= 0 ? 'positive' : 'negative'}`}>{trade.realized_pnl === undefined ? '—' : signedMoney(trade.realized_pnl)}</td></tr>)}
              </tbody></table>}
              {tableTab === 'orders' && <table><thead><tr><th>委托时间</th><th>标的</th><th>方向</th><th>类型</th><th className="num">限价</th><th className="num">委托数量</th><th className="num">已成交</th><th>状态</th></tr></thead><tbody>
                {report.orders.length === 0 ? <TableEmpty colSpan={8} message="本次回测没有委托记录" /> : report.orders.map((order) => <tr key={order.id}><td>{dateTime(order.created_at || order.timestamp)}</td><td><strong title={order.symbol}>{displaySymbol(order.symbol)}</strong></td><td><span className={`side-label ${lower(order.side) === 'buy' ? 'buy' : 'sell'}`}>{sideLabel(order.side)}</span></td><td>{orderTypeLabel(order.order_type || order.type)}</td><td className="num">{money(order.limit_price ?? order.price)}</td><td className="num">{integer(order.quantity)}</td><td className="num">{integer(order.filled_quantity ?? order.filled_qty)}</td><td><Badge>{statusLabel(order.status)}</Badge></td></tr>)}
              </tbody></table>}
            </div>
          </Panel>
        </div>
      )}
    </div>
  );
}
