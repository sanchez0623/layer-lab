import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, AlertTriangle, ArrowDownToLine, BarChart3, CalendarDays, ChevronDown, ChevronUp,
  CircleDollarSign, Clock3, FlaskConical, Gauge, Layers3, Play, RotateCcw, ShieldAlert, Square, TrendingDown, WalletCards,
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
import ScreenerConfigPanel from '../components/ScreenerConfigPanel';
import PaginationBar from '../components/PaginationBar';
import { Badge, EmptyState, ErrorBanner, Field, Loading, Metric, PageHeader, Panel, SuccessBanner, SyntheticNotice, TableEmpty } from '../components/Ui';
import { defaultFeeConfig, feeBreakdownTitle, strategyToFeeConfig, stripFeeFields } from '../lib/feeConfig';
import type { BacktestJobView, BacktestSummary, Dataset, FeeConfig, Paginated, Report, Round, RotationTrailItem, ScreenerConfig, StrategyConfig } from '../types';

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
  t_target_net_spread: 0.03,
  t_exit_cutoff: '14:50',
};

/** 回测模式：funnel = 漏斗轮动（清仓后重新选股换票）；single = 传统单标的。 */
type BacktestMode = 'funnel' | 'single';

/** 后台作业终态：轮询到这些状态即停止。CANCELLED / INTERRUPTED / FAILED 可续跑。 */
const JOB_TERMINAL_STATUSES = ['COMPLETED', 'CANCELLED', 'FAILED', 'INTERRUPTED'];
const JOB_RESUMABLE_STATUSES = ['CANCELLED', 'INTERRUPTED', 'FAILED'];

