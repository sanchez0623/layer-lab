import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, AlertTriangle, ArrowDownToLine, ArrowLeft, BarChart3, CalendarDays, ChevronDown, ChevronUp,
  CircleDollarSign, Clock3, FlaskConical, Gauge, Layers3, Loader2, Play, RefreshCw, RotateCcw, ShieldAlert, Square, Trash2, TrendingDown, WalletCards,
} from 'lucide-react';
import {
  Area, Bar, BarChart, CartesianGrid, Cell, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { api, downloadBacktestCsv } from '../lib/api';
import {
  dateTime, displaySymbol, integer, lower, money, number, orderTypeLabel, percent, purposeLabel, shortDate, sideLabel,
  signedMoney, signedPercent, statusLabel,
} from '../lib/format';
import { toast } from 'sonner';
import FeeConfigPanel from '../components/FeeConfigPanel';
import ScreenerConfigPanel from '../components/ScreenerConfigPanel';
import PaginationBar from '../components/PaginationBar';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { Badge, EmptyState, ErrorBanner, Field, FilterSelect, FormGrid, Loading, Metric, MetricGrid, PageHeader, Panel, SelectField, SwitchRow, SyntheticNotice, TableEmpty } from '../components/Ui';
import SearchInput from '../components/SearchInput';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Progress } from '@/components/ui/progress';
import { Switch } from '@/components/ui/switch';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { cn } from '@/lib/utils';
import { defaultFeeConfig, feeBreakdownTitle, strategyToFeeConfig, stripFeeFields } from '../lib/feeConfig';
import { EMPTY_TRADE_FILTERS, PURPOSE_FILTER_OPTIONS, SIDE_FILTER_OPTIONS, matchesTradeFilters, tradeFilterActive } from '../lib/recordFilters';
import type { TradeFilters } from '../lib/recordFilters';
import type { BacktestJobView, BacktestSummary, Dataset, FeeConfig, MonthlyPnl, Order, Paginated, RecordsPage, Report, Round, RotationTrailItem, ScreenerConfig, StrategyConfig, Trade } from '../types';

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
  max_holding_days: 22,
  holding_tp_override: 0.03,
  etf_beta_enabled: false,
  etf_symbol: 'sz.159915',
  etf_position_pct: 0.2,
  etf_entry_ma_days: 60,
  etf_exit_ma_days: 20,
  etf_hard_stop_pct: 0.06,
  // 入场保护（回测默认开启，可置 0 关闭）：不追高 8% + 清仓后冷却 5 个交易日。
  etf_entry_max_deviation: 0.08,
  etf_reentry_cooldown_days: 5,
};

/** 回测模式：funnel = 漏斗轮动（清仓后重新选股换票）；single = 传统单标的。 */
type BacktestMode = 'funnel' | 'single';

/** 工作台子视图：run = 运行回测（配置 / 进度），reports = 回测报告（列表 / 搜索 / 删除 / 详情）。 */
type WorkspaceTab = 'run' | 'reports';

/** 后台作业终态：轮询到这些状态即停止。CANCELLED / INTERRUPTED / FAILED 可续跑。 */
const JOB_TERMINAL_STATUSES = ['COMPLETED', 'CANCELLED', 'FAILED', 'INTERRUPTED'];
const JOB_RESUMABLE_STATUSES = ['CANCELLED', 'INTERRUPTED', 'FAILED'];
/** 在途状态：服务端 worker 还挂着（或正在收尾取消），页面重挂后要继续轮询进度。 */
const JOB_ACTIVE_STATUSES = ['RUNNING', 'CANCELLING'];
/** 「放弃」过的作业 id 落 sessionStorage：刷新后不再重新弹面板（内存 ref 刷新即丢）。 */
const DISMISSED_JOBS_KEY = 'layerlab.dismissedBacktestJobs';

const readDismissedJobs = (): Set<string> => {
  try {
    const raw = sessionStorage.getItem(DISMISSED_JOBS_KEY);
    return new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch { return new Set(); }
};

const rememberDismissedJob = (jobId: string) => {
  const dismissed = readDismissedJobs();
  dismissed.add(jobId);
  try { sessionStorage.setItem(DISMISSED_JOBS_KEY, JSON.stringify([...dismissed])); } catch { /* 存储不可用时忽略 */ }
};

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

/** 月度最大回撤按「负号 + 百分比」展示；0 不加负号。 */
const drawdownLabel = (value: number): string => (value > 0 ? `-${percent(value)}` : percent(0));

/** 图表 X 轴月份压缩为 YY-MM（2026-01 → 26-01），满轴时避免标签相互挤压。 */
const shortMonth = (month: string): string => month.slice(2);

function UnitInput({
  value, onChange, unit, min, max, step, type = 'number',
}: {
  value: string | number;
  onChange: (value: string) => void;
  unit: string;
  min?: number;
  max?: number;
  step?: number;
  type?: string;
}) {
  return (
    <div className="relative">
      <Input type={type} min={min} max={max} step={step} value={value} onChange={(event) => onChange(event.target.value)} className="pr-10" />
      <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 font-mono text-[11px] text-muted-foreground">{unit}</span>
    </div>
  );
}

export default function BacktestPage() {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
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
  const [pendingDelete, setPendingDelete] = useState<{ id: string; name: string } | null>(null);
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
  // 成交明细 / 委托记录 / 已结束轮次 后台分页：主报告只带 *_total，明细按页拉取。
  const [recordData, setRecordData] = useState<RecordsPage<Trade | Order | Round> | null>(null);
  const [recordPage, setRecordPage] = useState(1);
  const [recordPageSize, setRecordPageSize] = useState(20);
  const [recordLoading, setRecordLoading] = useState(false);
  // 每月盈亏表前端分页：月度数据由后端 metrics.monthly 下发（派生自权益曲线），无需额外请求。
  const [monthPage, setMonthPage] = useState(1);
  const [monthPageSize, setMonthPageSize] = useState(12);
  // 成交明细筛选：symbolDraft 是输入框即时值（防抖后提交进 tradeFilter），
  // purpose / side 由下拉直接提交；三者都是空串表示「全部」。
  const [symbolDraft, setSymbolDraft] = useState('');
  const [tradeFilter, setTradeFilter] = useState<TradeFilters>(EMPTY_TRADE_FILTERS);
  const tradeFilterOn = tradeFilterActive(tradeFilter);
  // 工作台视图：运行回测 / 回测报告。报告列表走后台分页 + 服务端搜索，列表项只含轻量摘要。
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('run');
  const [viewingReport, setViewingReport] = useState(false);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportList, setReportList] = useState<Paginated<BacktestSummary> | null>(null);
  const [reportPage, setReportPage] = useState(1);
  const [reportPageSize, setReportPageSize] = useState(20);
  const [reportSearch, setReportSearch] = useState('');
  const [reportQuery, setReportQuery] = useState('');
  const [reportListLoading, setReportListLoading] = useState(false);
  const [reportListVersion, setReportListVersion] = useState(0);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const loadInitial = async () => {
    setLoading(true);
    setError('');
    try {
      const [datasetResult, defaults] = await Promise.all([api.datasets(), api.strategyDefaults()]);
      setDatasets(datasetResult);
      // 重挂载时重新挂到仍在跑的后台作业（导航离开后服务端继续跑），避免丢失进度条。
      // 除 RUNNING 外还要重挂「可续跑但没在跑」的作业（刷新前进程重启/取消/失败）：
      // 这类作业不在轮询态，但必须把面板带回来，否则用户刷新后既看不到也续跑不了。
      try {
        const { jobs } = await api.backtestJobList(20);
        const fresh = (jobId: string) => !settledJobIdsRef.current.has(jobId)
          && !readDismissedJobs().has(jobId);
        const active = jobs.find((job) => fresh(job.id) && JOB_ACTIVE_STATUSES.includes(job.status));
        if (active) {
          jobIdRef.current = active.id;
          setJobView(active);
          setRunning(true);
        } else {
          const resumable = jobs.find((job) => fresh(job.id) && JOB_RESUMABLE_STATUSES.includes(job.status));
          if (resumable) {
            jobIdRef.current = resumable.id;
            setJobView(resumable);
            setRunning(false);
          }
        }
      } catch { /* 作业列表不可用时静默忽略，不影响页面初始化 */ }
      const defaultConfig = defaults.config || defaults;
      // 超期降阈默认预填 22 天 / 3%（后端默认 None = 关闭，UI 侧给推荐值，可由用户改空关闭）。
      // ETF 入场保护同理：后端 StrategyConfig 默认 0（关闭，保证旧账户/接口调用方行为不变），
      // 回测表单默认预填推荐值 8% / 5 个交易日，用户可改成 0 关闭。
      setConfig((current) => ({
        ...current, ...defaultConfig,
        max_holding_days: defaultConfig.max_holding_days ?? 22,
        holding_tp_override: defaultConfig.holding_tp_override ?? 0.03,
        etf_entry_max_deviation: defaultConfig.etf_entry_max_deviation || 0.08,
        etf_reentry_cooldown_days: defaultConfig.etf_reentry_cooldown_days || 5,
      }));
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
          toast.success(
            `已从选股结果带入：${[code, name].filter(Boolean).join(' ')}（运行 ${pending.runId || '—'}）`
            + (missing > 0 ? `；${missing} 个数据集未在后端找到，已保留默认选择` : '，可直接运行回测'),
          );
        } catch {
          // 忽略损坏的 sessionStorage 数据
        }
      }
      // 报告列表按需在「回测报告」tab 加载，进入页面不再自动拉取最新报告（大 payload 只在打开详情时读取）。
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
  // 在途作业：RUNNING/CANCELLING 或可续跑（CANCELLED/INTERRUPTED/FAILED）时禁止再起新回测，
  // 避免与服务端单作业互斥冲突；想另起必须先对旧作业做「继续」或「放弃」。
  const jobActive = running || submitting
    || (!!jobView && (JOB_ACTIVE_STATUSES.includes(jobView.status) || JOB_RESUMABLE_STATUSES.includes(jobView.status)));

  const stopPolling = () => {
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  /** 作业到达终态后的收尾：COMPLETED 拉全量报告并切到报告详情；终态保留在 jobView 供继续/放弃，直到用户处理。 */
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
        setViewingReport(true);
        setWorkspaceTab('reports');
        setTableTab('rounds');
        // 新报告是最新一份：清空搜索并刷新列表，返回列表时立即可见。
        setReportSearch('');
        setReportQuery('');
        setReportPage(1);
        setReportListVersion((current) => current + 1);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : '回测报告读取失败。');
      }
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
      toast.success(`已把回测配置应用到模拟盘账户「${current.name}」，下一根 K 线起生效。`);
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

  /** 放弃在途作业：仅清空前端在途状态并记入 sessionStorage（刷新后不再重新弹出），服务端作业本身不受影响。 */
  const dismissJob = () => {
    stopPolling();
    const id = jobIdRef.current || jobView?.id;
    if (id) {
      settledJobIdsRef.current.add(id);
      rememberDismissedJob(id);
    }
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
    setWorkspaceTab('reports');
    setViewingReport(true);
    setReportLoading(true);
    setError('');
    try { setReport(await api.backtest(id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '报告读取失败。'); }
    finally { setReportLoading(false); }
  };

  /** 删除报告：二次确认后调用 DELETE，成功则清理当前详情 / 作业展示并刷新列表。 */
  const performDeleteReport = async () => {
    const target = pendingDelete;
    if (!target) return;
    setDeletingId(target.id);
    setError('');
    try {
      const result = await api.deleteBacktest(target.id);
      if (!result.deleted) setError('后端未找到该报告，可能已被其他会话删除。');
      if (report?.id === target.id) {
        setReport(null);
        setViewingReport(false);
        setTrailData(null);
        setRecordData(null);
      }
      if (jobView?.id === target.id) {
        settledJobIdsRef.current.add(target.id);
        if (jobIdRef.current === target.id) jobIdRef.current = null;
        setJobView(null);
        setRunning(false);
      }
      toast.success('回测报告已删除。');
      setPendingDelete(null);
      setReportListVersion((current) => current + 1);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '删除回测报告失败。');
    } finally {
      setDeletingId(null);
    }
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
  // ETF beta 腿摘要（新报告才有；旧报告 undefined）。
  const betaInfo = (report?.data_info?.beta as {
    enabled?: boolean; symbol?: string; trade_count?: number; pnl?: number;
    entry_date?: string | null; last_exit_date?: string | null; exit_reason?: string | null;
  } | undefined) ?? null;
  // 明细条数：主报告只带 *_total；旧缓存报告回退到本地数组长度。
  const roundsTotal = report?.rounds_total ?? report?.rounds?.length ?? 0;
  const tradesTotal = report?.trades_total ?? report?.trades?.length ?? 0;
  const ordersTotal = report?.orders_total ?? report?.orders?.length ?? 0;
  // 漏斗轮动回测专属：每轮换票轨迹（选中日期/标的/入选依据/进出日期/盈亏）。
  // 主报告已瘦身不含全量 trail，改走 GET /backtests/{id}/rotation-trail 后台分页。
  const isFunnel = String(report?.data_info?.mode) === 'funnel_rotation';
  // 数据血缘只读报告自身快照，避免查看旧报告时混入当前配置表单的数据集选择。
  const reportDailySource = report ? String(report.data_info.daily_source || '—') : '—';
  const reportIntradaySource = report?.data_info.intraday_source ? String(report.data_info.intraday_source) : null;
  const reportIndexSource = report?.data_info.index_source ? String(report.data_info.index_source) : null;
  const reportSynthetic = Boolean(report?.data_info.is_synthetic);
  // 「数据与近似口径提醒」只放数据/口径类说明：委托被拒属于交易执行流水（拒绝仍可在
  // 事件/委托里查到），其中 T+1 可卖数量不足是隔夜单提交时点的瞬时限制，不进这份提醒
  // （个股与 ETF 一致口径）。
  const visibleWarnings = (report?.warnings ?? []).filter(
    (warning) => !String(warning).includes('T+1可卖数量不足'),
  );
  // 每月盈亏：后端 monthly_breakdown 按自然月汇总（旧报告由读取端点按权益曲线补算）。
  const monthly = useMemo<MonthlyPnl[]>(() => metrics?.monthly ?? [], [metrics]);
  const monthlySummary = useMemo(() => {
    const wins = monthly.filter((item) => item.pnl > 0).length;
    const losses = monthly.filter((item) => item.pnl < 0).length;
    const best = monthly.reduce<MonthlyPnl | null>((acc, item) => (!acc || item.pnl > acc.pnl ? item : acc), null);
    const worst = monthly.reduce<MonthlyPnl | null>((acc, item) => (!acc || item.pnl < acc.pnl ? item : acc), null);
    return {
      wins, losses, best, worst,
      winRate: monthly.length ? wins / monthly.length : 0,
      average: monthly.length ? monthly.reduce((sum, item) => sum + item.pnl, 0) / monthly.length : 0,
    };
  }, [monthly]);
  const monthPageCount = Math.max(1, Math.ceil(monthly.length / monthPageSize));
  // 切换每页条数后旧页码可能越界：渲染前收敛到最后一页，避免出现空表。
  const safeMonthPage = Math.min(monthPage, monthPageCount);
  const monthRows = monthly.slice((safeMonthPage - 1) * monthPageSize, safeMonthPage * monthPageSize);
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
  useEffect(() => { setTrailPage(1); setMonthPage(1); }, [report?.id]);

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

  // 成交 / 委托 / 轮次 明细分页：切 tab 或翻页时按需拉取当前 tab 的一页。
  useEffect(() => { setRecordPage(1); }, [report?.id, tableTab]);

  // 成交明细筛选：切 tab / 换报告时清空（筛选栏只服务成交明细 tab）。
  useEffect(() => {
    setSymbolDraft('');
    setTradeFilter(EMPTY_TRADE_FILTERS);
  }, [report?.id, tableTab]);

  /** 提交标的筛选（防抖回调与 Enter 立即提交共用）；值未变化时返回 prev，避免多余请求。 */
  const commitSymbol = (raw: string) => {
    const symbol = raw.trim();
    setTradeFilter((prev) => (prev.symbol === symbol ? prev : { ...prev, symbol }));
    setRecordPage(1);
  };

  // 标的输入防抖 300ms 后提交。
  useEffect(() => {
    const timer = window.setTimeout(() => commitSymbol(symbolDraft), 300);
    return () => window.clearTimeout(timer);
    // commitSymbol 每次渲染重建但只读写稳定 setter，按输入值触发即可。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbolDraft]);

  /** 下拉筛选（用途 / 方向）立即提交并回到第一页。 */
  const updateTradeFilter = (patch: Partial<TradeFilters>) => {
    setTradeFilter((prev) => {
      const next = { ...prev, ...patch };
      const unchanged = next.symbol === prev.symbol && next.purpose === prev.purpose && next.side === prev.side;
      return unchanged ? prev : next;
    });
    setRecordPage(1);
  };

  /** 清空全部筛选（输入框草稿 + 已提交条件）。 */
  const clearTradeFilters = () => {
    setSymbolDraft('');
    setTradeFilter(EMPTY_TRADE_FILTERS);
    setRecordPage(1);
  };

  useEffect(() => {
    if (!report) { setRecordData(null); return; }
    let cancelled = false;
    setRecordLoading(true);
    const filters = tableTab === 'trades' && tradeFilterOn
      ? { symbol: tradeFilter.symbol, purpose: tradeFilter.purpose, side: tradeFilter.side }
      : undefined;
    api.backtestRecords(report.id, tableTab, recordPage, recordPageSize, filters)
      .then((paged) => {
        if (cancelled) return;
        setRecordData(paged);
        // 过滤后总页数可能变小：服务端收敛页码后同步回写，避免停在空页。
        if (paged.page !== recordPage) setRecordPage(paged.page);
      })
      .catch((cause) => {
        if (cancelled) return;
        setRecordData(null);
        setError(cause instanceof Error ? cause.message : '成交明细读取失败。');
      })
      .finally(() => { if (!cancelled) setRecordLoading(false); });
    return () => { cancelled = true; };
  }, [report, tableTab, recordPage, recordPageSize, tradeFilter, tradeFilterOn]);

  // 报告搜索防抖：输入 300ms 后才提交服务端查询，并回到第一页。
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setReportPage(1);
      setReportQuery(reportSearch.trim());
    }, 300);
    return () => window.clearTimeout(timer);
  }, [reportSearch]);

  // 回测报告列表：仅在「回测报告」tab 的列表视图下按需加载；翻页 / 搜索 / 删除后刷新走同一入口。
  useEffect(() => {
    if (workspaceTab !== 'reports' || viewingReport) return;
    let cancelled = false;
    setReportListLoading(true);
    api.backtests({ q: reportQuery || undefined, page: reportPage, pageSize: reportPageSize })
      .then((data) => {
        if (cancelled) return;
        setReportList(data);
        // 删除当前页最后一条时后端会把越界页收敛到上一页，这里同步回写触发重取。
        if (data.page !== reportPage) setReportPage(data.page);
      })
      .catch((cause) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : '回测报告列表读取失败。');
      })
      .finally(() => { if (!cancelled) setReportListLoading(false); });
    return () => { cancelled = true; };
  }, [workspaceTab, viewingReport, reportQuery, reportPage, reportPageSize, reportListVersion]);

  // 当前 tab 的一页明细（旧缓存报告若仍带全量数组则回退用本地切片；成交明细筛选在本地同样生效）。
  const { recordItems, filteredTotal, recordFacets } = useMemo(() => {
    if (recordData) return { recordItems: recordData.items, filteredTotal: recordData.total, recordFacets: recordData.facets };
    const local = tableTab === 'rounds' ? report?.rounds : tableTab === 'trades' ? report?.trades : report?.orders;
    let list: any[] = local ?? [];
    if (tableTab === 'trades' && tradeFilterOn) list = list.filter((item: Trade) => matchesTradeFilters(item, tradeFilter));
    const start = (recordPage - 1) * recordPageSize;
    return { recordItems: list.slice(start, start + recordPageSize), filteredTotal: list.length, recordFacets: undefined };
  }, [recordData, tableTab, report, recordPage, recordPageSize, tradeFilter, tradeFilterOn]);

  // 用途 / 方向下拉选项：带分面条数（facets 缺失时降级为纯标签）；0 条选项禁用，
  // 但当前已选中的选项保持可选，避免过滤后自身值被禁用。
  // facets 存在时，缺省的分面键视为 0 条（该组合下没有数据），展示为「(0)」并禁用。
  const purposeCounts = recordFacets?.purpose;
  const sideCounts = recordFacets?.side;
  const purposeFilterOptions = useMemo(() => PURPOSE_FILTER_OPTIONS.map((value) => {
    const count = purposeCounts?.[value] ?? 0;
    return {
      value,
      label: purposeCounts ? `${purposeLabel(value)} (${count})` : purposeLabel(value),
      disabled: Boolean(purposeCounts) && count === 0 && tradeFilter.purpose !== value,
    };
  }), [purposeCounts, tradeFilter.purpose]);
  const sideFilterOptions = useMemo(() => SIDE_FILTER_OPTIONS.map((value) => {
    const count = sideCounts?.[value] ?? 0;
    return {
      value,
      label: sideCounts ? `${sideLabel(value)} (${count})` : sideLabel(value),
      disabled: Boolean(sideCounts) && count === 0 && tradeFilter.side !== value,
    };
  }), [sideCounts, tradeFilter.side]);

  // 明细分页总数：筛选时跟随过滤后条数，否则用报告自带 *_total。
  const recordsTotal = tableTab === 'rounds' ? roundsTotal : tableTab === 'trades' ? tradesTotal : ordersTotal;
  const paginationTotal = tableTab === 'trades' && tradeFilterOn ? filteredTotal : recordsTotal;
  const paginationTotalPages = recordData?.total_pages ?? Math.max(1, Math.ceil(paginationTotal / recordPageSize));

  return (
    <div className="animate-[page-in_.22s_ease-out]">
      <PageHeader
        eyebrow="RESEARCH / BACKTEST"
        title="回测工作台"
        description="运行新的策略实验，或检索、复查与清理历史回测报告。"
      />

      {error && <ErrorBanner message={error} onRetry={!report && !loading ? () => void loadInitial() : undefined} onClose={() => setError('')} />}

      <Tabs value={workspaceTab} onValueChange={(value) => setWorkspaceTab(value as WorkspaceTab)}>
        <TabsList className="mb-3">
          <TabsTrigger value="run" className="gap-1.5 px-3"><Play className="size-3.5" />运行回测{jobActive && <span className="size-1.5 rounded-full bg-primary" />}</TabsTrigger>
          <TabsTrigger value="reports" className="gap-1.5 px-3"><BarChart3 className="size-3.5" />回测报告{reportList && <span className="rounded bg-muted px-1 font-mono text-[10px] text-muted-foreground">{reportList.total}</span>}</TabsTrigger>
        </TabsList>

        {workspaceTab === 'run' && (
          <Panel className="mb-3" flush>
            <form onSubmit={(event) => { event.preventDefault(); void run(); }} noValidate className="p-4">
              <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex items-center gap-2.5">
                  <span className="grid size-7 place-items-center rounded-md bg-accent font-mono text-[11px] text-accent-foreground">01</span>
                  <div><h2 className="text-[15px] font-semibold">实验配置</h2><p className="text-[12px] text-muted-foreground">定义数据边界与交易摩擦，再启动本地回测。</p></div>
                </div>
                <div className="flex gap-1 rounded-lg bg-muted p-0.5">
                  <button type="button" className={cn('inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[12px] transition-colors', mode === 'funnel' ? 'bg-card text-primary shadow-xs' : 'text-muted-foreground')} onClick={() => setMode('funnel')}><Layers3 className="size-3.5" />漏斗轮动回测</button>
                  <button type="button" className={cn('inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[12px] transition-colors', mode === 'single' ? 'bg-card text-primary shadow-xs' : 'text-muted-foreground')} onClick={() => setMode('single')}><BarChart3 className="size-3.5" />单标的回测</button>
                </div>
              </div>

              <div className="grid gap-2">
                <SwitchRow label="启用做 T" description="可选，不占用 16 笔序列" checked={config.enable_t} onCheckedChange={(checked) => updateConfig('enable_t', checked)} />

                <FormGrid>
                  {config.enable_t && (
                    <>
                      <Field label="做 T 净利止盈（%）" hint="扣完成本后净赚该幅度即平 T">
                        <UnitInput unit="%" min={0} max={100} step={0.1} value={Number(((config.t_target_net_spread ?? 0.03) * 100).toFixed(2))} onChange={(value) => updateConfig('t_target_net_spread', Number(value) / 100)} />
                      </Field>
                      <Field label="做 T T+1 截止时间" hint="到点仍未止盈则尾盘强平">
                        <Input type="time" step={300} value={config.t_exit_cutoff ?? '14:50'} onChange={(event) => updateConfig('t_exit_cutoff', event.target.value)} />
                      </Field>
                    </>
                  )}
                  <Field label="超期降阈天数" hint="持有 ≥N 个交易日后止盈降阈；留空=关闭">
                    <UnitInput unit="交易日" min={1} max={250} step={1} value={config.max_holding_days ?? ''} onChange={(value) => updateConfig('max_holding_days', value === '' ? null : Number(value))} />
                  </Field>
                  <Field label="降阈后止盈（%）" hint="超期后价格轨/账户轨止盈阈值">
                    <UnitInput unit="%" min={0} max={100} step={0.1} value={config.holding_tp_override != null ? Number((config.holding_tp_override * 100).toFixed(2)) : ''} onChange={(value) => updateConfig('holding_tp_override', value === '' ? null : Number(value) / 100)} />
                  </Field>
                </FormGrid>

                {mode === 'funnel' && (
                  <div className="rounded-xl border bg-muted/25 p-3">
                    <SwitchRow label="指数 beta 腿（创业板 ETF）" description="个股空仓时持有 ETF；选到个股后卖出换股，不占 16 笔加仓序列" checked={Boolean(config.etf_beta_enabled)} onCheckedChange={(checked) => updateConfig('etf_beta_enabled', checked)} />
                    {config.etf_beta_enabled && (
                      <FormGrid className="mt-2">
                        <Field label="ETF 代码" hint="默认创业板 ETF 159915">
                          <SelectField value={config.etf_symbol ?? 'sz.159915'} onValueChange={(value) => updateConfig('etf_symbol', value)} options={[
                            { value: 'sz.159915', label: 'sz.159915 · 创业板ETF易方达' },
                            { value: 'sz.159952', label: 'sz.159952 · 创业板ETF广发' },
                            { value: 'sz.159964', label: 'sz.159964 · 创业板ETF平安' },
                          ]} />
                        </Field>
                        <Field label="仓位（占权益 %）" hint="空仓时买入的 ETF 市值占账户权益比例">
                          <UnitInput unit="%" min={1} max={100} step={1} value={Number(((config.etf_position_pct ?? 0.2) * 100).toFixed(0))} onChange={(value) => updateConfig('etf_position_pct', Number(value) / 100)} />
                        </Field>
                        <Field label="入场均线（日）" hint="创业板指收盘站上该均线才持 ETF"><UnitInput unit="日" min={2} max={250} step={1} value={config.etf_entry_ma_days ?? 60} onChange={(value) => updateConfig('etf_entry_ma_days', Number(value))} /></Field>
                        <Field label="出场均线（日）" hint="创业板指收盘跌破该均线清 ETF"><UnitInput unit="日" min={2} max={250} step={1} value={config.etf_exit_ma_days ?? 20} onChange={(value) => updateConfig('etf_exit_ma_days', Number(value))} /></Field>
                        <Field label="ETF 硬止损（%）" hint="ETF 价格较建仓成本回撤达到该幅度立即清仓"><UnitInput unit="%" min={0.1} max={99} step={0.1} value={Number(((config.etf_hard_stop_pct ?? 0.06) * 100).toFixed(1))} onChange={(value) => updateConfig('etf_hard_stop_pct', Number(value) / 100)} /></Field>
                        <Field label="不追高上限（偏离均线 %）" hint="指数收盘高于入场均线超过该幅度时不建仓，等回踩再买；0 关闭"><UnitInput unit="%" min={0} max={50} step={0.5} value={Number(((config.etf_entry_max_deviation ?? 0) * 100).toFixed(1))} onChange={(value) => updateConfig('etf_entry_max_deviation', Number(value) / 100)} /></Field>
                        <Field label="清仓后冷却（交易日）" hint="ETF 清仓后至少 N 个交易日内不再建仓，抑制均线附近反复打脸；0 关闭"><UnitInput unit="日" min={0} max={60} step={1} value={config.etf_reentry_cooldown_days ?? 0} onChange={(value) => updateConfig('etf_reentry_cooldown_days', Number(value))} /></Field>
                      </FormGrid>
                    )}
                  </div>
                )}

                {mode === 'funnel' && (
                  <div className="flex items-start gap-2 rounded-lg border border-primary/15 bg-accent/40 px-3 py-2 text-[11px] leading-5 text-muted-foreground">
                    <Layers3 className="mt-0.5 size-3.5 shrink-0 text-primary" />
                    <span>漏斗轮动回测：<strong className="text-foreground">不再锁定单一标的</strong>，每次清仓后按下方选股漏斗（L0→L4）重跑、取当时第一名换票；行情取自本地 universe.db 缓存（双创日线），为日线口径。</span>
                  </div>
                )}

                <FormGrid>
                  <Field label="实验名称" wide>
                    <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="输入回测名称" />
                  </Field>
                  {mode === 'single' && (
                    <Field label="主数据集" hint="quan_parquet / baostock 日线" wide>
                      <SelectField
                        value={datasetId}
                        onValueChange={(value) => {
                          const next = datasets.find((item) => item.id === value);
                          setDatasetId(value);
                          if (next) {
                            setStartDate(next.start?.slice(0, 10) || '');
                            setEndDate(next.end?.slice(0, 10) || '');
                            const minute = datasets.find((item) => item.timeframe === '5m' && item.symbol === next.symbol);
                            if (minute) setIntradayId(minute.id);
                          }
                        }}
                        options={datasets.filter((item) => item.timeframe === 'daily').map((item) => ({ value: item.id, label: `${item.name} · ${displaySymbol(item.symbol)} · ${item.source}` }))}
                        placeholder="请选择数据集"
                        ariaLabel="主数据集"
                      />
                    </Field>
                  )}
                  <Field label="开始日期">
                    <div className="relative"><CalendarDays className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" /><Input type="date" value={startDate} min={mode === 'single' ? selectedDataset?.start?.slice(0, 10) : undefined} max={endDate || (mode === 'single' ? selectedDataset?.end?.slice(0, 10) : undefined)} onChange={(event) => setStartDate(event.target.value)} className="pl-8" /></div>
                  </Field>
                  <Field label="结束日期">
                    <div className="relative"><CalendarDays className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" /><Input type="date" value={endDate} min={startDate || (mode === 'single' ? selectedDataset?.start?.slice(0, 10) : undefined)} max={mode === 'single' ? selectedDataset?.end?.slice(0, 10) : undefined} onChange={(event) => setEndDate(event.target.value)} className="pl-8" /></div>
                  </Field>
                  <Field label="初始资金">
                    <UnitInput unit="CNY" min={1} step={10000} value={initialCash} onChange={(value) => { const parsed = Number(value); setInitialCash(Number.isFinite(parsed) ? parsed : 0); }} />
                  </Field>
                  {mode === 'single' && (
                    <>
                      <Field label="5 分钟数据集" hint="腾讯 / quan_parquet 5 分钟">
                        <SelectField value={intradayId || '__none__'} onValueChange={(value) => setIntradayId(value === '__none__' ? '' : value)} options={[{ value: '__none__', label: '不使用（日线降级）' }, ...intradayOptions.map((item) => ({ value: item.id, label: `${item.name} · ${item.timeframe} · ${item.source}` }))]} ariaLabel="5 分钟数据集" />
                      </Field>
                      <Field label="指数数据集" hint="大盘门（日线）">
                        <SelectField value={indexId || '__none__'} onValueChange={(value) => setIndexId(value === '__none__' ? '' : value)} options={[{ value: '__none__', label: '不使用' }, ...indexOptions.map((item) => ({ value: item.id, label: `${item.name} · ${displaySymbol(item.symbol)}` }))]} ariaLabel="指数数据集" />
                      </Field>
                    </>
                  )}
                </FormGrid>

                {mode === 'funnel' && <ScreenerConfigPanel value={screener} onChange={setScreener} fundamentalLockedOff />}

                <button type="button" className="flex items-center justify-between rounded-lg border bg-card px-3 py-2 text-left text-[12px] font-medium text-foreground/80 hover:bg-muted/40" onClick={() => setAdvanced(!advanced)} aria-expanded={advanced}>
                  <span className="inline-flex items-center gap-2"><Gauge className="size-3.5" />交易摩擦与撮合参数</span>{advanced ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
                </button>
                {advanced && (
                  <div className="grid gap-2">
                    <FeeConfigPanel value={fees} onChange={setFees} />
                    <FormGrid>
                      <Field label="滑点"><UnitInput unit="bps" min={0} step={1} value={config.slippage_bps} onChange={(value) => updateConfig('slippage_bps', Number(value))} /></Field>
                      <Field label="成交量参与上限"><UnitInput unit="%" min={0} max={100} step={1} value={Number((config.volume_participation * 100).toFixed(0))} onChange={(value) => updateConfig('volume_participation', Number(value) / 100)} /></Field>
                    </FormGrid>
                  </div>
                )}
              </div>

              <div className="mt-3 flex flex-col gap-2 border-t pt-3 lg:flex-row lg:items-center lg:justify-between">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
                  {mode === 'funnel' ? (
                    <span>漏斗轮动 · {screener.boards.join(' / ')} · 每次清仓后重跑 L0→L4 取第一名换票（日线口径）</span>
                  ) : selectedDataset ? (
                    <><span>{selectedDataset.rows.toLocaleString('zh-CN')} 行</span><i className="size-1 rounded-full bg-border" /><span>{selectedDataset.timeframe} 粒度</span><i className="size-1 rounded-full bg-border" /><span>{shortDate(selectedDataset.start)} — {shortDate(selectedDataset.end)}</span>{selectedDataset.is_synthetic && <SyntheticNotice compact />}</>
                  ) : <span>请选择主数据集以查看数据边界</span>}
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button variant="outline" size="sm" className="h-8" type="button" onClick={() => void applyToPaper()} disabled={applying || jobActive}>
                    {applying ? <><Loader2 className="animate-spin" />正在应用</> : <><ArrowDownToLine />应用到模拟盘</>}
                  </Button>
                  <Button size="sm" className="h-8" type="submit" disabled={jobActive || (mode === 'single' && !datasetId)}>
                    {jobActive ? <><Loader2 className="animate-spin" />正在运行回测</> : <><Play />运行回测</>}
                  </Button>
                </div>
              </div>

              {jobView && (
                <div className="mt-3 rounded-xl border bg-muted/30 p-3" role="status">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-[12px] font-semibold">{jobView.status === 'RUNNING' ? '回测进行中' : `回测${statusLabel(jobView.status)}`} · {jobView.name || '后台回测'}</span>
                    <div className="flex items-center gap-1.5">
                      {jobView.status === 'RUNNING' && <Button variant="outline" size="sm" onClick={() => void cancelJob()} disabled={jobBusy}><Square className="size-3.5" />停止</Button>}
                      {JOB_RESUMABLE_STATUSES.includes(jobView.status) && <><Button variant="outline" size="sm" onClick={() => void resumeJob()} disabled={jobBusy}><Play />继续</Button><Button variant="ghost" size="sm" onClick={dismissJob} disabled={jobBusy}>放弃</Button></>}
                    </div>
                  </div>
                  <Progress value={Math.max(0, Math.min(100, jobView.progress))} className="mt-2" />
                  <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[10px] text-muted-foreground">
                    <span>{jobView.progress.toFixed(1)}%</span>
                    <span>{jobView.cursor_date ? `已跑至 ${jobView.cursor_date}` : '等待首个交易日完成'}</span>
                    {jobView.total_days ? <span>共 {integer(jobView.total_days)} 交易日</span> : null}
                    {jobView.error ? <span className="text-destructive">{jobView.error}</span> : null}
                  </div>
                </div>
              )}
            </form>
          </Panel>
        )}

        {workspaceTab === 'reports' && (
          viewingReport ? (
            reportLoading ? <Panel><Loading label="正在读取回测报告" /></Panel> : report && metrics ? (
              <div>
                <div className="mb-2 flex items-center justify-between gap-2">
                  <Button variant="outline" size="sm" type="button" onClick={() => setViewingReport(false)}><ArrowLeft />返回报告列表</Button>
                </div>
                <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
                  <div>
                    <div className="mb-1 flex items-center gap-2 font-mono text-[10px] tracking-wide text-muted-foreground"><Badge tone="indigo">{statusLabel(report.status)}</Badge><span>REPORT / {report.id.slice(0, 8).toUpperCase()}</span></div>
                    <h2 className="text-xl font-semibold tracking-tight">{report.name}</h2>
                    <p className="mt-1 text-[12px] text-muted-foreground">生成于 {dateTime(report.created_at)} · 初始资金 {money(metrics.initial_cash, 0)}</p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button variant="outline" size="sm" onClick={() => void exportCsv('trades')} disabled={exporting !== null}><ArrowDownToLine />{exporting === 'trades' ? '导出中' : '导出成交'}</Button>
                    <Button variant="outline" size="sm" onClick={() => void exportCsv('orders')} disabled={exporting !== null}><ArrowDownToLine />{exporting === 'orders' ? '导出中' : '导出委托'}</Button>
                    <Button variant="destructive" size="sm" onClick={() => setPendingDelete({ id: report.id, name: report.name })} disabled={deletingId === report.id}><Trash2 />{deletingId === report.id ? '删除中' : '删除报告'}</Button>
                  </div>
                </div>

                <MetricGrid columns={6}>
                  <Metric label="总收益率" value={signedPercent(metrics.total_return)} tone={metrics.total_return >= 0 ? 'positive' : 'negative'} sub={`期末权益 ${money(metrics.final_equity)}`} />
                  <Metric label="年化收益率" value={signedPercent(metrics.annualized_return)} tone={metrics.annualized_return >= 0 ? 'positive' : 'negative'} sub={`买入持有 ${signedPercent(metrics.buy_hold_return)}`} />
                  <Metric label="最大回撤" value={percent(Math.abs(metrics.max_drawdown))} tone="negative" sub={`Calmar ${number(metrics.calmar_ratio)}`} />
                  <Metric label="夏普比率" value={number(metrics.sharpe_ratio)} sub={`波动率 ${percent(metrics.volatility)}`} />
                  <Metric label="轮次胜率" value={percent(metrics.win_rate)} sub={`${metrics.closed_rounds} / ${metrics.total_rounds} 轮已结束`} />
                  <Metric label="净利润" value={signedMoney(metrics.net_profit)} tone={metrics.net_profit >= 0 ? 'positive' : 'negative'} sub={`${integer(metrics.total_trades)} 笔成交 · PF ${number(metrics.profit_factor)}`} />
                </MetricGrid>

                <div className="mb-3 grid gap-3 xl:grid-cols-[minmax(0,1fr)_300px]">
                  <Panel title="权益与风险轨迹" description="账户权益、实际投入与回撤随时间的变化" action={<div className="flex items-center gap-2 text-[10px] text-muted-foreground"><span className="inline-flex items-center gap-1"><i className="size-1.5 rounded-full bg-primary" />账户权益</span><span className="inline-flex items-center gap-1"><i className="size-1.5 rounded-full bg-muted-foreground" />实际投入</span><span className="inline-flex items-center gap-1"><i className="size-1.5 rounded-full bg-success" />回撤</span></div>}>
                    <div className="h-[320px] w-full">
                      {report.equity_curve.length > 0 ? (
                        <ResponsiveContainer width="100%" height="100%">
                          <ComposedChart data={report.equity_curve} margin={{ top: 12, right: 4, left: 4, bottom: 0 }}>
                            <defs><linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#5046d8" stopOpacity={0.14} /><stop offset="100%" stopColor="#5046d8" stopOpacity={0} /></linearGradient></defs>
                            <CartesianGrid stroke="#ececf1" strokeDasharray="3 4" vertical={false} />
                            <XAxis dataKey="date" tickFormatter={(value: string) => value.slice(5, 10)} tick={{ fill: '#85858f', fontSize: 11 }} axisLine={false} tickLine={false} minTickGap={48} dy={8} />
                            <YAxis yAxisId="money" tickFormatter={(value: number) => `${(value / 10000).toFixed(0)}万`} tick={{ fill: '#85858f', fontSize: 11 }} axisLine={false} tickLine={false} width={52} domain={['auto', 'auto']} />
                            <YAxis yAxisId="drawdown" orientation="right" tickFormatter={(value: number) => `${(value * 100).toFixed(0)}%`} tick={{ fill: '#9a9aa3', fontSize: 11 }} axisLine={false} tickLine={false} width={42} domain={['auto', 0]} />
                            <Tooltip content={({ active, payload, label }) => active && payload?.length ? <div className="rounded-lg border bg-popover p-3 text-xs shadow-md"><strong className="block text-foreground">{label}</strong>{[['账户权益', money(payload[0].payload.equity)], ['实际投入', money(payload[0].payload.invested)], ['可用现金', money(payload[0].payload.cash)], ['持仓市值', money(payload[0].payload.market_value)], ['回撤', percent(payload[0].payload.drawdown)], ['持仓数量', integer(payload[0].payload.position_qty)]].map(([k, v]) => <div key={String(k)} className="mt-1 flex items-center justify-between gap-4"><span className="text-muted-foreground">{k}</span><b className="font-mono text-foreground">{v}</b></div>)}</div> : null} />
                            <Area yAxisId="money" type="monotone" dataKey="equity" stroke="#5046d8" strokeWidth={2.2} fill="url(#equityFill)" activeDot={{ r: 4, fill: '#5046d8', stroke: '#fff', strokeWidth: 2 }} />
                            <Line yAxisId="money" type="monotone" dataKey="invested" stroke="#8b8b98" strokeWidth={1.5} dot={false} strokeDasharray="5 4" />
                            <Line yAxisId="drawdown" type="stepAfter" dataKey="drawdown" stroke="#138064" strokeWidth={1.4} dot={false} />
                          </ComposedChart>
                        </ResponsiveContainer>
                      ) : <EmptyState compact title="暂无权益曲线" description="报告未返回可绘制的时序数据。" />}
                    </div>
                    <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[10px] text-muted-foreground"><span>左轴：资金（万元） · 右轴：回撤（%）</span><span>期末 {report.equity_curve[report.equity_curve.length - 1]?.date || '—'}</span></div>
                  </Panel>

                  <Panel title="本轮风险诊断" description="从资金占用到尾部风险的横向检查" action={<ShieldAlert className="size-4" />}>
                    <div className="grid gap-1.5">
                      {[
                        { icon: WalletCards, label: '最大资金投入', value: money(metrics.max_invested), note: '峰值占用' },
                        { icon: TrendingDown, label: '最大浮亏', value: money(metrics.max_float_loss), note: '未实现口径', tone: 'text-success' },
                        { icon: RotateCcw, label: '做 T 净贡献', value: signedMoney(metrics.t_net_profit), note: `${integer(metrics.t_trade_count)} 笔`, tone: metrics.t_net_profit >= 0 ? 'text-destructive' : 'text-success' },
                        ...(betaInfo?.enabled ? [{ icon: Layers3, label: 'ETF beta 净贡献', value: signedMoney(Number(betaInfo.pnl ?? 0)), note: `${integer(Number(betaInfo.trade_count ?? 0))} 笔 · ${betaInfo.exit_reason || '持仓中'}`, tone: Number(betaInfo.pnl ?? 0) >= 0 ? 'text-destructive' : 'text-success' }] : []),
                        { icon: CircleDollarSign, label: '累计交易费用', value: money(metrics.total_fees), note: '五項分项相加，滑点另计' },
                        { icon: Activity, label: '平均资金敞口', value: percent(metrics.exposure), note: '权益占比' },
                      ].map(({ icon: Icon, label, value, note, tone }) => (
                        <div key={label} className="flex items-center gap-2 rounded-lg border bg-card px-2.5 py-2">
                          <span className="grid size-7 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground"><Icon className="size-3.5" /></span>
                          <div className="min-w-0 flex-1"><small className="block text-[10px] text-muted-foreground">{label}</small><strong className={cn('block font-mono text-[13px]', tone)}>{value}</strong></div>
                          <span className="text-right text-[10px] text-muted-foreground">{note}</span>
                        </div>
                      ))}
                    </div>
                    <div className="mt-2 flex items-start gap-1.5 rounded-lg bg-muted/60 px-2.5 py-2 text-[10px] leading-4 text-muted-foreground"><Gauge className="mt-0.5 size-3 shrink-0" /><span>风险表现需结合数据粒度、成交假设与样本区间共同判断。</span></div>
                  </Panel>
                </div>

                {monthly.length > 0 && (
                  <Panel className="mb-3" title="每月盈亏" description={`按自然月汇总账户权益变动（含未实现盈亏与手续费）· 共 ${monthly.length} 个月 · 红涨绿跌`} action={<CalendarDays className="size-4" />}>
                    <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg bg-muted/40 px-2.5 py-1.5 text-[11px] text-muted-foreground">
                      <span>盈利月 <strong className="font-mono text-destructive">{monthlySummary.wins}</strong></span>
                      <span>亏损月 <strong className="font-mono text-success">{monthlySummary.losses}</strong></span>
                      <span>月胜率 <strong className="font-mono text-foreground">{percent(monthlySummary.winRate)}</strong></span>
                      <span>月均盈亏 <strong className={cn('font-mono', monthlySummary.average >= 0 ? 'text-destructive' : 'text-success')}>{signedMoney(monthlySummary.average)}</strong></span>
                      <span>最佳月 <strong className="font-mono text-destructive">{monthlySummary.best?.month || '—'} {monthlySummary.best ? signedMoney(monthlySummary.best.pnl) : ''}</strong></span>
                      <span>最差月 <strong className="font-mono text-success">{monthlySummary.worst?.month || '—'} {monthlySummary.worst ? signedMoney(monthlySummary.worst.pnl) : ''}</strong></span>
                    </div>
                    <div className="h-[220px] w-full">
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart data={monthly} margin={{ top: 8, right: 4, left: 4, bottom: 0 }}>
                          <CartesianGrid stroke="#ececf1" strokeDasharray="3 4" vertical={false} />
                          <XAxis dataKey="month" tickFormatter={shortMonth} tick={{ fill: '#85858f', fontSize: 10 }} axisLine={false} tickLine={false} minTickGap={28} dy={6} />
                          <YAxis tickFormatter={(value: number) => `${(value / 10000).toFixed(0)}万`} tick={{ fill: '#85858f', fontSize: 11 }} axisLine={false} tickLine={false} width={52} domain={['auto', 'auto']} />
                          <ReferenceLine y={0} stroke="#c9c9d2" strokeWidth={1} />
                          <Tooltip cursor={{ fill: '#f4f4f7' }} content={({ active, payload }) => {
                            const item = active && payload?.length ? (payload[0].payload as MonthlyPnl) : null;
                            return item ? <div className="rounded-lg border bg-popover p-3 text-xs shadow-md"><strong className="block text-foreground">{item.month}</strong>{[['净盈亏', signedMoney(item.pnl)], ['收益率', signedPercent(item.return_rate)], ['基期权益', money(item.start_equity, 0)], ['月末权益', money(item.end_equity, 0)], ['交易日', `${integer(item.trading_days)} 天`], ['当月最大回撤', drawdownLabel(item.max_drawdown)], ['累计收益率', signedPercent(item.cumulative_return)]].map(([k, v]) => <div key={String(k)} className="mt-1 flex items-center justify-between gap-4"><span className="text-muted-foreground">{k}</span><b className="font-mono text-foreground">{v}</b></div>)}</div> : null;
                          }} />
                          <Bar dataKey="pnl" maxBarSize={28} radius={[2, 2, 0, 0]}>
                            {monthly.map((item) => <Cell key={item.month} fill={item.pnl >= 0 ? '#cc454c' : '#138064'} />)}
                          </Bar>
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center justify-between gap-2 text-[10px] text-muted-foreground"><span>柱高＝当月净盈亏（元），向上盈利 / 向下亏损</span><span>月度盈亏基于每月最后一个交易日权益，不含外部资金进出</span></div>
                    <Table className="mt-2">
                      <TableHeader><TableRow><TableHead>月份</TableHead><TableHead className="text-right">净盈亏</TableHead><TableHead className="text-right">收益率</TableHead><TableHead className="text-right">交易日</TableHead><TableHead className="text-right">当月最大回撤</TableHead><TableHead className="text-right">累计收益率</TableHead></TableRow></TableHeader>
                      <TableBody>
                        {monthRows.map((item) => (
                          <TableRow key={item.month}>
                            <TableCell><strong className="font-mono text-[12px]">{item.month}</strong></TableCell>
                            <TableCell className={cn('text-right font-mono text-[12px]', item.pnl >= 0 ? 'text-destructive' : 'text-success')}>{signedMoney(item.pnl)}</TableCell>
                            <TableCell className={cn('text-right font-mono text-[12px]', item.return_rate >= 0 ? 'text-destructive' : 'text-success')}>{signedPercent(item.return_rate)}</TableCell>
                            <TableCell className="text-right font-mono text-[12px]">{integer(item.trading_days)}</TableCell>
                            <TableCell className="text-right font-mono text-[12px] text-muted-foreground">{drawdownLabel(item.max_drawdown)}</TableCell>
                            <TableCell className={cn('text-right font-mono text-[12px]', item.cumulative_return >= 0 ? 'text-destructive' : 'text-success')}>{signedPercent(item.cumulative_return)}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                    <div className="pt-3"><PaginationBar page={safeMonthPage} pageSize={monthPageSize} total={monthly.length} totalPages={monthPageCount} onPageChange={setMonthPage} onPageSizeChange={(size) => { setMonthPageSize(size); setMonthPage(1); }} pageSizeOptions={[12, 24, 36, 60]} /></div>
                  </Panel>
                )}

                <Panel className="mb-3" title="数据血缘与口径" description="本报告基于后端实际返回的数据元信息" action={<Layers3 className="size-4" />}>
                  <div className="mb-2 flex flex-wrap gap-1.5"><Badge>{reportDailySource} 日线</Badge>{reportIntradaySource && <Badge tone="indigo">{reportIntradaySource} 5分钟</Badge>}{reportIndexSource && <Badge tone="green">指数大盘门</Badge>}{reportSynthetic && <SyntheticNotice compact />}</div>
                  {Object.keys(report.data_info || {}).length > 0 && <div className="grid gap-1.5 sm:grid-cols-2 xl:grid-cols-3">{Object.entries(report.data_info).map(([key, value]) => <div key={key} className="flex items-center justify-between gap-3 rounded-md bg-muted/40 px-2 py-1 text-[10px]"><span className="text-muted-foreground">{key}</span><strong className="truncate font-mono text-foreground" title={typeof value === 'object' ? JSON.stringify(value) : String(value)}>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</strong></div>)}</div>}
                  {visibleWarnings.length > 0 ? (
                    <Alert className="mt-2 border-warning/25 bg-warning/5 text-warning">
                      <AlertTriangle />
                      <AlertTitle>数据与近似口径提醒</AlertTitle>
                      <AlertDescription className="text-warning/80"><ul className="list-disc space-y-0.5 pl-4">{visibleWarnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></AlertDescription>
                    </Alert>
                  ) : <div className="mt-2 flex items-center gap-1.5 text-[10px] text-muted-foreground"><FlaskConical className="size-3.5" />后端未返回额外警告；仍请留意日线无法还原精确 tick 成交路径。</div>}
                </Panel>

                {isFunnel && trailData && trailData.total > 0 && (
                  <Panel className="mb-3" title="换票轨迹" description={`每次清仓后重跑选股漏斗（L0→L4）取当时第一名；WAITING 为 L0 关门或无候选的空仓等待 · 共 ${trailData.total} 次筛选`} action={<Layers3 className="size-4" />} flush>
                    <Table>
                      <TableHeader><TableRow><TableHead className="w-8" /><TableHead>轮次</TableHead><TableHead>选股日</TableHead><TableHead>标的</TableHead><TableHead>名称</TableHead><TableHead className="text-right">入选 Chg20D</TableHead><TableHead>L0 门</TableHead><TableHead className="text-right">入场</TableHead><TableHead className="text-right">出场</TableHead><TableHead className="text-right">净盈亏</TableHead><TableHead>状态</TableHead></TableRow></TableHeader>
                      <TableBody>
                        {rotationTrail.map((item, index) => {
                          const trailKey = item.round || `${item.select_date}-${index}`;
                          const expanded = expandedTrails.has(trailKey);
                          return (
                            <Fragment key={trailKey}>
                              <TableRow className={item.status === 'WAITING' ? 'bg-muted/25' : ''}>
                                <TableCell><button className="grid size-6 place-items-center rounded text-muted-foreground hover:bg-muted" onClick={() => toggleTrail(trailKey)} aria-label={expanded ? '收起本轮决策日志' : '展开本轮决策日志'} title={expanded ? '收起决策日志' : '查看本轮决策日志'}>{expanded ? <ChevronUp className="size-3.5" /> : <ChevronDown className="size-3.5" />}</button></TableCell>
                                <TableCell><strong className="font-mono text-[11px] text-primary">{item.round || `#${(trailPage - 1) * trailPageSize + index + 1}`}</strong></TableCell>
                                <TableCell>{shortDate(item.select_date)}</TableCell>
                                <TableCell>{item.symbol ? <strong className="font-mono text-[12px]" title={item.symbol}>{displaySymbol(item.symbol)}</strong> : <span className="text-muted-foreground">空仓等待</span>}</TableCell>
                                <TableCell>{item.name || '—'}</TableCell>
                                <TableCell className={cn('text-right font-mono text-[12px]', item.chg20d_at_select != null && (item.chg20d_at_select >= 0 ? 'text-destructive' : 'text-success'))}>{item.chg20d_at_select != null ? signedPercent(item.chg20d_at_select) : '—'}</TableCell>
                                <TableCell><Badge tone={item.gate_open ? 'green' : 'amber'}>{item.gate_open ? '开门' : '关门'}</Badge></TableCell>
                                <TableCell className="text-right">{item.entry_date ? shortDate(item.entry_date) : '—'}</TableCell>
                                <TableCell className="text-right">{item.exit_date ? shortDate(item.exit_date) : '—'}</TableCell>
                                <TableCell className={cn('text-right font-mono text-[12px]', item.pnl != null && (item.pnl >= 0 ? 'text-destructive' : 'text-success'))}>{item.pnl != null ? signedMoney(item.pnl) : '—'}</TableCell>
                                <TableCell><Badge tone={item.status === 'CLOSED' ? 'green' : item.status === 'OPEN' ? 'indigo' : 'amber'}>{item.status === 'CLOSED' ? '已结束' : item.status === 'OPEN' ? '持仓中' : '空仓等待'}</Badge></TableCell>
                              </TableRow>
                              {expanded && (
                                <TableRow className="bg-muted/20">
                                  <TableCell />
                                  <TableCell colSpan={10}>
                                    <div className="py-1">
                                      <div className="flex flex-wrap gap-1.5">
                                        {[{ tag: '全市场', value: item.funnel?.universe }, { tag: 'L0 大盘门', value: item.funnel?.after_l0 }, { tag: 'L1 超跌', value: item.funnel?.after_l1 }, { tag: 'L2 否决', value: item.funnel?.after_l2 }, { tag: 'L3 容量', value: item.funnel?.after_l3 }, { tag: '可用候选', value: item.funnel?.eligible }].map((stage) => (
                                          <span key={stage.tag} className="rounded-md border bg-card px-2 py-1 text-[10px]"><small className="mr-1 text-muted-foreground">{stage.tag}</small><strong className="font-mono">{stage.value ?? '—'}</strong></span>
                                        ))}
                                      </div>
                                      {item.screen_warnings && item.screen_warnings.length > 0 ? <ul className="mt-1.5 list-disc space-y-0.5 pl-4 text-[10px] text-muted-foreground">{item.screen_warnings.map((warning, wIndex) => <li key={wIndex}>{warning}</li>)}</ul> : <p className="mt-1.5 text-[10px] text-muted-foreground">本轮选股无逐轮决策提示。</p>}
                                    </div>
                                  </TableCell>
                                </TableRow>
                              )}
                            </Fragment>
                          );
                        })}
                      </TableBody>
                    </Table>
                    <div className="px-4 pb-4"><PaginationBar page={trailData.page} pageSize={trailData.page_size} total={trailData.total} totalPages={trailData.total_pages} onPageChange={setTrailPage} onPageSizeChange={(size) => { setTrailPageSize(size); setTrailPage(1); }} disabled={trailLoading} /></div>
                  </Panel>
                )}

                <Panel className="mb-3" flush>
                  <div className="flex flex-col gap-2 border-b px-4 py-2.5 sm:flex-row sm:items-center sm:justify-between">
                    <Tabs value={tableTab} onValueChange={(value) => setTableTab(value as 'rounds' | 'trades' | 'orders')}>
                      <TabsList>
                        <TabsTrigger value="rounds">已结束轮次<span className="ml-1 font-mono text-[10px] text-muted-foreground">{roundsTotal}</span></TabsTrigger>
                        <TabsTrigger value="trades">成交明细<span className="ml-1 font-mono text-[10px] text-muted-foreground">{tradesTotal}</span></TabsTrigger>
                        <TabsTrigger value="orders">委托记录<span className="ml-1 font-mono text-[10px] text-muted-foreground">{ordersTotal}</span></TabsTrigger>
                      </TabsList>
                    </Tabs>
                    <span className="inline-flex items-center gap-1 text-[10px] text-muted-foreground"><Clock3 className="size-3" />按时间顺序记录</span>
                  </div>
                  {tableTab === 'trades' && tradesTotal > 0 && (
                    <div className="flex flex-col gap-2 border-b bg-muted/20 px-4 py-2.5 lg:flex-row lg:items-center lg:justify-between">
                      <div className="flex flex-wrap items-center gap-2">
                        <SearchInput value={symbolDraft} onChange={setSymbolDraft} onEnter={() => commitSymbol(symbolDraft)} placeholder="标的代码（如 688200）" ariaLabel="按标的筛选成交明细" className="w-48" />
                        <FilterSelect value={tradeFilter.purpose} onValueChange={(value) => updateTradeFilter({ purpose: value })} options={purposeFilterOptions} allLabel="全部用途" ariaLabel="按用途筛选成交明细" className="w-40" />
                        <FilterSelect value={tradeFilter.side} onValueChange={(value) => updateTradeFilter({ side: value })} options={sideFilterOptions} allLabel="全部方向" ariaLabel="按方向筛选成交明细" className="w-36" />
                        {tradeFilterOn && <Button variant="ghost" size="sm" type="button" onClick={clearTradeFilters}>清空筛选</Button>}
                      </div>
                      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground" aria-live="polite">
                        {recordLoading && <Loader2 className="size-3.5 animate-spin" />}
                        <span>{tradeFilterOn ? `筛选出 ${filteredTotal} / ${tradesTotal} 条` : `共 ${tradesTotal} 条`}{recordLoading ? ' · 筛选中' : ''}</span>
                      </div>
                    </div>
                  )}
                  {tableTab === 'rounds' && <Table><TableHeader><TableRow><TableHead>轮次</TableHead><TableHead>开始时间</TableHead><TableHead>结束时间</TableHead><TableHead className="text-right">最大投入</TableHead><TableHead className="text-right">净盈亏</TableHead><TableHead className="text-right">收益率</TableHead><TableHead className="text-right">成交数</TableHead><TableHead>状态</TableHead></TableRow></TableHeader><TableBody>
                    {roundsTotal === 0 ? <TableEmpty colSpan={8} message="本次回测没有轮次记录" /> : recordItems.map((round, index) => {
                      const pnl = roundNumber(round, ['net_profit', 'pnl', 'realized_pnl']);
                      const returns = roundNumber(round, ['return_rate', 'total_return', 'return']);
                      const seq = (recordPage - 1) * recordPageSize + index + 1;
                      return <TableRow key={String(round.id ?? index)}><TableCell><strong className="font-mono text-[11px] text-primary">#{String(roundValue(round, ['round', 'index']) ?? seq).padStart(2, '0')}</strong></TableCell><TableCell>{shortDate(String(roundValue(round, ['start_date', 'start', 'opened_at']) || ''))}</TableCell><TableCell>{shortDate(String(roundValue(round, ['end_date', 'end', 'closed_at']) || ''))}</TableCell><TableCell className="text-right font-mono text-[12px]">{money(roundNumber(round, ['max_invested', 'invested']))}</TableCell><TableCell className={cn('text-right font-mono text-[12px]', pnl !== undefined && (pnl >= 0 ? 'text-destructive' : 'text-success'))}>{signedMoney(pnl)}</TableCell><TableCell className={cn('text-right font-mono text-[12px]', returns !== undefined && (returns >= 0 ? 'text-destructive' : 'text-success'))}>{signedPercent(returns)}</TableCell><TableCell className="text-right font-mono text-[12px]">{integer(roundNumber(round, ['trade_count', 'trades_count', 'total_trades']))}</TableCell><TableCell><Badge tone={lower(round.status) === 'closed' ? 'green' : 'neutral'}>{statusLabel(round.status || 'closed')}</Badge></TableCell></TableRow>;
                    })}
                  </TableBody></Table>}
                  {tableTab === 'trades' && <Table><TableHeader><TableRow><TableHead>成交时间</TableHead><TableHead>标的</TableHead><TableHead>方向</TableHead><TableHead>用途</TableHead><TableHead className="text-right">价格</TableHead><TableHead className="text-right">数量</TableHead><TableHead className="text-right">成交额</TableHead><TableHead className="text-right">费用</TableHead><TableHead className="text-right">已实现盈亏</TableHead></TableRow></TableHeader><TableBody>
                    {tradesTotal === 0 ? <TableEmpty colSpan={9} message="本次回测没有成交记录" /> : tradeFilterOn && recordItems.length === 0 && !recordLoading ? <TableEmpty colSpan={9} message="没有符合条件的成交记录" action={<Button variant="outline" size="sm" type="button" onClick={clearTradeFilters}>清空筛选</Button>} /> : recordItems.map((trade, index) => <TableRow key={String(trade.id ?? `${trade.timestamp}-${index}`)}><TableCell>{dateTime(trade.timestamp)}</TableCell><TableCell><strong className="font-mono text-[12px]" title={trade.symbol}>{displaySymbol(trade.symbol)}</strong></TableCell><TableCell><span className={cn('rounded px-1.5 py-0.5 text-[10px]', lower(trade.side) === 'buy' ? 'bg-destructive/10 text-destructive' : 'bg-success/10 text-success')}>{sideLabel(trade.side)}</span></TableCell><TableCell>{purposeLabel(trade.purpose)}</TableCell><TableCell className="text-right font-mono text-[12px]">{money(trade.price)}</TableCell><TableCell className="text-right font-mono text-[12px]">{integer(trade.quantity)}</TableCell><TableCell className="text-right font-mono text-[12px]">{money(trade.amount ?? trade.notional)}</TableCell><TableCell className="text-right font-mono text-[12px]" title={feeBreakdownTitle(trade)}>{money(trade.total_fee ?? trade.fee)}</TableCell><TableCell className={cn('text-right font-mono text-[12px]', (trade.realized_pnl || 0) >= 0 ? 'text-destructive' : 'text-success')}>{trade.realized_pnl === undefined ? '—' : signedMoney(trade.realized_pnl)}</TableCell></TableRow>)}
                  </TableBody></Table>}
                  {tableTab === 'orders' && <Table><TableHeader><TableRow><TableHead>委托时间</TableHead><TableHead>标的</TableHead><TableHead>方向</TableHead><TableHead>类型</TableHead><TableHead className="text-right">限价</TableHead><TableHead className="text-right">委托数量</TableHead><TableHead className="text-right">已成交</TableHead><TableHead>状态</TableHead></TableRow></TableHeader><TableBody>
                    {ordersTotal === 0 ? <TableEmpty colSpan={8} message="本次回测没有委托记录" /> : recordItems.map((order) => <TableRow key={order.id}><TableCell>{dateTime(order.created_at || order.timestamp)}</TableCell><TableCell><strong className="font-mono text-[12px]" title={order.symbol}>{displaySymbol(order.symbol)}</strong></TableCell><TableCell><span className={cn('rounded px-1.5 py-0.5 text-[10px]', lower(order.side) === 'buy' ? 'bg-destructive/10 text-destructive' : 'bg-success/10 text-success')}>{sideLabel(order.side)}</span></TableCell><TableCell>{orderTypeLabel(order.order_type || order.type)}</TableCell><TableCell className="text-right font-mono text-[12px]">{money(order.limit_price ?? order.price)}</TableCell><TableCell className="text-right font-mono text-[12px]">{integer(order.quantity)}</TableCell><TableCell className="text-right font-mono text-[12px]">{integer(order.filled_quantity ?? order.filled_qty)}</TableCell><TableCell><Badge>{statusLabel(order.status)}</Badge></TableCell></TableRow>)}
                  </TableBody></Table>}
                  <div className="px-4 pb-4">
                    <PaginationBar page={recordData?.page ?? recordPage} pageSize={recordData?.page_size ?? recordPageSize} total={paginationTotal} totalPages={paginationTotalPages} onPageChange={setRecordPage} onPageSizeChange={(size) => { setRecordPageSize(size); setRecordPage(1); }} disabled={recordLoading} />
                  </div>
                </Panel>
              </div>
            ) : (
              <Panel>
                <EmptyState icon={<BarChart3 className="size-6" />} title="报告数据不可用" description="该报告缺少指标数据，可能仍在生成或已被删除。" action={<Button variant="outline" size="sm" type="button" onClick={() => setViewingReport(false)}>返回报告列表</Button>} />
              </Panel>
            )
          ) : (
            <Panel title="回测报告" description={reportQuery ? `搜索「${reportQuery}」 · 共 ${reportList?.total ?? 0} 份` : `共 ${reportList?.total ?? 0} 份报告 · 可按 Report ID 或名称搜索`} action={<Button variant="outline" size="sm" type="button" onClick={() => setReportListVersion((current) => current + 1)} disabled={reportListLoading}><RefreshCw className={reportListLoading ? 'animate-spin' : ''} />刷新</Button>} flush>
              <div className="flex flex-wrap items-center gap-2 px-4 pb-2 pt-1">
                <SearchInput value={reportSearch} onChange={setReportSearch} onEnter={() => setReportQuery(reportSearch.trim())} placeholder="搜索 Report ID 或名称" ariaLabel="搜索回测报告" className="w-full max-w-sm" />
              </div>
              {reportListLoading && !reportList ? <div className="px-4 pb-4"><Loading label="正在载入回测报告" /></div> : (
                <Table>
                  <TableHeader><TableRow><TableHead>Report ID</TableHead><TableHead>名称</TableHead><TableHead>状态</TableHead><TableHead>创建时间</TableHead><TableHead>操作</TableHead></TableRow></TableHeader>
                  <TableBody>
                    {(reportList?.items.length ?? 0) === 0 ? <TableEmpty colSpan={5} message={reportQuery ? `没有匹配「${reportQuery}」的报告` : '暂无回测报告，先去运行一组实验吧'} /> : reportList?.items.map((item) => (
                      <TableRow key={item.id}>
                        <TableCell><code className="font-mono text-[10px] text-muted-foreground" title={item.id}>{item.id}</code></TableCell>
                        <TableCell><strong className="text-[12px]">{item.name}</strong></TableCell>
                        <TableCell><Badge tone={lower(item.status) === 'completed' ? 'green' : 'neutral'}>{statusLabel(item.status)}</Badge></TableCell>
                        <TableCell className="text-[12px]">{dateTime(item.created_at)}</TableCell>
                        <TableCell>
                          <div className="flex items-center gap-1">
                            <button className="rounded px-1.5 py-0.5 text-[11px] text-primary hover:bg-accent" type="button" onClick={() => void loadReport(item.id)} disabled={deletingId === item.id}>查看</button>
                            <button className="rounded px-1.5 py-0.5 text-[11px] text-destructive hover:bg-destructive/10" type="button" onClick={() => setPendingDelete({ id: item.id, name: item.name })} disabled={deletingId === item.id}>{deletingId === item.id ? '删除中' : '删除'}</button>
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
              <div className="px-4 pb-4"><PaginationBar page={reportList?.page ?? reportPage} pageSize={reportList?.page_size ?? reportPageSize} total={reportList?.total ?? 0} totalPages={reportList?.total_pages ?? 0} onPageChange={setReportPage} onPageSizeChange={(size) => { setReportPageSize(size); setReportPage(1); }} disabled={reportListLoading} /></div>
              {(reportList?.items.length ?? 0) === 0 && !reportQuery && !reportListLoading && (
                <div className="flex justify-center pb-4"><Button type="button" onClick={() => setWorkspaceTab('run')}><Play />去运行回测</Button></div>
              )}
            </Panel>
          )
        )}
      </Tabs>

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => { if (!open) setPendingDelete(null); }}
        title="删除回测报告"
        description={pendingDelete ? <>确定删除报告「{pendingDelete.name}」吗？<br />Report ID: <span className="font-mono">{pendingDelete.id}</span><br />将同步删除该报告的后台任务与检查点数据，且无法恢复。</> : ''}
        confirmLabel="删除"
        destructive
        busy={deletingId !== null}
        onConfirm={performDeleteReport}
      />
    </div>
  );
}