/** 漏斗轮动回测默认选股阈值；比率字段为小数，界面百分比换算在 ScreenerConfigPanel 内完成。 */
const fallbackScreener: ScreenerConfig = {
  boards: ['创业板', '科创板'],
  chg20_max: -0.15,
  week52_drawdown: 0.3,
  day_change_max: 0.05,
  min_amount: 5e8,
  min_avg_amount_20d: 5e8,
  min_listing_sessions: 6,
  exclude_st: true,
  exclude_delisting: true,
  exclude_unprofitable_u: true,
  enable_fundamental_veto: false,
  max_deep_checks: 10,
  blacklist: [],
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
  const [applying, setApplying] = useState(false);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState('');
  const [pendingNotice, setPendingNotice] = useState('');
  // 后台回测作业：submitting=提交中，jobView=轮询到的最新进度，jobBusy=取消/续跑请求在途。
  // 轮询只由 jobView 驱动（jobId 存进 ref 避免闭包过期），卸载时清 interval，导航离开后服务端仍继续跑。
  const [submitting, setSubmitting] = useState(false);
  const [jobView, setJobView] = useState<BacktestJobView | null>(null);
  const [jobBusy, setJobBusy] = useState(false);
  const jobIdRef = useRef<string | null>(null);
  const pollTimerRef = useRef<number | null>(null);
  // 本会话内已落地为终态的作业 id：重挂载时用它避免把刚「放弃」的已完成作业重新拉起。
  const settledJobIdsRef = useRef<Set<string>>(new Set());
  const [tableTab, setTableTab] = useState<'rounds' | 'trades' | 'orders'>('rounds');
  const [exporting, setExporting] = useState<'trades' | 'orders' | null>(null);
  const [mode, setMode] = useState<BacktestMode>('funnel');
  const [screener, setScreener] = useState<ScreenerConfig>(fallbackScreener);
  // 换票轨迹后台分页状态：主报告已瘦身不含全量 trail，列表按页拉取。
  const [trailData, setTrailData] = useState<Paginated<RotationTrailItem> | null>(null);
  const [trailPage, setTrailPage] = useState(1);
  const [trailPageSize, setTrailPageSize] = useState(20);
  const [trailLoading, setTrailLoading] = useState(false);

  const loadInitial = async () => {
    setLoading(true);
    setError('');
    try {
      const [datasetResult, defaults, reports] = await Promise.all([api.datasets(), api.strategyDefaults(), api.backtests()]);
      setDatasets(datasetResult);
      // 重挂载时重新挂到仍在跑的后台作业（导航离开后服务端继续跑），避免丢失进度条。
      try {
        const { jobs } = await api.backtestJobList(10);
        const active = jobs.find((job) => job.status === 'RUNNING' && !settledJobIdsRef.current.has(job.id));
        if (active) {
          jobIdRef.current = active.id;
          setJobView(active);
          setRunning(true);
        }
      } catch { /* 作业列表不可用时静默忽略，不影响页面初始化 */ }
      const defaultConfig = defaults.config || defaults;
      setConfig((current) => ({ ...current, ...defaultConfig }));
      setFees(defaults.fees || strategyToFeeConfig(defaultConfig));
      if (defaults.initial_cash) setInitialCash(defaults.initial_cash);
      // 漏斗轮动回测：用后端选股默认阈值打底（失败不阻塞，沿用本地 fallbackScreener）。
      api.screenerDefaults().then((screenerDefaults) => {
        if (screenerDefaults?.config) {
          setScreener((current) => ({ ...current, ...screenerDefaults.config, enable_fundamental_veto: false }));
        }
      }).catch(() => undefined);
      const daily = datasetResult.find((item) => item.timeframe === 'daily' && item.source === 'baostock')
        || datasetResult.find((item) => item.timeframe === 'daily');
      if (daily) {
        setDatasetId((current) => current || daily.id);
        setStartDate((current) => current || daily.start?.slice(0, 10));
        setEndDate((current) => current || daily.end?.slice(0, 10));
        const minute = datasetResult.find((item) => item.timeframe === '5m' && item.symbol === daily.symbol);
        if (minute) setIntradayId((current) => current || minute.id);
        const index = datasetResult.find((item) => item.timeframe === 'daily' && ['sh.000688', 'sz.399006'].includes(item.symbol));
        if (index) setIndexId((current) => current || index.id);
      }
      // 选股页「带入回测」写入的待应用数据集：预填三个选择器后立即清除，只消费一次。
      const pendingRaw = sessionStorage.getItem('layerlab.pendingDatasets');
      if (pendingRaw) {
        sessionStorage.removeItem('layerlab.pendingDatasets');
        try {
          const pending = JSON.parse(pendingRaw) as {
            runId?: string;
            candidate?: { code?: string; name?: string };
            datasets?: { daily?: Dataset; intraday?: Dataset | null; index?: Dataset | null };
          };
          const available = (id?: string) => Boolean(id && datasetResult.some((item) => item.id === id));
          let missing = 0;
          const pendingDaily = pending.datasets?.daily;
          if (pendingDaily && available(pendingDaily.id)) {
            setDatasetId(pendingDaily.id);
            if (pendingDaily.start) setStartDate(pendingDaily.start.slice(0, 10));
            if (pendingDaily.end) setEndDate(pendingDaily.end.slice(0, 10));
          } else if (pendingDaily) {
            missing += 1;
          }
          const pendingIntraday = pending.datasets?.intraday;
          if (pendingIntraday) {
            if (available(pendingIntraday.id)) setIntradayId(pendingIntraday.id);
            else missing += 1;
          } else {
            setIntradayId('');
          }
          const pendingIndex = pending.datasets?.index;
          if (pendingIndex) {
            if (available(pendingIndex.id)) setIndexId(pendingIndex.id);
            else missing += 1;
          } else {
            setIndexId('');
          }
          const code = pending.candidate?.code || pendingDaily?.symbol || '';
          const name = pending.candidate?.name || pendingDaily?.name || '';
          setPendingNotice(
            `已从选股结果带入：${[code, name].filter(Boolean).join(' ')}（运行 ${pending.runId || '—'}）`
            + (missing > 0 ? `；${missing} 个数据集未在后端找到，已保留默认选择` : '，可直接运行回测'),
          );
        } catch {
          // 忽略损坏的 sessionStorage 数据
        }
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
  const intradayOptions = datasets.filter((item) => item.id !== datasetId && item.timeframe === '5m');
  const indexOptions = datasets.filter((item) => item.id !== datasetId && item.timeframe === 'daily' && ['sh.000688', 'sz.399006'].includes(item.symbol));
  // 在途作业：RUNNING 或可续跑（CANCELLED/INTERRUPTED/FAILED）时禁止再起新回测，避免与服务端单作业互斥冲突。
  const jobActive = running || submitting || (!!jobView && (jobView.status === 'RUNNING' || JOB_RESUMABLE_STATUSES.includes(jobView.status)));

  const stopPolling = () => {
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  /** 作业到达终态后的收尾：COMPLETED 拉全量报告并刷新历史；终态保留在 jobView 供继续/放弃，直到用户处理。 */
  const settleJob = async (view: BacktestJobView) => {
    stopPolling();
    jobIdRef.current = view.id;
    setJobView(view);
    setRunning(false);
    if (view.status === 'COMPLETED') {
      settledJobIdsRef.current.add(view.id);
      try {
        const result = await api.backtest(view.id);
        setReport(result);
        setTableTab('rounds');
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : '回测报告读取失败。');
      }
      const reports = await api.backtests().catch(() => null);
      if (reports) setHistory(reports); else setHistory((current) => current);
    }
  };

  /** 提交后台作业并开始轮询进度；409（已有回测在跑）向上抛出，由调用方 setError 呈现。 */
  const startJob = async (mode: 'single' | 'funnel') => {
    const payload = mode === 'funnel'
      ? {
        mode, name: name.trim() || '选股漏斗轮动回测',
        initial_cash: initialCash, start_date: startDate || undefined, end_date: endDate || undefined,
        config: stripFeeFields(config), fees, screener,
      }
      : {
        mode, name: name.trim() || '未命名回测', dataset_id: datasetId,
        intraday_dataset_id: intradayId || undefined, index_dataset_id: indexId || undefined,
        initial_cash: initialCash, start_date: startDate || undefined, end_date: endDate || undefined,
        config: stripFeeFields(config), fees,
      };
    const { job_id } = await api.runBacktestJob(payload);
    jobIdRef.current = job_id;
    setRunning(true);
    try {
      const view = await api.backtestJobProgress(job_id);
      setJobView(view);
      if (JOB_TERMINAL_STATUSES.includes(view.status)) await settleJob(view);
    } catch (cause) {
      // 进度查询失败不阻断：保留在途状态，交给轮询继续重试。
      setError(cause instanceof Error ? cause.message : '回测进度查询失败。');
    }
  };

  const run = async () => {
    if (startDate && endDate && startDate > endDate) { setError('开始日期不能晚于结束日期，请调整回测区间。'); return; }
    if (!Number.isFinite(initialCash) || initialCash <= 0) { setError('初始资金必须大于 0。'); return; }
    // 单标的模式必须先选主数据集；漏斗轮动模式用本地 universe.db，无需数据集。
    if (mode === 'single' && !datasetId) { setError('请选择主数据集后再运行回测。'); return; }
    if (mode === 'funnel' && screener.boards.length === 0) { setError('请至少选择一个板块（创业板 / 科创板）。'); return; }
    setSubmitting(true);
    setRunning(true);
    setError('');
    try {
      await startJob(mode);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '回测运行失败，请稍后重试。');
      setRunning(false);
    } finally {
      setSubmitting(false);
    }
  };

  // 把当前回测配置（策略字段 + 费率）整套应用到模拟盘「当前账户」，下一根 K 起生效。
  const applyToPaper = async () => {
    setApplying(true);
    setError('');
    try {
      const current = await api.currentAccount();
      await api.applyAccountConfig(current.id, { config: stripFeeFields(config), fees });
      setPendingNotice(`已把回测配置应用到模拟盘账户「${current.name}」，下一根 K 线起生效。`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '应用到模拟盘失败，请确认模拟盘服务可用。');
    } finally {
      setApplying(false);
    }
  };

  /** 续跑 CANCELLED / INTERRUPTED / FAILED 作业：从 checkpoint 接着跑并恢复轮询。 */
  const resumeJob = async () => {
    const jobId = jobIdRef.current || jobView?.id;
    if (!jobId) return;
    setJobBusy(true);
    setError('');
    try {
      await api.resumeBacktestJob(jobId);
      jobIdRef.current = jobId;
      setRunning(true);
      const view = await api.backtestJobProgress(jobId);
      setJobView(view);
      if (JOB_TERMINAL_STATUSES.includes(view.status)) await settleJob(view);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '续跑失败，请稍后重试。');
    } finally {
      setJobBusy(false);
    }
  };

  /** 请求取消：后端在下一个交易日边界停下，作业转 CANCELLED（可续跑）。 */
  const cancelJob = async () => {
    const jobId = jobIdRef.current || jobView?.id;
    if (!jobId) return;
    setJobBusy(true);
    setError('');
    try { await api.cancelBacktestJob(jobId); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '取消失败，请稍后重试。'); }
    finally { setJobBusy(false); }
  };

  /** 放弃在途作业：仅清空前端在途状态，服务端作业本身不受影响。 */
  const dismissJob = () => {
    stopPolling();
    const id = jobIdRef.current || jobView?.id;
    if (id) settledJobIdsRef.current.add(id);
    jobIdRef.current = null;
    setJobView(null);
    setRunning(false);
  };

  // 进度轮询：作业在途时每秒拉一次，直到终态；卸载即清 interval，导航离开后服务端继续跑。
  useEffect(() => {
    if (!running || !jobView || JOB_TERMINAL_STATUSES.includes(jobView.status)) return;
    stopPolling();
    pollTimerRef.current = window.setInterval(() => {
      const jobId = jobIdRef.current;
      if (!jobId) return;
      void api.backtestJobProgress(jobId).then(async (view) => {
        if (JOB_TERMINAL_STATUSES.includes(view.status)) await settleJob(view);
        else setJobView(view);
      }).catch(() => undefined);
    }, 1000);
    return stopPolling;
  }, [running, jobView?.id, jobView?.status]);

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
  // 漏斗轮动回测专属：每轮换票轨迹（选中日期/标的/入选依据/进出日期/盈亏）。
  // 主报告已瘦身不含全量 trail，改走 GET /backtests/{id}/rotation-trail 后台分页。
  const isFunnel = String(report?.data_info?.mode) === 'funnel_rotation';
  const rotationTrail = useMemo<RotationTrailItem[]>(() => trailData?.items ?? [], [trailData]);
  // 换票轨迹行展开：查看该选股日的逐轮决策日志（为什么开门/关门/空仓/旁路几只）+ 漏斗计数。
  const [expandedTrails, setExpandedTrails] = useState<Set<string>>(new Set());
  const toggleTrail = (key: string) => {
    setExpandedTrails((current) => {
      const next = new Set(current);
      if (next.has(key)) { next.delete(key); } else { next.add(key); }
      return next;
    });
  };

  // 切换报告时回到第一页（同一 report 内翻页不重置）。
  useEffect(() => { setTrailPage(1); }, [report?.id]);

  useEffect(() => {
    if (!report || !isFunnel) { setTrailData(null); return; }
    let cancelled = false;
    setTrailLoading(true);
    api.backtestRotationTrail(report.id, trailPage, trailPageSize)
      .then((paged) => { if (!cancelled) setTrailData(paged); })
      .catch(() => { if (!cancelled) setTrailData(null); })
      .finally(() => { if (!cancelled) setTrailLoading(false); });
    return () => { cancelled = true; };
  }, [report, isFunnel, trailPage, trailPageSize]);

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
      {pendingNotice && <SuccessBanner onClose={() => setPendingNotice('')}>{pendingNotice}</SuccessBanner>}

      <Panel className="config-panel">
        <form onSubmit={(event) => { event.preventDefault(); void run(); }} noValidate>
          <div className="config-topline">
            <div className="config-title"><span className="step-index">01</span><div><h2>实验配置</h2><p>定义数据边界与交易摩擦，再启动本地回测。</p></div></div>
            <div className="backtest-mode-toggle" role="tablist" aria-label="回测模式">
              <button
                type="button"
                role="tab"
                className={`backtest-mode-tab ${mode === 'funnel' ? 'active' : ''}`}
                aria-selected={mode === 'funnel'}
                onClick={() => setMode('funnel')}
              >
                <Layers3 size={15} />漏斗轮动回测
              </button>
              <button
                type="button"
                role="tab"
                className={`backtest-mode-tab ${mode === 'single' ? 'active' : ''}`}
                aria-selected={mode === 'single'}
                onClick={() => setMode('single')}
              >
                <BarChart3 size={15} />单标的回测
              </button>
            </div>
          </div>
          <label className="switch-row enable-t-row">
            <span><strong>启用做 T</strong><small>可选，不占用 16 笔序列</small></span>
            <input type="checkbox" checked={config.enable_t} onChange={(event) => updateConfig('enable_t', event.target.checked)} />
            <span className="switch" />
          </label>
          {config.enable_t && (
            <div className="form-grid t-params-grid">
              <Field label="做 T 净利止盈（%）" hint="扣完成本后净赚该幅度即平 T" className="span-3">
                <div className="input-with-unit">
                  <input type="number" min="0" max="100" step="0.1" value={Number(((config.t_target_net_spread ?? 0.03) * 100).toFixed(2))} onChange={(event) => updateConfig('t_target_net_spread', Number(event.target.value) / 100)} />
                  <span>%</span>
                </div>
              </Field>
              <Field label="做 T T+1 截止时间" hint="到点仍未止盈则尾盘强平" className="span-3">
                <div className="input-with-unit">
                  <input type="time" step="300" value={config.t_exit_cutoff ?? '14:50'} onChange={(event) => updateConfig('t_exit_cutoff', event.target.value)} />
                </div>
              </Field>
            </div>
          )}
          {mode === 'funnel' && (
            <div className="funnel-mode-note">
              <Layers3 size={15} />
              <span>
                漏斗轮动回测：<strong>不再锁定单一标的</strong>，而是每次清仓后按下方选股漏斗（L0→L4）重跑、取当时第一名换票，与模拟盘「清仓后重新选股」口径一致；
                行情取自本地 universe.db 缓存（双创日线），为日线口径（5 分钟做 T 与 tick 级风控降级）。
              </span>
            </div>
          )}
          <div className={`form-grid ${mode === 'funnel' ? 'backtest-basics-grid' : 'backtest-form-grid'}`}>
            <Field label="实验名称" className={mode === 'funnel' ? 'backtest-name-field' : 'span-2'}><input value={name} onChange={(event) => setName(event.target.value)} placeholder="输入回测名称" /></Field>
            {mode === 'single' && (
              <Field label="主数据集" hint="quan_parquet / baostock 日线" className="span-2">
                <select value={datasetId} onChange={(event) => {
                  const next = datasets.find((item) => item.id === event.target.value);
                  setDatasetId(event.target.value);
                  if (next) {
                    setStartDate(next.start?.slice(0, 10));
                    setEndDate(next.end?.slice(0, 10));
                    const minute = datasets.find((item) => item.timeframe === '5m' && item.symbol === next.symbol);
                    if (minute) setIntradayId(minute.id);
                  }
                }} required>
                  <option value="">请选择数据集</option>
                  {datasets.filter((item) => item.timeframe === 'daily').map((item) => <option key={item.id} value={item.id}>{item.name} · {displaySymbol(item.symbol)} · {item.source}</option>)}
                </select>
              </Field>
            )}
            <Field label="开始日期"><div className="input-icon"><CalendarDays size={15} /><input type="date" value={startDate} min={mode === 'single' ? selectedDataset?.start?.slice(0, 10) : undefined} max={endDate || (mode === 'single' ? selectedDataset?.end?.slice(0, 10) : undefined)} onChange={(event) => setStartDate(event.target.value)} /></div></Field>
            <Field label="结束日期"><div className="input-icon"><CalendarDays size={15} /><input type="date" value={endDate} min={startDate || (mode === 'single' ? selectedDataset?.start?.slice(0, 10) : undefined)} max={mode === 'single' ? selectedDataset?.end?.slice(0, 10) : undefined} onChange={(event) => setEndDate(event.target.value)} /></div></Field>
            <Field label="初始资金" className={mode === 'funnel' ? 'backtest-cash-field' : ''}><div className="input-with-unit"><input type="number" min="1" step="10000" value={initialCash} onChange={(event) => { const parsed = Number(event.target.value); setInitialCash(Number.isFinite(parsed) ? parsed : 0); }} required /><span>CNY</span></div></Field>
            {mode === 'single' && (
              <>
                <Field label="5 分钟数据集" hint="腾讯 / quan_parquet 5 分钟"><select value={intradayId} onChange={(event) => setIntradayId(event.target.value)}><option value="">不使用（日线降级）</option>{intradayOptions.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.timeframe} · {item.source}</option>)}</select></Field>
                <Field label="指数数据集" hint="大盘门（日线）"><select value={indexId} onChange={(event) => setIndexId(event.target.value)}><option value="">不使用</option>{indexOptions.map((item) => <option key={item.id} value={item.id}>{item.name} · {displaySymbol(item.symbol)}</option>)}</select></Field>
              </>
            )}
          </div>
          {mode === 'funnel' && <ScreenerConfigPanel value={screener} onChange={setScreener} fundamentalLockedOff />}
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
              {mode === 'funnel' ? (
                <span>漏斗轮动 · {screener.boards.join(' / ')} · 每次清仓后重跑 L0→L4 取第一名换票（日线口径）</span>
              ) : selectedDataset ? (
                <><span>{selectedDataset.rows.toLocaleString('zh-CN')} 行</span><i /> <span>{selectedDataset.timeframe} 粒度</span><i /><span>{shortDate(selectedDataset.start)} — {shortDate(selectedDataset.end)}</span>{selectedDataset.is_synthetic && <SyntheticNotice compact />}</>
              ) : (
                <span>请选择主数据集以查看数据边界</span>
              )}
            </div>
            <button className="button secondary" type="button" onClick={() => void applyToPaper()} disabled={applying || jobActive}>
              {applying ? <><span className="button-spinner" />正在应用</> : <><ArrowDownToLine size={16} />应用到模拟盘</>}
            </button>
            <button className="button primary run-button" type="submit" disabled={jobActive || (mode === 'single' && !datasetId)}>
              {jobActive ? <><span className="button-spinner" />正在运行回测</> : <><Play size={16} fill="currentColor" />运行回测</>}
            </button>
          </div>
          {jobView && (
            <div className="job-progress" role="status">
              <div className="job-progress-topline">
                <span className="job-progress-label">
                  {jobView.status === 'RUNNING' ? '回测进行中' : `回测${statusLabel(jobView.status)}`}
                  {' · '}
                  {jobView.name || '后台回测'}
                </span>
                <div className="job-progress-actions">
                  {jobView.status === 'RUNNING' && (
                    <button type="button" className="button secondary" onClick={() => void cancelJob()} disabled={jobBusy}>
                      <Square size={14} fill="currentColor" />停止
                    </button>
                  )}
                  {JOB_RESUMABLE_STATUSES.includes(jobView.status) && (
                    <>
                      <button type="button" className="button secondary" onClick={() => void resumeJob()} disabled={jobBusy}>
                        <Play size={14} fill="currentColor" />继续
                      </button>
                      <button type="button" className="button secondary" onClick={dismissJob} disabled={jobBusy}>放弃</button>
                    </>
                  )}
                </div>
              </div>
              <div className="job-progress-track" aria-hidden="true">
                <div className="job-progress-fill" style={{ width: `${Math.max(0, Math.min(100, jobView.progress))}%` }} />
              </div>
              <div className="job-progress-meta">
                <span>{jobView.progress.toFixed(1)}%</span>
                <span>{jobView.cursor_date ? `已跑至 ${jobView.cursor_date}` : '等待首个交易日完成'}</span>
                {jobView.total_days ? <span>共 {integer(jobView.total_days)} 交易日</span> : null}
                {jobView.error ? <span className="negative">{jobView.error}</span> : null}
              </div>
            </div>
          )}
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
              <div className="lineage-tags"><Badge>{String(report.data_info.daily_source || selectedDataset?.source || 'baostock')} 日线</Badge>{intradayId && <Badge tone="indigo">{String(report.data_info.intraday_source || 'tencent')} 5分钟</Badge>}{indexId && <Badge tone="green">指数大盘门</Badge>}{selectedDataset?.is_synthetic && <SyntheticNotice compact />}</div>
            </div>
            {Object.keys(report.data_info || {}).length > 0 && <div className="data-info-grid">{Object.entries(report.data_info).map(([key, value]) => <div key={key}><span>{key}</span><strong title={typeof value === 'object' ? JSON.stringify(value) : String(value)}>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</strong></div>)}</div>}
            {report.warnings.length > 0 ? <div className="warning-box"><AlertTriangle size={18} /><div><strong>数据与近似口径提醒</strong><small>全程不变的口径 / 数据边界；逐轮决策日志见下方「换票轨迹」展开行</small><ul>{report.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div></div> : <div className="no-warning"><FlaskConical size={15} />后端未返回额外警告；仍请留意日线无法还原精确 tick 成交路径。</div>}
          </Panel>

          {isFunnel && trailData && trailData.total > 0 && (
            <Panel className="rotation-trail-panel" title="换票轨迹" description={`每次清仓后重跑选股漏斗（L0→L4）取当时第一名；WAITING 为 L0 关门或无候选的空仓等待 · 点行首箭头展开该轮决策日志 · 共 ${trailData.total} 次筛选`} action={<Layers3 size={18} />}>
              <div className="table-scroll">
                <table>
                  <thead><tr><th></th><th>轮次</th><th>选股日</th><th>标的</th><th>名称</th><th className="num">入选 Chg20D</th><th>L0 门</th><th className="num">入场</th><th className="num">出场</th><th className="num">净盈亏</th><th>状态</th></tr></thead>
                  <tbody>
                    {rotationTrail.map((item, index) => {
                      const trailKey = item.round || `${item.select_date}-${index}`;
                      const expanded = expandedTrails.has(trailKey);
                      return (
                        <Fragment key={trailKey}>
                          <tr className={item.status === 'WAITING' ? 'trail-waiting' : ''}>
                            <td>
                              <button className="trail-expand-btn" onClick={() => toggleTrail(trailKey)} aria-label={expanded ? '收起本轮决策日志' : '展开本轮决策日志'} title={expanded ? '收起决策日志' : '查看本轮决策日志'}>
                                {expanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
                              </button>
                            </td>
                            <td><strong className="round-id">{item.round || `#${(trailPage - 1) * trailPageSize + index + 1}`}</strong></td>
                            <td>{shortDate(item.select_date)}</td>
                            <td>{item.symbol ? <strong title={item.symbol}>{displaySymbol(item.symbol)}</strong> : <span className="muted">空仓等待</span>}</td>
                            <td>{item.name || '—'}</td>
                            <td className={`num ${item.chg20d_at_select != null ? (item.chg20d_at_select >= 0 ? 'positive' : 'negative') : ''}`}>{item.chg20d_at_select != null ? signedPercent(item.chg20d_at_select) : '—'}</td>
                            <td><Badge tone={item.gate_open ? 'green' : 'amber'}>{item.gate_open ? '开门' : '关门'}</Badge></td>
                            <td className="num">{item.entry_date ? shortDate(item.entry_date) : '—'}</td>
                            <td className="num">{item.exit_date ? shortDate(item.exit_date) : '—'}</td>
                            <td className={`num ${item.pnl != null ? (item.pnl >= 0 ? 'positive' : 'negative') : ''}`}>{item.pnl != null ? signedMoney(item.pnl) : '—'}</td>
                            <td><Badge tone={item.status === 'CLOSED' ? 'green' : item.status === 'OPEN' ? 'indigo' : 'amber'}>{item.status === 'CLOSED' ? '已结束' : item.status === 'OPEN' ? '持仓中' : '空仓等待'}</Badge></td>
                          </tr>
                          {expanded && (
                            <tr className="trail-detail-row">
                              <td></td>
                              <td colSpan={10}>
                                <div className="trail-detail">
                                  <div className="trail-funnel">
                                    {[
                                      { tag: '全市场', value: item.funnel?.universe },
                                      { tag: 'L0 大盘门', value: item.funnel?.after_l0 },
                                      { tag: 'L1 超跌', value: item.funnel?.after_l1 },
                                      { tag: 'L2 否决', value: item.funnel?.after_l2 },
                                      { tag: 'L3 容量', value: item.funnel?.after_l3 },
                                      { tag: '可用候选', value: item.funnel?.eligible },
                                    ].map((stage) => (
                                      <span className="trail-funnel-stage" key={stage.tag}><small>{stage.tag}</small><strong>{stage.value ?? '—'}</strong></span>
                                    ))}
                                  </div>
                                  {item.screen_warnings && item.screen_warnings.length > 0 ? (
                                    <ul className="trail-warnings">
                                      {item.screen_warnings.map((warning, wIndex) => <li key={wIndex}>{warning}</li>)}
                                    </ul>
                                  ) : <p className="trail-no-warning">本轮选股无逐轮决策提示。</p>}
                                </div>
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <PaginationBar
                page={trailData.page}
                pageSize={trailData.page_size}
                total={trailData.total}
                totalPages={trailData.total_pages}
                onPageChange={setTrailPage}
                onPageSizeChange={(size) => { setTrailPageSize(size); setTrailPage(1); }}
                disabled={trailLoading}
              />
            </Panel>
          )}

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
