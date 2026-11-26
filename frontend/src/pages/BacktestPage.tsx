import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, AlertTriangle, ArrowDownToLine, ArrowLeft, BarChart3, CalendarDays, ChevronDown, ChevronUp,
  CircleDollarSign, Clock3, FlaskConical, Gauge, Landmark, Layers3, Loader2, Pencil, Play, RefreshCw, RotateCcw, ShieldAlert, ShieldCheck, Square, Squirrel, Trash2, TrendingDown, WalletCards, Waves,
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
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { NumberInput } from '@/components/ui/number-input';
import { Progress } from '@/components/ui/progress';
import { Switch } from '@/components/ui/switch';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { cn } from '@/lib/utils';
import { defaultFeeConfig, feeBreakdownTitle, feeConfigToStrategyPatch, strategyToFeeConfig, stripFeeFields } from '../lib/feeConfig';
import { EMPTY_TRADE_FILTERS, PURPOSE_FILTER_OPTIONS, SIDE_FILTER_OPTIONS, matchesTradeFilters, tradeFilterActive } from '../lib/recordFilters';
import type { TradeFilters } from '../lib/recordFilters';
import { validateStrategyConfig, describeConfigIssue } from '../lib/configValidation';
import type { BacktestConfigDraft, BacktestJobView, BacktestSummary, DepthBucket, FeeConfig, MonthlyPnl, Order, Paginated, RecordsPage, RepoDaily, Report, ReportConfig, Round, RotationTrailItem, RotationTrailPage, RotationTrailSummary, ScreenerConfig, StrategyConfig, Trade, Withdrawal } from '../types';

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
  // 滑点模型：fixed=固定 slippage（bps，旧行为）/ impact=平方根冲击成本模型（随成交量变化）。
  // 必须登记在 fallbackConfig 里，否则 mergeKnown 在「带入回测」时会静默丢弃报告快照里的值。
  slippage_model: 'fixed',
  // 冲击成本系数 k（>=0，默认 1.0）：impact 模式下 rate = k × √(本笔量/当日成交量)。
  impact_coefficient: 1.0,
  t_target_net_spread: 0.03,
  t_exit_cutoff: '14:50',
  // 硬截止的截止日：T+0 = 建仓当日到点强平 / T+1 = 次日尾盘（默认）。
  // 同样必须登记在 fallbackConfig，否则「带入回测」会丢掉报告快照里的截止日。
  t_exit_cutoff_day: 'T+1',
  max_holding_days: 22,
  holding_tp_override: 0.03,
  // 硬性最大持仓天数：持有 ≥N 个交易日后无条件强平（0 = 不限/关闭）。
  force_exit_holding_days: 0,
  // 硬止损：持仓价格收益 ≤ 该线立即清仓（0 = 关闭，负值 = 止损比例）。
  stop_loss_pct: 0,
  // 加仓按档位先松后紧：A3 浮亏信号逐档加严（档1 最松 → 档4 = 原始 A3）。
  add_tier_strict: true,
  // 深跌幅当日做T：日内跌幅 ≥ 该值绕过底仓笔数门槛（大于 0 即可，不强制 ≥10%；0 = 关闭）。
  t_deep_drop_pct: 0.10,
  // 做T准入四阈值：最小振幅 / 换手率区间 / 最小期望净利（振幅扣掉一次往返成本后）。
  t_min_amplitude: 0.04,
  t_min_turnover: 0.03,
  t_max_turnover: 0.12,
  t_min_expected_net_spread: 0.03,
  // 做T底仓笔数门槛：累计底仓（sequence_index）达到该笔数才允许做T准入（默认 4 = 第5笔起）。
  t_min_base_entries: 4,
  // 指数 beta 腿默认开启（需求：空仓期默认用创业板 ETF 承接 beta，可手动关闭）。
  etf_beta_enabled: true,
  // beta 腿与个股「并存」（不互斥）：必须登记在 fallbackConfig 里，否则 mergeKnown
  // 只拷贝 base 已有的键，「带入回测」时报告快照里的 etf_beta_coexist 会被静默丢弃，
  // 开关永远回退到默认 false（bug：ETF 与个股并存配置带入不生效）。
  etf_beta_coexist: false,
  etf_symbol: 'sz.159915',
  etf_position_pct: 0.2,
  etf_entry_ma_days: 60,
  etf_exit_ma_days: 20,
  etf_hard_stop_pct: 0.06,
  // 入场保护（回测默认开启，可置 0 关闭）：不追高 8% + 清仓后冷却 5 个交易日。
  etf_entry_max_deviation: 0.08,
  etf_reentry_cooldown_days: 5,
  // 盈利提现：把已落袋盈利从账户现金中取出（真实资金离场）。默认关闭。
  withdraw_mode: 'none',
  withdraw_method: 'ratio',
  withdraw_ratio: 0.2,
  withdraw_fixed_amount: 5000,
  withdraw_skip_when_no_profit: true,
  // Armed 量价背离缩量项口径：annualized=线性年化（默认）/debiased=实测去偏/cumulative=旧口径（回滚）。
  risk_volume_caliber: 'annualized',
  // 缩量阈值：折算后的当日量能低于「前 5 日全天均量 × 本比例」即视为缩量（0~1 小数）。
  risk_volume_ratio: 0.8,
  // 16 笔金额的规模口径：fixed=绝对金额（默认，向后兼容）/
  // equity=按「(当前权益+已提现)/初始本金」等比缩放，避免账户增长后相对仓位被稀释。
  // 必须登记在 fallbackConfig 里，否则 mergeKnown 在「带入回测」时会静默丢弃报告快照里的值。
  installment_scale_mode: 'fixed',
  installment_scale_floor: 0.5,
  // 闲置资金逆回购（每日盘后结算）：空仓/未占用现金每日模拟国债逆回购收益。
  // 默认关闭（与后端 domain.py 一致）；必须登记在 fallbackConfig 里，
  // 否则 mergeKnown 在「带入回测」时会静默丢弃报告快照里的值。
  idle_cash_repo_enabled: false,
  // 逆回购年化收益率下限/上限（小数，0.010 = 1.00%）。校验：0 <= min <= max <= 0.1。
  repo_yield_min: 0.010,
  repo_yield_max: 0.015,
  // 方案A 两段式止盈：达标转 trailing（激活点复用后端 price_take_profit=15%）。
  // 必须登记在 fallbackConfig，否则「带入回测」会静默丢弃报告快照里的值。
  trailing_take_profit_enabled: false,
  trailing_drawdown_pct: 0.06,
};

/** 回测仅保留漏斗轮动模式（清仓后重跑选股换票）；旧的单标的模式已下线。 */

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
  l1_above_ma20: false,
  l1_stabilize_days: 0,
  l1_min_rebound: null,
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

/** 草稿/报告配置合并：只覆盖表单已有的键（null 也保留，如「超期降阈天数清空=关闭」）。 */
const mergeKnown = <T extends object>(base: T, saved?: Partial<T> | null): T => {
  if (!saved) return base;
  const source = saved as Record<string, unknown>;
  const next = { ...base } as Record<string, unknown>;
  Object.keys(base).forEach((key) => {
    if (key in source && source[key] !== undefined) next[key] = source[key];
  });
  return next as T;
};

const ISO_DATE = /^\d{4}-\d{2}-\d{2}/;

/** 月度最大回撤按「负号 + 百分比」展示；0 不加负号。 */
const drawdownLabel = (value: number): string => (value > 0 ? `-${percent(value)}` : percent(0));

/** 图表 X 轴月份压缩为 YY-MM（2026-01 → 26-01），满轴时避免标签相互挤压。 */
const shortMonth = (month: string): string => month.slice(2);

/** 空仓瓶颈层 → 可读标签（与后端 rotation_trail.funnel_bottleneck 口径对应）。 */
const trailBottleneckLabel: Record<string, string> = {
  L1: 'L1 超跌',
  L2: 'L2 否决',
  L3: 'L3 容量',
  L4: 'L4 深筛',
  unknown: '未知阶段',
};

/** 空仓区间行的原因文案：L0 关门 / 无候选（卡在哪一层）。 */
const trailWaitReason = (item: RotationTrailItem): string => {
  if (item.reason === 'gate_closed') return 'L0 大盘门关闭';
  const stage = item.bottleneck ? trailBottleneckLabel[item.bottleneck] ?? item.bottleneck : '漏斗';
  return `无候选（${stage}后为空）`;
};

/** 把回测详情/列表同步进 URL hash：详情 #/backtest/<id>，列表 #/backtest。
 *  目标与当前一致时不写，避免触发多余的 hashchange（防 App.tsx 重设 reportId prop 造成死循环）。 */
const setBacktestHash = (reportId?: string) => {
  const target = reportId ? `#/backtest/${encodeURIComponent(reportId)}` : '#/backtest';
  if (window.location.hash !== target) window.location.hash = target;
};

/** 读取当前 hash 里的报告 id 段（#/backtest/<id> → id），用于区分「浏览器前进/后退」与「组件内点击」。 */
const readBacktestHashId = (): string | undefined => {
  const segments = window.location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  return segments[0] === 'backtest' && segments[1] ? decodeURIComponent(segments[1]) : undefined;
};

export default function BacktestPage({ initialReportId }: { initialReportId?: string } = {}) {
  const [config, setConfig] = useState<StrategyConfig>(fallbackConfig);
  const [fees, setFees] = useState<FeeConfig>(defaultFeeConfig);
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
  const [tableTab, setTableTab] = useState<'trades' | 'orders' | 'withdrawals'>('trades');
  const [exporting, setExporting] = useState<'trades' | 'orders' | 'trail' | null>(null);
  const [screener, setScreener] = useState<ScreenerConfig>(fallbackScreener);
  // 换票轨迹后台分页状态：主报告已瘦身不含全量 trail，列表按页拉取；
  // 后端默认把连续同因空仓合并成区间并附带 summary 汇总块。
  const [trailData, setTrailData] = useState<RotationTrailPage | null>(null);
  const [trailPage, setTrailPage] = useState(1);
  const [trailPageSize, setTrailPageSize] = useState(20);
  const [trailLoading, setTrailLoading] = useState(false);
  // 成交明细 / 委托记录 / 已结束轮次 后台分页：主报告只带 *_total，明细按页拉取。
  const [recordData, setRecordData] = useState<RecordsPage<Trade | Order | Round | Withdrawal> | null>(null);
  const [recordPage, setRecordPage] = useState(1);
  const [recordPageSize, setRecordPageSize] = useState(20);
  const [recordLoading, setRecordLoading] = useState(false);
  // 每月盈亏表前端分页：月度数据由后端 metrics.monthly 下发（派生自权益曲线），无需额外请求。
  const [monthPage, setMonthPage] = useState(1);
  const [monthPageSize, setMonthPageSize] = useState(12);
  // 逆回购每日明细弹窗：点某月行打开，按自然月后台分页拉取逐日计提明细。
  const [repoMonth, setRepoMonth] = useState<string | null>(null);
  const [repoData, setRepoData] = useState<Paginated<RepoDaily> | null>(null);
  const [repoPage, setRepoPage] = useState(1);
  const [repoLoading, setRepoLoading] = useState(false);
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
  // 报告重命名：内联编辑态（编辑中标志 + 草稿值 + 提交中标志）。
  const [renamingReport, setRenamingReport] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const [renaming, setRenaming] = useState(false);
  // 回测表单草稿：服务端持久化，改完自动保存，进入页面优先恢复（不再被默认值重置）。
  const [draftStatus, setDraftStatus] = useState<'idle' | 'saving' | 'saved' | 'invalid' | 'error'>('idle');
  const [draftError, setDraftError] = useState('');
  const [resettingDraft, setResettingDraft] = useState(false);
  const draftHydratedRef = useRef(false);
  const lastSavedDraftRef = useRef('');
  // 前端预校验：约束与后端 StrategyConfig.__post_init__ 同口径，结果直接显示在字段下方（tips），
  // 存在问题时暂停自动保存并阻止运行/应用，避免每敲一次输入就吃一个 400。
  const configIssues = useMemo(() => validateStrategyConfig(config), [config]);
  const configInvalid = Object.keys(configIssues).length > 0;
  const firstConfigIssueField = (Object.keys(configIssues)[0] ?? undefined) as keyof StrategyConfig | undefined;
  const firstConfigIssue = firstConfigIssueField ? (configIssues[firstConfigIssueField] ?? '') : '';
  const firstConfigIssueText = firstConfigIssueField ? describeConfigIssue(firstConfigIssueField, firstConfigIssue) : '';
  // 字段下方红字在长表单里容易被忽略：出现/变化时再弹一次 toast（同一错误只弹一次）。
  const lastIssueToastRef = useRef('');
  useEffect(() => {
    if (!firstConfigIssueField) { lastIssueToastRef.current = ''; return; }
    const key = `${firstConfigIssueField}|${firstConfigIssue}`;
    if (key === lastIssueToastRef.current) return;
    lastIssueToastRef.current = key;
    toast.error(firstConfigIssueText, { duration: 5000 });
  }, [firstConfigIssueField, firstConfigIssue, firstConfigIssueText]);

  const loadInitial = async (resetForm = false) => {
    setLoading(true);
    setError('');
    try {
      const [defaults, draftResponse] = await Promise.all([
        api.strategyDefaults(),
        // 草稿读取失败不阻塞页面：退回服务端默认配置。
        api.backtestConfigDraft().catch(() => ({ draft: null, updated_at: null })),
      ]);
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
      const draft = resetForm ? null : draftResponse.draft;
      // 超期降阈默认预填 22 天 / 3%（后端默认 None = 关闭，UI 侧给推荐值，可由用户改空关闭）。
      // ETF 入场保护同理：后端 StrategyConfig 默认 0（关闭，保证旧账户/接口调用方行为不变），
      // 回测表单默认预填推荐值 8% / 5 个交易日，用户可改成 0 关闭。
      // 有服务端草稿时，草稿字段覆盖默认值（null 也保留，如清空超期降阈=关闭）。
      const baseConfig: StrategyConfig = {
        ...fallbackConfig,
        ...defaultConfig,
        etf_beta_enabled: defaultConfig.etf_beta_enabled ?? true,
        etf_beta_coexist: defaultConfig.etf_beta_coexist ?? false,
        max_holding_days: defaultConfig.max_holding_days ?? 22,
        holding_tp_override: defaultConfig.holding_tp_override ?? 0.03,
        etf_entry_max_deviation: defaultConfig.etf_entry_max_deviation || 0.08,
        etf_reentry_cooldown_days: defaultConfig.etf_reentry_cooldown_days || 5,
      };
      const baseFees = defaults.fees || strategyToFeeConfig(defaultConfig);
      const restoredFees = draft?.fees ? { ...baseFees, ...draft.fees } : baseFees;
      setConfig({ ...mergeKnown(baseConfig, draft?.config), ...feeConfigToStrategyPatch(restoredFees) });
      setFees(restoredFees);
      setScreener(mergeKnown(fallbackScreener, draft?.screener));
      setInitialCash(
        typeof draft?.initial_cash === 'number' ? draft.initial_cash : (defaults.initial_cash ?? 4_000_000),
      );
      if (draft) {
        if (draft.name !== undefined) setName(draft.name);
        if (draft.start_date && ISO_DATE.test(draft.start_date)) setStartDate(draft.start_date.slice(0, 10));
        if (draft.end_date && ISO_DATE.test(draft.end_date)) setEndDate(draft.end_date.slice(0, 10));
      } else if (resetForm) {
        // 「恢复默认」：表单字段一并回到出厂状态。
        setName('分层双账本策略回测');
        setStartDate('');
        setEndDate('');
        setAdvanced(false);
      }
      // 漏斗轮动回测：用后端选股默认阈值打底（失败不阻塞，沿用本地 fallbackScreener）。
      // 有草稿时草稿字段优先级最高，避免异步默认值把用户改过的阈值覆盖掉。
      api.screenerDefaults().then((screenerDefaults) => {
        if (screenerDefaults?.config) {
          setScreener((current) => ({
            ...current,
            ...screenerDefaults.config,
            ...(draft?.screener ?? {}),
            enable_fundamental_veto: false,
          }));
        }
      }).catch(() => undefined);
      // 旧的选股页「带入回测」是为单标的模式准备的，单标的已下线：消费并丢弃一次，避免残留提示。
      sessionStorage.removeItem('layerlab.pendingDatasets');
      // 初始化完成前不自动保存，避免把默认值写回覆盖已有草稿。
      draftHydratedRef.current = true;
      // 报告列表按需在「回测报告」tab 加载，进入页面不再自动拉取最新报告（大 payload 只在打开详情时读取）。
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '初始化失败，请检查后端服务。');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void loadInitial(); }, []);

  // 回测表单草稿自动保存：任何改动 700ms 后把整份表单 PUT 到服务端。
  // 初始金额暂不合法（用户清空重输）时跳过，避免每敲一个字符报一次 422。
  // 前端校验不通过（如做T阈值越界）也不发请求：后端必然 400，改为字段下方 tips + 底部红字提示。
  useEffect(() => {
    if (!draftHydratedRef.current) return;
    if (!Number.isFinite(initialCash) || initialCash <= 0) return;
    const payload: BacktestConfigDraft = {
      name: name.trim(),
      initial_cash: initialCash,
      start_date: startDate || null,
      end_date: endDate || null,
      config: stripFeeFields(config),
      fees,
      screener,
    };
    const serialized = JSON.stringify(payload);
    // 与已保存内容一致（例如把非法值改回原值）：恢复「已保存」状态并清掉错误。
    if (serialized === lastSavedDraftRef.current) { setDraftStatus('saved'); setDraftError(''); return; }
    if (configInvalid) { setDraftStatus('invalid'); return; }
    setDraftStatus('saving');
    const timer = window.setTimeout(() => {
      api.saveBacktestConfigDraft(payload).then(() => {
        lastSavedDraftRef.current = serialized;
        setDraftStatus('saved');
        setDraftError('');
      }).catch((cause) => {
        setDraftStatus('error');
        // 后端 detail 里就是具体原因（如「t_deep_drop_pct 必须…」），原样透出，
        // 别再只显示一句泛泛的「保存失败」让用户去翻 Network 面板。
        setDraftError(cause instanceof Error ? cause.message : '');
      });
    }, 700);
    return () => window.clearTimeout(timer);
  }, [name, initialCash, startDate, endDate, config, fees, screener, configInvalid]);

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
        setTableTab('trades');
        setBacktestHash(result.id); // 新报告详情也映射进 URL，刷新可停留
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
  const startJob = async () => {
    const payload = {
      mode: 'funnel' as const, name: name.trim() || '选股漏斗轮动回测',
      initial_cash: initialCash, start_date: startDate || undefined, end_date: endDate || undefined,
      config: stripFeeFields(config), fees, screener,
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
    if (configInvalid) { setError(`配置有误：${firstConfigIssueText}`); return; }
    if (startDate && endDate && startDate > endDate) { setError('开始日期不能晚于结束日期，请调整回测区间。'); return; }
    if (!Number.isFinite(initialCash) || initialCash <= 0) { setError('初始资金必须大于 0。'); return; }
    // 漏斗轮动模式用本地 universe.db，无需选择数据集。
    if (screener.boards.length === 0) { setError('请至少选择一个板块（创业板 / 科创板）。'); return; }
    setSubmitting(true);
    setRunning(true);
    setError('');
    try {
      await startJob();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '回测运行失败，请稍后重试。');
      setRunning(false);
    } finally {
      setSubmitting(false);
    }
  };

  // 把当前回测配置（策略字段 + 费率）整套应用到模拟盘「当前账户」，下一根 K 起生效。
  const applyToPaper = async () => {
    if (configInvalid) { setError(`配置有误：${firstConfigIssueText}`); return; }
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

  /** 恢复默认：删除服务端草稿并重新载入默认配置（数据集/日期也回到默认选择）。 */
  const resetConfigDraft = async () => {
    setResettingDraft(true);
    setError('');
    try {
      await api.resetBacktestConfigDraft();
      await loadInitial(true);
      toast.success('已恢复默认回测配置。');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '恢复默认配置失败，请稍后重试。');
    } finally {
      setResettingDraft(false);
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
      // 续跑成功即清掉中断时写入的 error（后端 resume 已置 error=None），红字提示立刻消失，
      // 不再残留到进度条/终态；真正再次出错时后端会写入新 error，红字才会重新出现。
      setJobView({ ...view, error: null });
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

  /** 放弃在途作业：清空前端在途状态，并删除服务端作业行 + 检查点。
   *
   * 旧实现只清前端状态 + 写 sessionStorage（刷新即丢），服务端那行 CANCELLED/FAILED
   * 任务永远还在，一刷新又被 list_jobs 拉回来重新弹「可续传」横幅。现在真正调
   * DELETE /backtests/jobs/{id} 删掉服务端作业，刷新后不再复现。删除失败（如网络/
   * 已被并发删除）不阻塞前端收尾，仅提示。
   */
  const dismissJob = async () => {
    stopPolling();
    const id = jobIdRef.current || jobView?.id;
    jobIdRef.current = null;
    setJobView(null);
    setRunning(false);
    if (!id) return;
    settledJobIdsRef.current.add(id);
    try {
      await api.deleteBacktestJob(id);
    } catch {
      // 删除失败不阻塞前端收尾：作业已到终态，最坏情况是刷新后再看到一次横幅。
    }
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
    try {
      setReport(await api.backtest(id));
      setBacktestHash(id); // 成功：详情映射进 URL，刷新/分享可停留该报告
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : '报告读取失败。';
      setError(message);
      // 报告不存在/读取失败：优雅回落列表视图并清回 #/backtest，不白屏。
      setReport(null);
      setViewingReport(false);
      setBacktestHash();
      toast.error(`无法打开回测报告：${message}`);
    } finally {
      setReportLoading(false);
    }
  };

  /** 关闭详情回到列表：清详情态并把 hash 同步回 #/backtest（返回按钮 / 删除清理复用）。 */
  const closeDetail = () => {
    setViewingReport(false);
    setReport(null);
    setTrailData(null);
    setRecordData(null);
    setBacktestHash();
  };

  /** 进入报告重命名内联编辑态：草稿预填当前名称。 */
  const startRename = () => {
    if (!report) return;
    setRenameValue(report.name || '');
    setRenamingReport(true);
  };

  const cancelRename = () => {
    setRenamingReport(false);
    setRenameValue('');
  };

  /** 提交重命名：前端先校验非空，成功后同步详情态与列表并退出编辑态。 */
  const submitRename = async () => {
    if (!report) return;
    const nextName = renameValue.trim();
    if (!nextName) {
      toast.error('报告名称不能为空。');
      return;
    }
    setRenaming(true);
    try {
      const result = await api.renameBacktest(report.id, nextName);
      // 同步当前详情（report.name 即标题展示），并刷新列表缓存里的摘要名。
      setReport((current) => (current ? { ...current, name: result.name } : current));
      setReportList((current) => current
        ? { ...current, items: current.items.map((item) => (item.id === result.id ? { ...item, name: result.name } : item)) }
        : current);
      setRenamingReport(false);
      setRenameValue('');
      toast.success('报告已重命名。');
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : '重命名失败。';
      toast.error(`重命名失败：${message}`);
    } finally {
      setRenaming(false);
    }
  };

  // 浏览器前进/后退 + 刷新恢复：reportId prop 变化时打开或关闭详情。
  // 只用 report?.id 做守卫即可：loadReport 异步完成后 report.id === initialReportId，
  // hash 回流再触发 effect 时条件为 false，自然跳过，不会死循环。
  // 不再额外比对 readBacktestHashId()（刷新时 hash 与 initialReportId 必然相同，
  // 那个守卫会错误地阻止初始加载，导致刷新后永远停在 run tab 而不进详情）。
  useEffect(() => {
    if (initialReportId) {
      if (initialReportId !== report?.id) {
        void loadReport(initialReportId);
      }
    } else if (viewingReport && !readBacktestHashId()) {
      closeDetail();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialReportId]);

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
        closeDetail(); // 清详情态并把 hash 同步回 #/backtest
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

  const exportCsv = async (kind: 'trades' | 'orders' | 'trail') => {
    if (!report) return;
    setExporting(kind);
    setError('');
    try { await downloadBacktestCsv(report.id, kind); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '导出失败。'); }
    finally { setExporting(null); }
  };

  const updateConfig = <K extends keyof StrategyConfig>(key: K, value: StrategyConfig[K]) => setConfig((current) => ({ ...current, [key]: value }));

  /** 报告详情「带入回测」：把报告快照里的参数回填到运行表单（含日期/费率/选股阈值）。 */
  const applyReportToForm = () => {
    if (!report) return;
    const saved = report.config as ReportConfig;
    const reportFees = strategyToFeeConfig(saved);
    setConfig({ ...mergeKnown(fallbackConfig, saved), ...feeConfigToStrategyPatch(reportFees) });
    setFees(reportFees);
    if (saved.screener) setScreener(mergeKnown(fallbackScreener, saved.screener));
    setName(report.name || '回测');
    if (typeof saved.initial_cash === 'number') setInitialCash(saved.initial_cash);
    if (saved.start_date && ISO_DATE.test(saved.start_date)) setStartDate(saved.start_date.slice(0, 10));
    if (saved.end_date && ISO_DATE.test(saved.end_date)) setEndDate(saved.end_date.slice(0, 10));
    setAdvanced(false);
    setWorkspaceTab('run');
    toast.success(`已把报告「${report.name}」的参数带入回测配置，可直接运行或微调。`);
  };
  const metrics = report?.metrics;
  // 现金流调整（TWR）口径指标：以 metrics.cashflow_adjusted 为主，逐字段回退顶层旧口径，
  // 兼容旧报告（无 cashflow_adjusted）与残缺块（部分字段缺失），绝不显示 NaN。
  const cfa = metrics?.cashflow_adjusted;
  const cf = {
    annualized_return: cfa?.annualized_return ?? metrics?.annualized_return ?? 0,
    max_drawdown: cfa?.max_drawdown ?? metrics?.max_drawdown ?? 0,
    sharpe_ratio: cfa?.sharpe_ratio ?? metrics?.sharpe_ratio ?? 0,
    calmar_ratio: cfa?.calmar_ratio ?? metrics?.calmar_ratio ?? 0,
    volatility: cfa?.volatility ?? metrics?.volatility ?? 0,
  };
  // ETF beta 腿摘要（新报告才有；旧报告 undefined）。
  const betaInfo = (report?.data_info?.beta as {
    enabled?: boolean; symbol?: string; trade_count?: number; pnl?: number;
    entry_date?: string | null; last_exit_date?: string | null; exit_reason?: string | null;
  } | undefined) ?? null;
  // 明细条数：主报告只带 *_total；旧缓存报告回退到本地数组长度。
  const tradesTotal = report?.trades_total ?? report?.trades?.length ?? 0;
  const ordersTotal = report?.orders_total ?? report?.orders?.length ?? 0;
  const withdrawalsTotal = report?.withdrawals_total ?? report?.withdrawals?.length ?? 0;
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
  // 逆回购收益列 / 弹窗入口：仅当报告带 repo_income（新报告且开启逆回购）才显示，旧报告全 undefined 时隐藏。
  const hasRepoIncome = useMemo(() => monthly.some((item) => item.repo_income != null), [monthly]);
  const monthPageCount = Math.max(1, Math.ceil(monthly.length / monthPageSize));
  // 切换每页条数后旧页码可能越界：渲染前收敛到最后一页，避免出现空表。
  const safeMonthPage = Math.min(monthPage, monthPageCount);
  const monthRows = monthly.slice((safeMonthPage - 1) * monthPageSize, safeMonthPage * monthPageSize);
  const rotationTrail = useMemo<RotationTrailItem[]>(() => trailData?.items ?? [], [trailData]);
  // 换票轨迹汇总（后端读时聚合；旧报告同样有）：节奏、空仓构成、胜率与退出原因。
  const trailSummary = useMemo<RotationTrailSummary | null>(() => trailData?.summary ?? null, [trailData]);
  const trailStageSummary = useMemo(() => {
    if (!trailSummary) return '';
    return Object.entries(trailSummary.no_candidate_by_stage)
      .sort(([, a], [, b]) => b - a)
      .map(([stage, days]) => `${trailBottleneckLabel[stage] ?? stage} ${integer(days)} 天`)
      .join(' / ');
  }, [trailSummary]);
  const trailExitSummary = useMemo(() => {
    if (!trailSummary) return '';
    return Object.entries(trailSummary.exit_reasons)
      .sort(([, a], [, b]) => b - a)
      .slice(0, 4)
      .map(([reason, count]) => `${reason} ${integer(count)}`)
      .join(' · ');
  }, [trailSummary]);
  // 换票轨迹行展开：查看该选股日的逐轮决策日志（为什么开门/关门/空仓/旁路几只）+ 漏斗计数。
  const [expandedTrails, setExpandedTrails] = useState<Set<string>>(new Set());
  const toggleTrail = (key: string) => {
    setExpandedTrails((current) => {
      const next = new Set(current);
      if (next.has(key)) { next.delete(key); } else { next.add(key); }
      return next;
    });
  };
  // 轮次行 →「成交明细」联动：切 tab 会清空筛选，因此先把标的存 ref，由下面的 effect 清空后应用。
  const pendingTrailSymbolRef = useRef<string | null>(null);
  const focusRoundTrades = (symbol: string) => {
    const code = displaySymbol(symbol);
    if (!code || code === '—') return;
    if (tableTab === 'trades') {
      setSymbolDraft(code);
      setTradeFilter((previous) => ({ ...previous, symbol: code }));
      setRecordPage(1);
    } else {
      pendingTrailSymbolRef.current = code;
      setTableTab('trades');
    }
    window.requestAnimationFrame(() => {
      document.getElementById('backtest-records')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
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

  // 逆回购每日明细弹窗：打开某月（repoMonth）或翻页时按自然月拉取一页；关闭时清空。
  // page_size 默认 31（一个月最多 31 个自然日，单页基本覆盖整月，超出再翻）。
  useEffect(() => {
    if (!report || !repoMonth) { setRepoData(null); return; }
    let cancelled = false;
    setRepoLoading(true);
    api.repoDaily(report.id, { page: repoPage, pageSize: 31, month: repoMonth })
      .then((paged) => { if (!cancelled) setRepoData(paged); })
      .catch(() => { if (!cancelled) setRepoData(null); })
      .finally(() => { if (!cancelled) setRepoLoading(false); });
    return () => { cancelled = true; };
  }, [report, repoMonth, repoPage]);

  // 成交 / 委托 / 轮次 明细分页：切 tab 或翻页时按需拉取当前 tab 的一页。
  useEffect(() => { setRecordPage(1); }, [report?.id, tableTab]);

  // 成交明细筛选：切 tab / 换报告时清空（筛选栏只服务成交明细 tab）。
  // 换票轨迹「在成交明细中查看」会先把标的放进 ref，这里清空后立即应用，避免被覆盖。
  useEffect(() => {
    const pending = pendingTrailSymbolRef.current;
    pendingTrailSymbolRef.current = null;
    setSymbolDraft(pending ?? '');
    setTradeFilter(pending ? { ...EMPTY_TRADE_FILTERS, symbol: pending } : EMPTY_TRADE_FILTERS);
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
    const local = tableTab === 'trades' ? report?.trades : tableTab === 'orders' ? report?.orders : report?.withdrawals;
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
  const recordsTotal = tableTab === 'trades' ? tradesTotal : tableTab === 'orders' ? ordersTotal : withdrawalsTotal;
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
                  <div><h2 className="text-[15px] font-semibold">实验配置</h2><p className="text-[12px] text-muted-foreground">漏斗轮动回测：清仓后重跑选股漏斗换票；定义数据边界与交易摩擦，再启动本地回测。</p></div>
                </div>
              </div>

              <div className="grid gap-2">
                {/* 做 T 分组：启停 + 七个准入门槛/退出参数 */}
                <div className="rounded-xl border bg-muted/25 p-3">
                  <strong className="mb-2 block text-[12px] font-semibold text-foreground/80">做 T（日内做差价）</strong>
                  <SwitchRow label="启用做 T" description="可选，不占用 16 笔序列" checked={config.enable_t} onCheckedChange={(checked) => updateConfig('enable_t', checked)} />
                  {config.enable_t && (
                    <FormGrid className="mt-2">
                      <Field label="做 T 净利止盈（%）" hint="扣完成本后净赚该幅度即平 T" error={configIssues.t_target_net_spread}>
                        <NumberInput unit="%" min={0} max={100} step={0.1} value={Number(((config.t_target_net_spread ?? 0.03) * 100).toFixed(2))} onChange={(value) => updateConfig('t_target_net_spread', Number(value) / 100)} />
                      </Field>
                      <Field label="做 T 截止日" hint="T+1 = 次日尾盘截止（默认，T0 当日不强平）；T+0 = 建仓当日到点即强平" error={configIssues.t_exit_cutoff_day}>
                        <SelectField value={config.t_exit_cutoff_day ?? 'T+1'} onValueChange={(value) => updateConfig('t_exit_cutoff_day', value as StrategyConfig['t_exit_cutoff_day'])} options={[
                          { value: 'T+1', label: 'T+1 · 次日尾盘截止（默认）' },
                          { value: 'T+0', label: 'T+0 · 建仓当日截止' },
                        ]} ariaLabel="做 T 截止日" />
                      </Field>
                      <Field label="做 T 截止时间" hint="截止日到点仍未止盈则尾盘强平" error={configIssues.t_exit_cutoff}>
                        <Input type="time" step={300} value={config.t_exit_cutoff ?? '14:50'} onChange={(event) => updateConfig('t_exit_cutoff', event.target.value)} />
                      </Field>
                      <Field label="深跌幅当日做T（%）" hint="日内跌幅 ≥ 该值绕过底仓笔数门槛（大于 0 即可，不强制 ≥10%）；0 = 关闭" error={configIssues.t_deep_drop_pct}>
                        <NumberInput unit="%" min={0} max={100} step={0.5} value={Number((((config.t_deep_drop_pct ?? 0.10) || 0) * 100).toFixed(1))} onChange={(value) => updateConfig('t_deep_drop_pct', Number(value) === 0 ? 0 : Number(value) / 100)} />
                      </Field>
                      <Field label="做 T 最小振幅（%）" hint="近5根平均振幅低于该值不做 T" error={configIssues.t_min_amplitude}>
                        <NumberInput unit="%" min={0} max={100} step={0.5} value={Number(((config.t_min_amplitude ?? 0.04) * 100).toFixed(2))} onChange={(value) => updateConfig('t_min_amplitude', Number(value) / 100)} />
                      </Field>
                      <Field label="做 T 换手率下限（%）" hint="当日换手低于该值不做 T" error={configIssues.t_min_turnover}>
                        <NumberInput unit="%" min={0} max={100} step={0.5} value={Number(((config.t_min_turnover ?? 0.03) * 100).toFixed(2))} onChange={(value) => updateConfig('t_min_turnover', Number(value) / 100)} />
                      </Field>
                      <Field label="做 T 换手率上限（%）" hint="当日换手高于该值不做 T（需 ≥ 下限）" error={configIssues.t_max_turnover}>
                        <NumberInput unit="%" min={0} max={100} step={0.5} value={Number(((config.t_max_turnover ?? 0.12) * 100).toFixed(2))} onChange={(value) => updateConfig('t_max_turnover', Number(value) / 100)} />
                      </Field>
                      <Field label="做 T 最小期望净利（%）" hint="振幅扣掉一次往返成本后仍需 ≥ 该值" error={configIssues.t_min_expected_net_spread}>
                        <NumberInput unit="%" min={0} max={100} step={0.5} value={Number(((config.t_min_expected_net_spread ?? 0.03) * 100).toFixed(2))} onChange={(value) => updateConfig('t_min_expected_net_spread', Number(value) / 100)} />
                      </Field>
                      <Field label="底仓笔数门槛（笔）" hint="累计底仓达到该笔数才允许做T准入；深跌幅当日可绕过。1~16，默认 4（第5笔起）" error={configIssues.t_min_base_entries}>
                        <NumberInput unit="笔" min={1} max={16} step={1} value={config.t_min_base_entries ?? 4} onChange={(value) => updateConfig('t_min_base_entries', Number(value))} />
                      </Field>
                    </FormGrid>
                  )}
                </div>

                {/* 加仓分组 */}
                <div className="rounded-xl border bg-muted/25 p-3">
                  <strong className="mb-2 block text-[12px] font-semibold text-foreground/80">加仓</strong>
                  <SwitchRow label="加仓按档位先松后紧" description="A3 浮亏信号逐档加严：档1（10万）最松（浮亏+缩量），档4（40万）最严（= 原始 A3）；浮盈右侧回撤不变。关闭 = 全档统一原始 A3" checked={config.add_tier_strict ?? true} onCheckedChange={(checked) => updateConfig('add_tier_strict', checked)} />
                  {/* 16 笔金额的规模口径：equity 模式按账户权益等比缩放，避免账户增长后相对仓位被稀释 */}
                  <FormGrid className="mt-2">
                    <Field label="16 笔金额口径" hint="fixed=绝对金额（默认，向后兼容）；equity=按「(当前权益+已提现)/初始本金」等比缩放，账户增长时同步放大仓位、回撤时同步缩小（研究报告：敞口与 TWR 年化双升，代价 maxDD 放大）" error={configIssues.installment_scale_mode}>
                      <SelectField value={config.installment_scale_mode ?? 'fixed'} onValueChange={(value) => updateConfig('installment_scale_mode', value as StrategyConfig['installment_scale_mode'])} options={[
                        { value: 'fixed', label: 'fixed · 绝对金额（默认）' },
                        { value: 'equity', label: 'equity · 按权益等比缩放' },
                      ]} />
                    </Field>
                    <Field label="缩放下限（倍）" hint="equity 模式下的放大倍数下限：权益跌到初始本金一半以下也至少按半仓规模下注，防止尘粒单（0~1 小数）" error={configIssues.installment_scale_floor}>
                      <NumberInput unit="×" min={0.1} max={1} step={0.1} value={config.installment_scale_floor ?? 0.5} onChange={(value) => updateConfig('installment_scale_floor', Number(value))} />
                    </Field>
                  </FormGrid>
                </div>

                {/* 止盈分组 */}
                <div className="rounded-xl border bg-muted/25 p-3">
                  <strong className="mb-2 block text-[12px] font-semibold text-foreground/80">止盈</strong>
                  {/* 方案A 两段式止盈：浮盈到价格轨止盈（默认15%）后转 trailing；关闭 = 旧的到点即清仓 */}
                  <SwitchRow label="达标转 Trailing（两段式止盈）" description="开启后：浮盈达到价格轨止盈（默认15%）不立即清仓，转为跟踪浮盈峰值、从峰值回撤设定比例才清仓。关闭 = 到点即一次性清仓（旧行为）" checked={Boolean(config.trailing_take_profit_enabled)} onCheckedChange={(checked) => updateConfig('trailing_take_profit_enabled', checked)} />
                  {config.trailing_take_profit_enabled && (
                    <FormGrid className="mt-2">
                      <Field label="Trailing 回撤清仓（%）" hint="转 trailing 后，从浮盈峰值回撤该比例即清仓（0~100，不含 0）。默认 6%" error={configIssues.trailing_drawdown_pct}>
                        <NumberInput unit="%" min={0.1} max={100} step={0.5} value={Number(((config.trailing_drawdown_pct ?? 0.06) * 100).toFixed(1))} onChange={(value) => updateConfig('trailing_drawdown_pct', Number(value) / 100)} />
                      </Field>
                    </FormGrid>
                  )}
                  <FormGrid>
                    <Field label="超期降阈天数" hint="持有 ≥N 个交易日后止盈降阈；留空=关闭">
                      <NumberInput unit="交易日" min={1} max={250} step={1} allowEmpty value={config.max_holding_days ?? ''} onChange={(value) => updateConfig('max_holding_days', value === '' ? null : Number(value))} />
                    </Field>
                    <Field label="降阈后止盈（%）" hint="超期后价格轨/账户轨止盈阈值">
                      <NumberInput unit="%" min={0} max={100} step={0.1} allowEmpty value={config.holding_tp_override != null ? Number((config.holding_tp_override * 100).toFixed(2)) : ''} onChange={(value) => updateConfig('holding_tp_override', value === '' ? null : Number(value) / 100)} />
                    </Field>
                    <Field label="最大持仓天数" hint="持有 ≥N 个交易日后无条件强平（兜底）；0 = 不限" error={configIssues.force_exit_holding_days}>
                      <NumberInput unit="交易日" min={0} max={250} step={1} value={config.force_exit_holding_days ?? 0} onChange={(value) => updateConfig('force_exit_holding_days', Number(value))} />
                    </Field>
                    <Field label="硬止损（%）" hint="持仓浮亏达该比例立即清仓（不等最大持仓天数强平）；0 = 关闭" error={configIssues.stop_loss_pct}>
                      <NumberInput unit="%" min={-100} max={0} step={1} value={config.stop_loss_pct != null ? Number((config.stop_loss_pct * 100).toFixed(1)) : 0} onChange={(value) => updateConfig('stop_loss_pct', Number(value) / 100)} />
                    </Field>
                  </FormGrid>
                </div>

                {/* Armed 量价背离（辅助清仓）缩量项口径 */}
                <div className="rounded-xl border bg-muted/25 p-3">
                  <strong className="mb-2 block text-[12px] font-semibold text-foreground/80">Armed 量价背离（辅助清仓）</strong>
                  <FormGrid>
                    <Field label="缩量口径" hint="盘中把当日累计量折算成日量当量再与前5日均量比：annualized=线性年化（默认，只会高估早盘量→缩量项偏保守）；debiased=用实测累计分布去偏；cumulative=旧口径（累计量直比，偏松，仅回滚）" error={configIssues.risk_volume_caliber}>
                      <SelectField value={config.risk_volume_caliber ?? 'annualized'} onValueChange={(value) => updateConfig('risk_volume_caliber', value as StrategyConfig['risk_volume_caliber'])} options={[
                        { value: 'annualized', label: 'annualized · 线性年化（默认）' },
                        { value: 'debiased', label: 'debiased · 实测分布去偏' },
                        { value: 'cumulative', label: 'cumulative · 旧口径（回滚）' },
                      ]} />
                    </Field>
                    <Field label="缩量阈值（%）" hint="折算后的当日量能低于「前 5 日全天均量 × 本比例」即视为缩量" error={configIssues.risk_volume_ratio}>
                      <NumberInput unit="%" min={0.1} max={100} step={1} value={Number(((config.risk_volume_ratio ?? 0.8) * 100).toFixed(0))} onChange={(value) => updateConfig('risk_volume_ratio', Number(value) / 100)} />
                    </Field>
                  </FormGrid>
                </div>

                <div className="rounded-xl border bg-muted/25 p-3">
                    <SwitchRow label="指数 beta 腿（创业板 ETF）" description="个股空仓时持有 ETF；选到个股后卖出换股，不占 16 笔加仓序列" checked={Boolean(config.etf_beta_enabled)} onCheckedChange={(checked) => updateConfig('etf_beta_enabled', checked)} />
                    {config.etf_beta_enabled && (
                      <SwitchRow className="mt-1" label="与个股并存（不互斥）" description="选到个股后不清 ETF，与个股共用现金；建仓额度按可用现金封顶。关闭则回到「个股空仓才持 ETF、选到个股清仓」" checked={Boolean(config.etf_beta_coexist)} onCheckedChange={(checked) => updateConfig('etf_beta_coexist', checked)} />
                    )}
                    {config.etf_beta_enabled && (
                      <FormGrid className="mt-2">
                        <Field label="ETF 代码" hint="默认创业板 ETF 159915" error={configIssues.etf_symbol}>
                          <SelectField value={config.etf_symbol ?? 'sz.159915'} onValueChange={(value) => updateConfig('etf_symbol', value)} options={[
                            { value: 'sz.159915', label: 'sz.159915 · 创业板ETF易方达' },
                            { value: 'sz.159952', label: 'sz.159952 · 创业板ETF广发' },
                            { value: 'sz.159964', label: 'sz.159964 · 创业板ETF平安' },
                          ]} />
                        </Field>
                        <Field label="仓位（占权益 %）" hint="空仓时买入的 ETF 市值占账户权益比例" error={configIssues.etf_position_pct}>
                          <NumberInput unit="%" min={1} max={100} step={1} value={Number(((config.etf_position_pct ?? 0.2) * 100).toFixed(0))} onChange={(value) => updateConfig('etf_position_pct', Number(value) / 100)} />
                        </Field>
                        <Field label="入场均线（日）" hint="创业板指收盘站上该均线才持 ETF" error={configIssues.etf_entry_ma_days}><NumberInput unit="日" min={2} max={250} step={1} precision={0} value={config.etf_entry_ma_days ?? 60} onChange={(value) => updateConfig('etf_entry_ma_days', Number(value))} /></Field>
                        <Field label="出场均线（日）" hint="创业板指收盘跌破该均线清 ETF" error={configIssues.etf_exit_ma_days}><NumberInput unit="日" min={2} max={250} step={1} precision={0} value={config.etf_exit_ma_days ?? 20} onChange={(value) => updateConfig('etf_exit_ma_days', Number(value))} /></Field>
                        <Field label="ETF 硬止损（%）" hint="ETF 价格较建仓成本回撤达到该幅度立即清仓" error={configIssues.etf_hard_stop_pct}><NumberInput unit="%" min={0.1} max={99} step={0.1} value={Number(((config.etf_hard_stop_pct ?? 0.06) * 100).toFixed(1))} onChange={(value) => updateConfig('etf_hard_stop_pct', Number(value) / 100)} /></Field>
                        <Field label="不追高上限（偏离均线 %）" hint="指数收盘高于入场均线超过该幅度时不建仓，等回踩再买；0 关闭" error={configIssues.etf_entry_max_deviation}><NumberInput unit="%" min={0} max={50} step={0.5} value={Number(((config.etf_entry_max_deviation ?? 0) * 100).toFixed(1))} onChange={(value) => updateConfig('etf_entry_max_deviation', Number(value) / 100)} /></Field>
                        <Field label="清仓后冷却（交易日）" hint="ETF 清仓后至少 N 个交易日内不再建仓，抑制均线附近反复打脸；0 关闭" error={configIssues.etf_reentry_cooldown_days}><NumberInput unit="日" min={0} max={60} step={1} precision={0} value={config.etf_reentry_cooldown_days ?? 0} onChange={(value) => updateConfig('etf_reentry_cooldown_days', Number(value))} /></Field>
                      </FormGrid>
                    )}
                  </div>

                {/* 闲置资金逆回购（每日盘后结算）：空仓/未占用现金每日模拟国债逆回购收益 */}
                <div className="rounded-xl border bg-muted/25 p-3">
                  <SwitchRow label="闲置资金逆回购" description="空仓/未占用现金每日盘后结算，模拟国债逆回购收益（收益进现金，体现在每月盈亏与总收益）；回测与模拟盘同一套算法" checked={Boolean(config.idle_cash_repo_enabled)} onCheckedChange={(checked) => updateConfig('idle_cash_repo_enabled', checked)} />
                  {config.idle_cash_repo_enabled && (
                    <FormGrid className="mt-2">
                      <Field label="年化下限（%）" hint="每日收益率在 [下限, 上限] 区间按日期确定性取值（同一天同 rate，可复现）" error={configIssues.repo_yield_min}>
                        <NumberInput unit="%" min={0} max={10} step={0.1} value={Number(((config.repo_yield_min ?? 0.010) * 100).toFixed(2))} onChange={(value) => updateConfig('repo_yield_min', Number(value) / 100)} />
                      </Field>
                      <Field label="年化上限（%）" hint="上限最多 10%（防把 1% 误填成 100%）；须 ≥ 下限" error={configIssues.repo_yield_max}>
                        <NumberInput unit="%" min={0} max={10} step={0.1} value={Number(((config.repo_yield_max ?? 0.015) * 100).toFixed(2))} onChange={(value) => updateConfig('repo_yield_max', Number(value) / 100)} />
                      </Field>
                    </FormGrid>
                  )}
                </div>

                <div className="rounded-xl border bg-muted/25 p-3">
                  <FormGrid>
                    <Field label="盈利提现模式" hint="把已落袋盈利从账户现金中取出（真实资金离场，降低可买资金与权益）；仅按已实现盈亏计算">
                      <SelectField value={config.withdraw_mode ?? 'none'} onValueChange={(value) => updateConfig('withdraw_mode', value as StrategyConfig['withdraw_mode'])} options={[
                        { value: 'none', label: '关闭' },
                        { value: 'monthly', label: '每月提现（跨月按上一自然月已实现盈亏）' },
                        { value: 'per_trade', label: '每笔提现（每笔盈利卖出后）' },
                      ]} />
                    </Field>
                    {(config.withdraw_mode ?? 'none') !== 'none' && (
                      <>
                        <Field label="提取方式" hint={config.withdraw_method === 'fixed' ? '当期盈利不足固定金额仍提取该金额，仅以现金余额封顶' : '提取当期已实现盈亏的该比例'}>
                          <SelectField value={config.withdraw_method ?? 'ratio'} onValueChange={(value) => updateConfig('withdraw_method', value as StrategyConfig['withdraw_method'])} options={[
                            { value: 'ratio', label: '按盈利比例' },
                            { value: 'fixed', label: '按固定金额' },
                          ]} />
                        </Field>
                        {(config.withdraw_method ?? 'ratio') === 'ratio' ? (
                          <Field label="提取比例（%）" hint="提取当期已实现盈亏的该比例（0~100）" error={configIssues.withdraw_ratio}>
                            <NumberInput unit="%" min={0.1} max={100} step={1} value={Number(((config.withdraw_ratio ?? 0.2) * 100).toFixed(1))} onChange={(value) => updateConfig('withdraw_ratio', Number(value) / 100)} />
                          </Field>
                        ) : (
                          <Field label="固定金额（元）" hint="当期盈利不足该金额时仍提取该金额（仅以现金余额封顶）" error={configIssues.withdraw_fixed_amount}>
                            <NumberInput unit="CNY" min={0} step={1000} precision={2} value={config.withdraw_fixed_amount ?? 5000} onChange={(value) => updateConfig('withdraw_fixed_amount', Number(value))} />
                          </Field>
                        )}
                        {config.withdraw_mode === 'monthly' && (
                          <Field label="当月不盈利则不提取" hint="已实现盈亏 <= 0 的月份不触发提现">
                            <SwitchRow label="当月不盈利则不提取" description={config.withdraw_skip_when_no_profit ? '无盈利月份跳过' : '无盈利月份仍按固定金额提取'} checked={Boolean(config.withdraw_skip_when_no_profit)} onCheckedChange={(checked) => updateConfig('withdraw_skip_when_no_profit', checked)} className="w-full" />
                          </Field>
                        )}
                      </>
                    )}
                  </FormGrid>
                </div>

                <div className="flex items-start gap-2 rounded-lg border border-primary/15 bg-accent/40 px-3 py-2 text-[11px] leading-5 text-muted-foreground">
                  <Layers3 className="mt-0.5 size-3.5 shrink-0 text-primary" />
                  <span>漏斗轮动回测：<strong className="text-foreground">不再锁定单一标的</strong>，每次清仓后按下方选股漏斗（L0→L4）重跑、取当时第一名换票；行情取自本地 universe.db 缓存（双创日线），为日线口径。</span>
                </div>

                <FormGrid>
                  <Field label="实验名称" wide>
                    <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="输入回测名称" />
                  </Field>
                  <Field label="开始日期">
                    <div className="relative"><CalendarDays className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" /><Input type="date" value={startDate} max={endDate || undefined} onChange={(event) => setStartDate(event.target.value)} className="pl-8" /></div>
                  </Field>
                  <Field label="结束日期">
                    <div className="relative"><CalendarDays className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" /><Input type="date" value={endDate} min={startDate || undefined} onChange={(event) => setEndDate(event.target.value)} className="pl-8" /></div>
                  </Field>
                  <Field label="初始资金">
                    <NumberInput unit="CNY" min={1} step={10000} precision={0} value={initialCash} onChange={(value) => { const parsed = Number(value); setInitialCash(Number.isFinite(parsed) ? parsed : 0); }} />
                  </Field>
                </FormGrid>

                <ScreenerConfigPanel value={screener} onChange={setScreener} fundamentalLockedOff />

                <button type="button" className="flex items-center justify-between rounded-lg border bg-card px-3 py-2 text-left text-[12px] font-medium text-foreground/80 hover:bg-muted/40" onClick={() => setAdvanced(!advanced)} aria-expanded={advanced}>
                  <span className="inline-flex items-center gap-2"><Gauge className="size-3.5" />交易摩擦与撮合参数</span>{advanced ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
                </button>
                {advanced && (
                  <div className="grid gap-2">
                    <FeeConfigPanel value={fees} onChange={setFees} />
                    <FormGrid>
                      <Field label="滑点模型" hint="fixed=固定 slippage（bps，旧行为，与历史回测一致）；impact=冲击成本模型，滑点随成交量变化（大单更贵）" error={configIssues.slippage_model}>
                        <SelectField value={config.slippage_model ?? 'fixed'} onValueChange={(value) => updateConfig('slippage_model', value as StrategyConfig['slippage_model'])} options={[{ value: 'fixed', label: '固定 slippage（bps）' }, { value: 'impact', label: '冲击成本模型（随量变化）' }]} ariaLabel="滑点模型" />
                      </Field>
                      {config.slippage_model === 'impact' ? (
                        <Field label="冲击系数" hint="rate = 系数 × √(本笔量/当日成交量)；默认 1.0 时满额单≈√(参与度)" error={configIssues.impact_coefficient}>
                          <NumberInput unit="k" min={0} step={0.001} value={config.impact_coefficient ?? 1.0} onChange={(value) => updateConfig('impact_coefficient', Number(value))} />
                        </Field>
                      ) : (
                        <Field label="滑点" error={configIssues.slippage_bps}><NumberInput unit="bps" min={0} step={1} precision={0} value={config.slippage_bps} onChange={(value) => updateConfig('slippage_bps', Number(value))} /></Field>
                      )}
                      <Field label="成交量参与上限" error={configIssues.volume_participation}><NumberInput unit="%" min={0} max={100} step={1} value={Number((config.volume_participation * 100).toFixed(0))} onChange={(value) => updateConfig('volume_participation', Number(value) / 100)} /></Field>
                    </FormGrid>
                  </div>
                )}
              </div>

              <div className="mt-3 flex flex-col gap-2 border-t pt-3 lg:flex-row lg:items-center lg:justify-between">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
                  <span>漏斗轮动 · {screener.boards.join(' / ')} · 每次清仓后重跑 L0→L4 取第一名换票（日线口径）</span>
                  {draftStatus !== 'idle' && (
                    <>
                      <i className="size-1 rounded-full bg-border" />
                      <span className={cn((draftStatus === 'error' || draftStatus === 'invalid') && 'text-destructive')}>
                        {draftStatus === 'saving' ? '配置保存中…'
                          : draftStatus === 'saved' ? '配置已自动保存'
                            : draftStatus === 'invalid' ? '配置有误，暂未自动保存，请按字段下方提示修正'
                              : `配置保存失败${draftError ? `：${draftError}` : '，改动仍保留在页面'}`}
                      </span>
                    </>
                  )}
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button variant="ghost" size="sm" className="h-8" type="button" onClick={() => void resetConfigDraft()} disabled={resettingDraft || jobActive}>
                    {resettingDraft ? <><Loader2 className="animate-spin" />正在恢复</> : <><RotateCcw />恢复默认</>}
                  </Button>
                  <Button variant="outline" size="sm" className="h-8" type="button" onClick={() => void applyToPaper()} disabled={applying || jobActive}>
                    {applying ? <><Loader2 className="animate-spin" />正在应用</> : <><ArrowDownToLine />应用到模拟盘</>}
                  </Button>
                  <Button size="sm" className="h-8" type="submit" disabled={jobActive}>
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
                      {JOB_RESUMABLE_STATUSES.includes(jobView.status) && <><Button variant="outline" size="sm" onClick={() => void resumeJob()} disabled={jobBusy}><Play />继续</Button><Button variant="ghost" size="sm" onClick={() => void dismissJob()} disabled={jobBusy}>放弃</Button></>}
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
                  <Button variant="outline" size="sm" type="button" onClick={closeDetail}><ArrowLeft />返回报告列表</Button>
                </div>
                <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
                  <div>
                    <div className="mb-1 flex items-center gap-2 font-mono text-[10px] tracking-wide text-muted-foreground"><Badge tone="indigo">{statusLabel(report.status)}</Badge><span>REPORT / {report.id.slice(0, 8).toUpperCase()}</span></div>
                    {renamingReport ? (
                      <div className="flex flex-wrap items-center gap-2">
                        <Input
                          value={renameValue}
                          onChange={(event) => setRenameValue(event.target.value)}
                          onKeyDown={(event) => {
                            if (event.key === 'Enter') { event.preventDefault(); void submitRename(); }
                            if (event.key === 'Escape') { event.preventDefault(); cancelRename(); }
                          }}
                          placeholder="报告名称"
                          aria-label="报告名称"
                          maxLength={200}
                          className="h-8 w-64 text-base font-semibold"
                          autoFocus
                        />
                        <Button variant="outline" size="sm" onClick={() => void submitRename()} disabled={renaming}>
                          {renaming ? <Loader2 className="animate-spin" /> : null}{renaming ? '保存中' : '保存'}
                        </Button>
                        <Button variant="ghost" size="sm" onClick={cancelRename} disabled={renaming}>取消</Button>
                      </div>
                    ) : (
                      <h2 className="group flex items-center gap-2 text-xl font-semibold tracking-tight">
                        {report.name}
                        <button
                          type="button"
                          onClick={startRename}
                          title="重命名报告"
                          aria-label="重命名报告"
                          className="rounded p-1 text-muted-foreground opacity-60 transition hover:bg-accent hover:text-foreground hover:opacity-100"
                        >
                          <Pencil className="size-3.5" />
                        </button>
                      </h2>
                    )}
                    <p className="mt-1 text-[12px] text-muted-foreground">生成于 {dateTime(report.created_at)} · 初始资金 {money(metrics.initial_cash, 0)}</p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button variant="outline" size="sm" onClick={applyReportToForm}><FlaskConical />带入回测</Button>
                    <Button variant="outline" size="sm" onClick={() => void exportCsv('trades')} disabled={exporting !== null}><ArrowDownToLine />{exporting === 'trades' ? '导出中' : '导出成交'}</Button>
                    <Button variant="outline" size="sm" onClick={() => void exportCsv('orders')} disabled={exporting !== null}><ArrowDownToLine />{exporting === 'orders' ? '导出中' : '导出委托'}</Button>
                    <Button variant="destructive" size="sm" onClick={() => setPendingDelete({ id: report.id, name: report.name })} disabled={deletingId === report.id}><Trash2 />{deletingId === report.id ? '删除中' : '删除报告'}</Button>
                  </div>
                </div>

                <MetricGrid columns={6}>
                  <Metric label="总收益率" value={signedPercent(metrics.total_return)} tone={metrics.total_return >= 0 ? 'positive' : 'negative'} sub={<span className="whitespace-normal break-words">{`期末权益 ${money(metrics.final_equity)}`}{metrics.profit_withdrawn ? ` · 盈利出金 ${money(metrics.profit_withdrawn)}` : ''}</span>} />
                  <Metric label="年化收益率（TWR 口径）" value={signedPercent(cf.annualized_return)} tone={cf.annualized_return >= 0 ? 'positive' : 'negative'} title={metrics.xirr_annualized != null ? `XIRR ${signedPercent(metrics.xirr_annualized)}：资金加权年化收益率（按每笔资金实际进出时点折算，反映真实资金的时间价值；与 TWR 时间加权口径互为补充）` : undefined} />
                  <Metric label="最大回撤（TWR 口径）" value={percent(Math.abs(cf.max_drawdown))} tone="negative" title="TWR 口径剔除提现台阶，只反映真实投资亏损的回撤；权益曲线图上的「回撤（含提现）」含提现造成的权益台阶，两者数值会不同（本报告：不含提现 16.75% / 含提现 23.50%）" sub="剔除提现台阶" />
                  <Metric label="夏普比率（TWR 口径）" value={number(cf.sharpe_ratio)} title="TWR 口径夏普＝年化收益 / 年化波动率，剔除提现台阶；含提现口径夏普见「本轮风险诊断」。" />
                  {(() => {
                    // 三口径胜率并列：账户（pnl，含 ETF）/ 选股（stock_pnl，剔 ETF）/
                    // ETF（beta 腿自己的平仓回合）。分母各不相同，sub 里分别标注。
                    const parts: string[] = [`${metrics.closed_rounds} / ${metrics.total_rounds} 轮已结束`];
                    if (metrics.stock_win_rate != null) {
                      parts.push(`选股 ${percent(metrics.stock_win_rate)}`);
                    }
                    if (metrics.beta_win_rate != null) {
                      parts.push(`ETF ${percent(metrics.beta_win_rate)}（${integer(metrics.beta_sell_wins ?? 0)}/${integer(metrics.beta_sell_rounds ?? 0)} 回合）`);
                    }
                    return (
                      <Metric
                        label="轮次胜率"
                        value={percent(metrics.win_rate)}
                        title="账户口径胜率（按轮次 pnl，含 ETF 腿盯市盈亏）。ETF beta 腿跨轮独立持有，其盈亏会记进轮次 pnl——故并列给出剔 ETF 的「选股胜率」与 ETF 腿自己的「交易回合胜率」，三者分母不同。"
                        sub={<span className="whitespace-normal break-words">{parts.join(' · ')}</span>}
                      />
                    );
                  })()}
                  <Metric label="净利润" value={signedMoney(metrics.net_profit)} tone={metrics.net_profit >= 0 ? 'positive' : 'negative'} sub={<span className="whitespace-normal break-words">{`${integer(metrics.total_trades)} 笔成交 · PF ${number(metrics.profit_factor)}`}{metrics.repo_income_total ? ` · 逆回购 ${signedMoney(metrics.repo_income_total)}` : ''}</span>} />
                </MetricGrid>

                {/* 买入持有基准：单独成框，给出各基准区间收益与相对策略的超额 */}
                {(metrics.buy_hold_benchmarks?.length || metrics.buy_hold_return != null) && (
                  <div className="mb-3 rounded-xl border bg-card px-3.5 py-3">
                    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                      <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-foreground"><BarChart3 className="size-3.5 text-muted-foreground" />买入持有基准</span>
                      {(metrics.buy_hold_benchmarks?.length ? metrics.buy_hold_benchmarks : []).map((b) => {
                        const excess = metrics.total_return - b.return;
                        return (
                          <span key={b.symbol} className="inline-flex items-baseline gap-1.5 text-[11px]">
                            <span className="text-muted-foreground">{b.symbol === '组合' ? '双创' : b.label}</span>
                            <b className={cn('font-mono', b.return >= 0 ? 'text-success' : 'text-destructive')}>{signedPercent(b.return)}</b>
                            <span className="text-[10px] text-muted-foreground">超额 {signedPercent(excess)}</span>
                          </span>
                        );
                      })}
                      {!metrics.buy_hold_benchmarks?.length && metrics.buy_hold_return != null && (
                        <span className="inline-flex items-baseline gap-1.5 text-[11px]">
                          <span className="text-muted-foreground">基准</span>
                          <b className={cn('font-mono', metrics.buy_hold_return >= 0 ? 'text-success' : 'text-destructive')}>{signedPercent(metrics.buy_hold_return)}</b>
                          <span className="text-[10px] text-muted-foreground">超额 {signedPercent(metrics.total_return - metrics.buy_hold_return)}</span>
                        </span>
                      )}
                      <span className="ml-auto text-[10px] text-muted-foreground">同期买入持有 · 正超额＝策略跑赢基准</span>
                    </div>
                  </div>
                )}

                <div className="mb-3 grid gap-3 xl:grid-cols-[minmax(0,1fr)_300px] xl:grid-rows-[auto_auto]">
                  <Panel className="xl:col-start-1 xl:row-start-1" title="权益与风险轨迹" description="账户权益、实际投入与回撤随时间的变化" action={<div className="flex items-center gap-2 text-[10px] text-muted-foreground"><span className="inline-flex items-center gap-1"><i className="size-1.5 rounded-full bg-primary" />账户权益</span><span className="inline-flex items-center gap-1"><i className="size-1.5 rounded-full bg-[#64748b]" />账户权益（含出金）</span><span className="inline-flex items-center gap-1"><i className="size-1.5 rounded-full bg-muted-foreground" />实际投入</span><span className="inline-flex items-center gap-1"><i className="size-1.5 rounded-full bg-success" />回撤</span></div>}>
                    <div className="h-[320px] w-full">
                      {report.equity_curve.length > 0 ? (
                        <ResponsiveContainer width="100%" height="100%">
                          <ComposedChart data={report.equity_curve} margin={{ top: 12, right: 4, left: 4, bottom: 0 }}>
                            <defs><linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#5046d8" stopOpacity={0.14} /><stop offset="100%" stopColor="#5046d8" stopOpacity={0} /></linearGradient></defs>
                            <CartesianGrid stroke="#ececf1" strokeDasharray="3 4" vertical={false} />
                            <XAxis dataKey="date" tickFormatter={(value: string) => value.slice(5, 10)} tick={{ fill: '#85858f', fontSize: 11 }} axisLine={false} tickLine={false} minTickGap={48} dy={8} />
                            <YAxis yAxisId="money" tickFormatter={(value: number) => `${(value / 10000).toFixed(0)}万`} tick={{ fill: '#85858f', fontSize: 11 }} axisLine={false} tickLine={false} width={52} domain={['auto', 'auto']} />
                            <YAxis yAxisId="drawdown" orientation="right" tickFormatter={(value: number) => `${(value * 100).toFixed(0)}%`} tick={{ fill: '#9a9aa3', fontSize: 11 }} axisLine={false} tickLine={false} width={42} domain={['auto', 0]} />
                            <Tooltip content={({ active, payload, label }) => active && payload?.length ? <div className="rounded-lg border bg-popover p-3 text-xs shadow-md"><strong className="block text-foreground">{label}</strong>{[['账户权益', money(payload[0].payload.equity)], ['账户权益（含出金）', money(payload[0].payload.equity_incl_withdrawal)], ['实际投入', money(payload[0].payload.invested)], ['可用现金', money(payload[0].payload.cash)], ['持仓市值', money(payload[0].payload.market_value)], ['回撤（含提现）', percent(payload[0].payload.drawdown)], ['持仓数量', integer(payload[0].payload.position_qty)]].map(([k, v]) => <div key={String(k)} className="mt-1 flex items-center justify-between gap-4"><span className="text-muted-foreground">{k}</span><b className="font-mono text-foreground">{v}</b></div>)}</div> : null} />
                            <Area yAxisId="money" type="monotone" dataKey="equity" stroke="#5046d8" strokeWidth={2.2} fill="url(#equityFill)" activeDot={{ r: 4, fill: '#5046d8', stroke: '#fff', strokeWidth: 2 }} />
                             <Line yAxisId="money" type="monotone" dataKey="equity_incl_withdrawal" stroke="#64748b" strokeWidth={1.6} strokeDasharray="6 4" dot={false} />
                            <Line yAxisId="money" type="monotone" dataKey="invested" stroke="#8b8b98" strokeWidth={1.5} dot={false} strokeDasharray="5 4" />
                            <Line yAxisId="drawdown" type="stepAfter" dataKey="drawdown" stroke="#138064" strokeWidth={1.4} dot={false} />
                          </ComposedChart>
                        </ResponsiveContainer>
                      ) : <EmptyState compact title="暂无权益曲线" description="报告未返回可绘制的时序数据。" />}
                    </div>
                    <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[10px] text-muted-foreground"><span>左轴：资金（万元） · 右轴：回撤（%，含提现）</span><span>期末 {report.equity_curve[report.equity_curve.length - 1]?.date || '—'}</span></div>
                  </Panel>

                  <Panel className="xl:col-start-2 xl:row-start-1 xl:row-span-2" contentClassName="flex-1 flex flex-col" title="本轮风险诊断" description="从资金占用到尾部风险的横向检查" action={<ShieldAlert className="size-4" />}>
                    <div className="flex flex-1 flex-col gap-1.5">
                      {[
                        { icon: WalletCards, label: '最大资金投入', value: money(metrics.max_invested), note: '峰值占用' },
                        { icon: TrendingDown, label: '最大浮亏', value: money(metrics.max_float_loss), note: '未实现口径', tone: 'text-success' },
                        { icon: RotateCcw, label: '做 T 净贡献', value: signedMoney(metrics.t_net_profit), note: `${integer(metrics.t_trade_count)} 笔`, tone: metrics.t_net_profit >= 0 ? 'text-destructive' : 'text-success' },
                        ...(betaInfo?.enabled ? [{ icon: Layers3, label: 'ETF beta 净贡献', value: signedMoney(Number(betaInfo.pnl ?? 0)), note: `${integer(Number(betaInfo.trade_count ?? 0))} 笔 · ${betaInfo.exit_reason || '持仓中'}`, tone: Number(betaInfo.pnl ?? 0) >= 0 ? 'text-destructive' : 'text-success' }] : []),
                        { icon: CircleDollarSign, label: '累计交易费用', value: money(metrics.total_fees), note: '五項分项相加，滑点另计' },
                        ...(metrics.total_withdrawn != null ? [{ icon: Landmark, label: '累计提现', value: money(metrics.total_withdrawn), note: `${metrics.withdrawn_count ? `${integer(metrics.withdrawn_count)} 笔` : '未提取'}${metrics.profit_withdrawn != null && metrics.profit_withdrawn > 0 ? ' · 盈利' : ''}`, tone: (metrics.total_withdrawn ?? 0) > 0 ? 'text-destructive' : undefined }] : []),
                        { icon: Activity, label: '平均资金敞口', value: percent(metrics.exposure), note: '权益占比' },
                      ].map(({ icon: Icon, label, value, note, tone }) => (
                        <div key={label} className="flex flex-1 flex-col justify-center rounded-lg border bg-card px-2.5 py-2">
                          <div className="flex items-center gap-2">
                            <span className="grid size-7 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground"><Icon className="size-3.5" /></span>
                            <div className="min-w-0 flex-1"><small className="block text-[10px] text-muted-foreground">{label}</small><strong className={cn('block font-mono text-[13px]', tone)}>{value}</strong></div>
                          </div>
                          {note && <div className="mt-1 whitespace-normal break-words text-right text-[10px] text-muted-foreground">{note}</div>}
                        </div>
                      ))}
                      <div className="mt-1 flex items-center gap-2 pt-1 text-[10px] font-medium text-muted-foreground"><span className="h-px flex-1 bg-border" />风险调整<span className="h-px flex-1 bg-border" /></div>
                      {([
                        { icon: Gauge, label: 'Calmar', value: number(cf.calmar_ratio), note: '年化收益 / 最大回撤（TWR）' },
                        { icon: Waves, label: '波动率', value: percent(cf.volatility), note: 'TWR 日收益年化' },
                        ...(cfa?.sharpe_ratio != null ? [{ icon: Squirrel, label: '夏普（含提现口径）', value: number(metrics.sharpe_ratio), note: '对比顶部 TWR 口径' }] : []),
                      ] as { icon: React.ComponentType<{ className?: string }>; label: string; value: string; note: string; tone?: string }[]).map(({ icon: Icon, label, value, note, tone }) => (
                        <div key={label} className="flex flex-1 flex-col justify-center rounded-lg border bg-card px-2.5 py-2">
                          <div className="flex items-center gap-2">
                            <span className="grid size-7 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground"><Icon className="size-3.5" /></span>
                            <div className="min-w-0 flex-1"><small className="block text-[10px] text-muted-foreground">{label}</small><strong className={cn('block font-mono text-[13px]', tone)}>{value}</strong></div>
                          </div>
                          {note && <div className="mt-1 whitespace-normal break-words text-right text-[10px] text-muted-foreground">{note}</div>}
                        </div>
                      ))}
                    </div>
                    <div className="mt-2 flex items-start gap-1.5 rounded-lg bg-muted/60 px-2.5 py-2 text-[10px] leading-4 text-muted-foreground"><Gauge className="mt-0.5 size-3 shrink-0" /><span>风险表现需结合数据粒度、成交假设与样本区间共同判断。</span></div>
                  </Panel>

                {(() => {
                  const rb = metrics.robustness;
                  const u = metrics.underwater;
                  if (rb == null && u == null) return null;
                  const tiles: { label: string; value: string; note?: string; tone?: string }[] = [];
                  if (rb != null) {
                    tiles.push({ label: '稳健版净利润', value: signedMoney(rb.net_profit), note: `剔最佳月 ${signedMoney(rb.excluded_best_month_pnl)} + 最大3笔 ${signedMoney(rb.excluded_top3_win_pnl)}`, tone: rb.net_profit >= 0 ? 'text-destructive' : 'text-success' });
                    tiles.push({ label: '稳健版总收益', value: signedPercent(rb.total_return), note: `vs 完整 ${signedPercent(metrics.total_return)}` });
                    tiles.push({ label: '稳健版年化', value: signedPercent(rb.annualized_return), note: `vs 完整 ${signedPercent(metrics.annualized_return)}` });
                  }
                  if (u != null) {
                    tiles.push({ label: '最长水下时长', value: `${integer(u.longest_underwater_days)} 交易日`, note: `${shortDate(u.longest_underwater_start)} ~ ${shortDate(u.longest_underwater_end)} · ${integer(u.longest_underwater_calendar_days)} 自然日` });
                    tiles.push({ label: '水下占比', value: percent(u.underwater_ratio), note: `${integer(u.underwater_days)} / ${integer(report.equity_curve.length)} 交易日` });
                    tiles.push({ label: '当前状态', value: u.currently_underwater ? `水下第 ${integer(u.current_drawdown_days)} 日` : '处于峰值上方', tone: u.currently_underwater ? 'text-success' : 'text-foreground' });
                  }
                  const dd = u?.drawdown_depth_distribution ?? null;
                  type DepthKey = 'lte_2pct' | '2_to_5pct' | '5_to_10pct' | 'gt_10pct';
                  // 四档按浅→深排列；某档 0 天仍显示（让人看到「没有深度回撤」也是信息）。
                  const depthRows: { key: DepthKey; label: string; tone: string; bar: string; bucket: DepthBucket }[] = dd
                    ? ([
                        { key: 'lte_2pct', label: '≤2%', tone: 'text-success', bar: 'bg-success' },
                        { key: '2_to_5pct', label: '2–5%', tone: 'text-foreground', bar: 'bg-muted-foreground' },
                        { key: '5_to_10pct', label: '5–10%', tone: 'text-warning', bar: 'bg-warning' },
                        { key: 'gt_10pct', label: '>10%', tone: 'text-destructive', bar: 'bg-destructive' },
                      ] as { key: DepthKey; label: string; tone: string; bar: string }[])
                      .map((row) => ({ ...row, bucket: dd[row.key] }))
                    : [];
                  return (
                    <Panel className="xl:col-start-1 xl:row-start-2" title="稳健性与水下时长" description="剔除极端项后的收益成色 · 净值曲线在水下的时长分布" action={<ShieldCheck className="size-4" />}>
                      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                        {tiles.map((tile) => (
                          <div key={tile.label} className="rounded-lg border bg-card px-2.5 py-2">
                            <small className="block text-[10px] text-muted-foreground">{tile.label}</small>
                            <strong className={cn('block font-mono text-[13px]', tile.tone)}>{tile.value}</strong>
                            <span className="mt-0.5 block text-[10px] leading-4 text-muted-foreground">{tile.note}</span>
                          </div>
                        ))}
                      </div>
                      {depthRows.length > 0 && (
                        <div className="mt-3 rounded-lg border bg-card px-2.5 py-2">
                          <div className="mb-1.5 flex items-center justify-between gap-2">
                            <small className="text-[10px] text-muted-foreground">水下深度分布 · 水下日按回撤深度拆分</small>
                            <span className="text-[10px] text-muted-foreground">共 {integer(u!.underwater_days)} 个水下交易日</span>
                          </div>
                          <div className="space-y-1">
                            {depthRows.map(({ key, label, tone, bar, bucket }) => {
                              const days = bucket.days;
                              // 条宽按「占全部交易日」比例，直观看到每档在整体里的分量。
                              const widthPct = Math.max(days > 0 ? 1 : 0, bucket.pct_of_trading * 100);
                              return (
                                <div key={key} className="flex items-center gap-2">
                                  <span className={cn('w-12 shrink-0 text-right font-mono text-[10px]', tone)}>{label}</span>
                                  <div className="h-1.5 flex-1 overflow-hidden rounded bg-muted">
                                    <div className={cn('h-full rounded', bar)} style={{ width: `${widthPct}%` }} />
                                  </div>
                                  <span className="min-w-24 shrink-0 whitespace-nowrap text-right font-mono text-[10px] text-muted-foreground">
                                    {integer(days)} 天 · {percent(bucket.pct_of_underwater)}
                                  </span>
                                </div>
                              );
                            })}
                          </div>
                          <div className="mt-1.5 text-[10px] leading-4 text-muted-foreground">
                            占比口径：条长=占全部交易日，右侧 %=占全部水下日。深档越少说明「水下」多为峰值附近浅晃，而非深度回撤。
                          </div>
                        </div>
                      )}
                    </Panel>
                  );
                })()}
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
                      <TableHeader><TableRow><TableHead>月份</TableHead><TableHead className="text-right">净盈亏</TableHead>{hasRepoIncome && <TableHead className="text-right">逆回购收益</TableHead>}<TableHead className="text-right">收益率</TableHead><TableHead className="text-right">交易日</TableHead><TableHead className="text-right">当月最大回撤</TableHead><TableHead className="text-right">累计收益率</TableHead></TableRow></TableHeader>
                      <TableBody>
                        {monthRows.map((item) => (
                          <TableRow
                            key={item.month}
                            onClick={hasRepoIncome ? () => { setRepoPage(1); setRepoMonth(item.month); } : undefined}
                            className={hasRepoIncome ? 'cursor-pointer' : undefined}
                            title={hasRepoIncome ? '点击查看该月逆回购每日明细' : undefined}
                          >
                            <TableCell><strong className="font-mono text-[12px]">{item.month}</strong></TableCell>
                            <TableCell className={cn('text-right font-mono text-[12px]', item.pnl >= 0 ? 'text-destructive' : 'text-success')}>{signedMoney(item.pnl)}</TableCell>
                            {hasRepoIncome && <TableCell className="text-right font-mono text-[12px] text-muted-foreground">{signedMoney(item.repo_income)}</TableCell>}
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

                {/* 逆回购每日明细弹窗：点每月盈亏表的月份行打开，按自然月后台分页下发逐日计提。 */}
                <Dialog open={repoMonth !== null} onOpenChange={(open) => { if (!open) setRepoMonth(null); }}>
                  <DialogContent className="max-w-2xl">
                    <DialogHeader>
                      <DialogTitle>{repoMonth} 逆回购每日明细</DialogTitle>
                      <DialogDescription>闲置资金逆回购盘后逐日计提 · 当日收益已计入当月净盈亏</DialogDescription>
                    </DialogHeader>
                    <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg bg-muted/40 px-2.5 py-1.5 text-[11px] text-muted-foreground">
                      <span>当月合计收益 <strong className={cn('font-mono', (monthly.find((m) => m.month === repoMonth)?.repo_income ?? 0) > 0 ? 'text-destructive' : 'text-foreground')}>{signedMoney(monthly.find((m) => m.month === repoMonth)?.repo_income)}</strong></span>
                      <span>计提天数 <strong className="font-mono text-foreground">{integer(repoData?.total ?? 0)}</strong></span>
                    </div>
                    <Table>
                      <TableHeader><TableRow><TableHead>日期</TableHead><TableHead className="text-right">闲置资金</TableHead><TableHead className="text-right">年化收益率</TableHead><TableHead className="text-right">当日收益</TableHead></TableRow></TableHeader>
                      <TableBody>
                        {(repoData?.items.length ?? 0) === 0 ? (
                          <TableEmpty colSpan={4} message="该月没有逆回购计提日（未开启或闲置资金为 0）" />
                        ) : repoData?.items.map((row) => (
                          <TableRow key={row.date}>
                            <TableCell className="font-mono text-[12px]">{shortDate(row.date)}</TableCell>
                            <TableCell className="text-right font-mono text-[12px]">{money(row.idle_cash, 0)}</TableCell>
                            <TableCell className="text-right font-mono text-[12px] text-muted-foreground">{percent(row.rate)}</TableCell>
                            <TableCell className={cn('text-right font-mono text-[12px]', row.income > 0 ? 'text-destructive' : 'text-success')}>{signedMoney(row.income)}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                    {repoData && repoData.total > repoData.page_size && (
                      <div className="pt-3"><PaginationBar page={repoData.page} pageSize={repoData.page_size} total={repoData.total} totalPages={repoData.total_pages} onPageChange={setRepoPage} disabled={repoLoading} /></div>
                    )}
                  </DialogContent>
                </Dialog>

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
                  <Panel
                    className="mb-3"
                    title="换票轨迹"
                    description={`每次清仓后重跑选股漏斗（L0→L4）取当时第一名；连续同因空仓合并为区间，可展开看逐轮决策日志${trailSummary ? ` · 共 ${integer(trailSummary.screens)} 次筛选` : ''}`}
                    action={<div className="flex items-center gap-2">
                      <Button variant="outline" size="sm" className="h-7" type="button" onClick={() => void exportCsv('trail')} disabled={exporting !== null || !report}>
                        <ArrowDownToLine />{exporting === 'trail' ? '导出中' : '导出轨迹'}
                      </Button>
                      <Layers3 className="size-4" />
                    </div>}
                    flush
                  >
                    {trailSummary && (
                      <div className="border-b bg-muted/20 px-4 py-2.5">
                        <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 text-[11px] text-muted-foreground">
                          <span>筛选 <strong className="font-mono text-foreground">{integer(trailSummary.screens)}</strong> 次</span>
                          <span>换票 <strong className="font-mono text-foreground">{integer(trailSummary.rounds)}</strong> 轮（已结束 {integer(trailSummary.closed_rounds)}）</span>
                          <span>空仓 <strong className="font-mono text-foreground">{integer(trailSummary.waiting_days)}</strong> 天 · {percent(trailSummary.waiting_ratio)}</span>
                          {(trailSummary.beta_holding_days ?? 0) > 0 && <span>ETF持仓 <strong className="font-mono text-foreground">{integer(trailSummary.beta_holding_days ?? 0)}</strong> 天{trailSummary.beta_pnl != null ? ` · ${signedMoney(trailSummary.beta_pnl)}` : ''}</span>}
                          <span>持仓利用率 <strong className="font-mono text-foreground">{percent(trailSummary.holding_ratio)}</strong>{trailSummary.covered_ratio != null ? <span className="text-muted-foreground"> · 覆盖 {percent(trailSummary.covered_ratio)}</span> : null}</span>
                          <span>轮次胜率 <strong className="font-mono text-foreground">{percent(trailSummary.win_rate)}</strong></span>
                          <span>平均单轮 <strong className={cn('font-mono', (trailSummary.avg_return ?? 0) >= 0 ? 'text-destructive' : 'text-success')}>{signedPercent(trailSummary.avg_return)}</strong></span>
                          {trailSummary.avg_entry_delay_days != null && <span>平均建仓延迟 <strong className="font-mono text-foreground">{number(trailSummary.avg_entry_delay_days, 1)}</strong> 天</span>}
                          {trailSummary.longest_wait && (
                            <span>最长空仓 <strong className="font-mono text-foreground">{integer(trailSummary.longest_wait.days)}</strong> 天（{shortDate(trailSummary.longest_wait.start_date)} ~ {shortDate(trailSummary.longest_wait.end_date)}）</span>
                          )}
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-muted-foreground">
                          <span>空仓构成：L0 关门 {integer(trailSummary.gate_closed_days)} 天 · 无候选 {integer(trailSummary.no_candidate_days)} 天{trailStageSummary ? `（${trailStageSummary}）` : ''}</span>
                          {trailExitSummary && <span>退出原因：{trailExitSummary}</span>}
                        </div>
                      </div>
                    )}
                    <Table>
                      <TableHeader><TableRow>
                        <TableHead className="w-8" />
                        <TableHead>轮次 / 区间</TableHead>
                        <TableHead>标的 / 板块</TableHead>
                        <TableHead>门 · 状态</TableHead>
                        <TableHead className="text-right">持仓</TableHead>
                        <TableHead className="text-right">收益 / 盈亏</TableHead>
                      </TableRow></TableHeader>
                      <TableBody>
                        {rotationTrail.map((item, index) => {
                          const isBeta = item.kind === 'BETA';
                          const isBetaSpan = item.kind === 'BETA_SPAN';
                          const isWaitingSpan = item.kind === 'WAITING_SPAN';
                          const isSpan = isWaitingSpan || isBetaSpan;
                          const trailKey = isSpan
                            ? `span-${item.start_date}-${index}`
                            : item.round || `row-${item.select_date}-${index}`;
                          const expanded = expandedTrails.has(trailKey);
                          const status = String(item.status || '').toUpperCase();
                          const gateBoards = Object.entries(item.gate?.boards ?? {});
                          const candidates = item.candidates ?? [];
                          // 轮次盈亏拆分（个股腿 / ETF 腿 / 轮内提现）：仅在读时派生成功后展示。
                          const pnlSplit = !isSpan && item.stock_pnl != null && item.beta_pnl != null
                            ? `个股 ${signedMoney(item.stock_pnl)} · ETF ${signedMoney(item.beta_pnl)}${(item.withdraw_in_round ?? 0) > 0 ? ` · 提现 ${money(item.withdraw_in_round)}` : ''}`
                            : null;
                          return (
                            <Fragment key={trailKey}>
                              <TableRow className={isSpan ? 'bg-muted/25' : ''}>
                                <TableCell><button className="grid size-6 place-items-center rounded text-muted-foreground hover:bg-muted" onClick={() => toggleTrail(trailKey)} aria-label={expanded ? '收起本轮决策日志' : '展开本轮决策日志'} title={expanded ? '收起决策日志' : '查看本轮决策日志'}>{expanded ? <ChevronUp className="size-3.5" /> : <ChevronDown className="size-3.5" />}</button></TableCell>
                                <TableCell className="whitespace-nowrap">
                                  {isBeta || isBetaSpan ? (
                                    <div><Badge tone="indigo">ETF持仓</Badge><small className="mt-0.5 block font-mono text-[10px] text-muted-foreground">{shortDate(item.start_date)} ~ {shortDate(item.end_date)}</small></div>
                                  ) : isSpan ? (
                                    <div><Badge tone="amber">空仓</Badge><small className="mt-0.5 block font-mono text-[10px] text-muted-foreground">{shortDate(item.start_date)} ~ {shortDate(item.end_date)}{isWaitingSpan && item.days != null ? `（${integer(item.days)} 日）` : ''}</small></div>
                                  ) : (
                                    <div><strong className="font-mono text-[11px] text-primary">{item.round || '待建仓'}</strong><small className="mt-0.5 block font-mono text-[10px] text-muted-foreground">{shortDate(item.select_date)}</small></div>
                                  )}
                                </TableCell>
                                <TableCell>
                                  {isBeta || isBetaSpan ? (
                                    <div><span className="text-[11px] text-muted-foreground">ETF持仓覆盖</span><small className="mt-0.5 block text-[10px] text-muted-foreground">{item.beta_symbol ? displaySymbol(item.beta_symbol) : 'beta 腿'}</small></div>
                                  ) : isSpan ? (
                                    <div><span className="text-[11px] text-muted-foreground">{item.reason === 'gate_closed' ? 'L0 关门' : '无候选'}</span><small className="mt-0.5 block text-[10px] text-muted-foreground">{trailWaitReason(item)}</small></div>
                                  ) : item.symbol ? (
                                    <div className="max-w-[13rem]">
                                      <div className="flex items-baseline gap-1"><strong className="font-mono text-[12px]" title={item.symbol}>{displaySymbol(item.symbol)}</strong><span className="truncate text-[11px] text-muted-foreground" title={item.name || ''}>{item.name || '—'}</span></div>
                                      <small className="mt-0.5 block text-[10px] text-muted-foreground">{item.board || '—'}{item.chg20d_at_select != null ? <b className={cn('ml-1 font-mono', item.chg20d_at_select >= 0 ? 'text-destructive' : 'text-success')}>{signedPercent(item.chg20d_at_select)}</b> : null}</small>
                                    </div>
                                  ) : <span className="text-[11px] text-muted-foreground">空仓等待</span>}
                                </TableCell>
                                <TableCell className="whitespace-nowrap">
                                  {isBeta || isBetaSpan || isSpan ? (
                                    <Badge tone={item.gate_open ? 'green' : 'amber'}>{item.gate_open ? '开门' : '关门'}</Badge>
                                  ) : (
                                    <div>
                                      <div className="flex items-center gap-1">
                                        <Badge tone={status === 'CLOSED' ? 'green' : status === 'OPEN' ? 'indigo' : 'amber'}>{status === 'CLOSED' ? '已结束' : status === 'OPEN' ? (item.entry_date ? '持仓中' : '待建仓') : '空仓等待'}</Badge>
                                        <Badge tone={item.gate_open ? 'green' : 'amber'}>{item.gate_open ? '开门' : '关门'}</Badge>
                                      </div>
                                      {item.close_reason && <small className="mt-0.5 block text-[10px] text-muted-foreground">{item.close_reason}</small>}
                                    </div>
                                  )}
                                </TableCell>
                                <TableCell className="text-right whitespace-nowrap">
                                  {isSpan ? <span className="text-[11px] text-muted-foreground">—</span> : item.entry_date || item.exit_date ? (
                                    <div>
                                      <span className="font-mono text-[11px]">{item.entry_date ? shortDate(item.entry_date) : '—'} → {item.exit_date ? shortDate(item.exit_date) : '持仓中'}</span>
                                      {item.holding_days != null && <small className="mt-0.5 block text-[10px] text-muted-foreground">持有 {integer(item.holding_days)} 日</small>}
                                    </div>
                                  ) : <span className="text-[11px] text-muted-foreground">—</span>}
                                </TableCell>
                                <TableCell className="text-right whitespace-nowrap">
                                  {isWaitingSpan ? <span className="text-[11px] text-muted-foreground">—</span> : (
                                    <div>
                                      <span className={cn('font-mono text-[12px]', item.return_rate != null && (item.return_rate >= 0 ? 'text-destructive' : 'text-success'))}>{item.return_rate != null ? signedPercent(item.return_rate) : '—'}</span>
                                      <small className={cn('mt-0.5 block font-mono text-[11px]', item.pnl != null && (item.pnl >= 0 ? 'text-destructive' : 'text-success'))}>{item.pnl != null ? signedMoney(item.pnl) : '—'}</small>
                                      {pnlSplit && <small className="mt-0.5 block text-[9px] leading-3 text-muted-foreground">{pnlSplit}</small>}
                                    </div>
                                  )}
                                </TableCell>
                              </TableRow>
                              {expanded && (
                                <TableRow className="bg-muted/20">
                                  <TableCell />
                                  <TableCell colSpan={5}>
                                    <div className="py-1">
                                      <div className="flex flex-wrap gap-1.5">
                                        {[{ tag: '全市场', value: item.funnel?.universe }, { tag: 'L0 大盘门', value: item.funnel?.after_l0 }, { tag: 'L1 超跌', value: item.funnel?.after_l1 }, { tag: 'L2 否决', value: item.funnel?.after_l2 }, { tag: 'L3 容量', value: item.funnel?.after_l3 }, { tag: '可用候选', value: item.funnel?.eligible }].map((stage) => (
                                          <span key={stage.tag} className="rounded-md border bg-card px-2 py-1 text-[10px]"><small className="mr-1 text-muted-foreground">{stage.tag}</small><strong className="font-mono">{stage.value ?? '—'}</strong></span>
                                        ))}
                                      </div>
                                      {gateBoards.length > 0 && (
                                        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                                          <small className="text-muted-foreground">L0 板块门：</small>
                                          {gateBoards.map(([board, entry]) => (
                                            <span key={board} className="rounded-md border bg-card px-2 py-0.5 text-[10px]">
                                              {board} <strong className={cn('font-mono', (entry.ret20 ?? 0) < 0 ? 'text-success' : 'text-destructive')}>{entry.ret20 != null ? signedPercent(entry.ret20) : '—'}</strong>
                                              <small className="ml-1 text-muted-foreground">{entry.open ? '开门' : '关门'}{entry.bypass ? '·超跌旁路' : ''}</small>
                                            </span>
                                          ))}
                                        </div>
                                      )}
                                      {!isSpan && candidates.length > 0 && (
                                        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                                          <small className="text-muted-foreground">候选前三：</small>
                                          {candidates.map((candidate) => (
                                            <span key={candidate.code} className="rounded-md border bg-card px-2 py-0.5 text-[10px]">
                                              <strong className="font-mono">{displaySymbol(candidate.code)}</strong> {candidate.name}
                                              <span className={cn('ml-1 font-mono', candidate.chg20d >= 0 ? 'text-destructive' : 'text-success')}>{signedPercent(candidate.chg20d)}</span>
                                            </span>
                                          ))}
                                        </div>
                                      )}
                                      {!isSpan && (item.entry_delay_days != null || item.max_invested != null || item.t_net_profit != null || item.stock_pnl != null || item.beta_pnl != null) && (
                                        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-muted-foreground">
                                          {item.entry_delay_days != null && <span>建仓延迟 <b className="font-mono text-foreground">{integer(item.entry_delay_days)}</b> 天</span>}
                                          {item.max_invested != null && <span>峰值投入 <b className="font-mono text-foreground">{money(item.max_invested)}</b></span>}
                                          {item.t_net_profit != null && <span>做 T 净贡献 <b className="font-mono text-foreground">{signedMoney(item.t_net_profit)}</b></span>}
                                          {item.stock_pnl != null && <span>个股腿盈亏 <b className={cn('font-mono', item.stock_pnl >= 0 ? 'text-destructive' : 'text-success')}>{signedMoney(item.stock_pnl)}</b></span>}
                                          {item.beta_pnl != null && <span>ETF 腿盈亏 <b className={cn('font-mono', item.beta_pnl >= 0 ? 'text-destructive' : 'text-success')}>{signedMoney(item.beta_pnl)}</b></span>}
                                          {(item.withdraw_in_round ?? 0) > 0 && <span>轮内提现 <b className="font-mono text-foreground">{money(item.withdraw_in_round)}</b></span>}
                                        </div>
                                      )}
                                      {!isSpan && item.close_signal_price != null && (
                                        <div className="mt-1.5 rounded-md border bg-card px-2.5 py-2 text-[10px]">
                                          <small className="block text-muted-foreground">止盈执行诊断（信号 → 成交）</small>
                                          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
                                            {item.close_signal_trigger != null && <span><span className="text-muted-foreground">触发 </span><b className="font-mono text-foreground">{item.close_signal_trigger}</b></span>}
                                            <span><span className="text-muted-foreground">信号价 </span><b className="font-mono text-foreground">{money(item.close_signal_price)}</b>{item.close_signal_return != null && <b className="ml-1 font-mono text-foreground">{signedPercent(item.close_signal_return)}</b>}</span>
                                            {item.close_signal_time != null && <span><span className="text-muted-foreground">信号时间 </span><b className="font-mono text-foreground">{shortDate(item.close_signal_time)}</b></span>}
                                            {item.close_fill_price != null && <span><span className="text-muted-foreground">成交价 </span><b className="font-mono text-foreground">{money(item.close_fill_price)}</b></span>}
                                            {item.close_fill_time != null && <span><span className="text-muted-foreground">成交时间 </span><b className="font-mono text-foreground">{shortDate(item.close_fill_time)}</b></span>}
                                            {item.close_execution_gap != null && <span><span className="text-muted-foreground">执行落差 </span><b className={cn('font-mono', item.close_execution_gap < 0 ? 'text-success' : 'text-muted-foreground')}>{signedPercent(item.close_execution_gap)}</b></span>}
                                            {item.close_round_return != null && <span><span className="text-muted-foreground">轮次收益 </span><b className="font-mono text-foreground">{signedPercent(item.close_round_return)}</b><small className="ml-1 text-muted-foreground">含 ETF 腿与轮内提现</small></span>}
                                            {item.close_return_gap != null && <span><span className="text-muted-foreground">总落差 </span><b className={cn('font-mono', item.close_return_gap < 0 ? 'text-success' : 'text-muted-foreground')}>{signedPercent(item.close_return_gap)}</b></span>}
                                          </div>
                                        </div>
                                      )}
                                      {item.screen_warnings && item.screen_warnings.length > 0 ? <ul className="mt-1.5 list-disc space-y-0.5 pl-4 text-[10px] text-muted-foreground">{item.screen_warnings.map((warning, wIndex) => <li key={wIndex}>{warning}</li>)}</ul> : <p className="mt-1.5 text-[10px] text-muted-foreground">{isBeta || isBetaSpan ? '该 ETF 持仓区间无额外决策提示。' : isSpan ? '该空仓区间无额外决策提示。' : '本轮选股无逐轮决策提示。'}</p>}
                                      {!isSpan && item.symbol && (
                                        <Button variant="outline" size="sm" className="mt-1.5 h-7" type="button" onClick={() => focusRoundTrades(String(item.symbol))}>
                                          <ArrowDownToLine className="rotate-180" />在成交明细中查看
                                        </Button>
                                      )}
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
                  <div id="backtest-records" className="flex flex-col gap-2 border-b px-4 py-2.5 sm:flex-row sm:items-center sm:justify-between">
                    <Tabs value={tableTab} onValueChange={(value) => setTableTab(value as 'trades' | 'orders' | 'withdrawals')}>
                      <TabsList>
                        <TabsTrigger value="trades">成交明细<span className="ml-1 font-mono text-[10px] text-muted-foreground">{tradesTotal}</span></TabsTrigger>
                        <TabsTrigger value="orders">委托记录<span className="ml-1 font-mono text-[10px] text-muted-foreground">{ordersTotal}</span></TabsTrigger>
                        {withdrawalsTotal > 0 && <TabsTrigger value="withdrawals">提现记录<span className="ml-1 font-mono text-[10px] text-muted-foreground">{withdrawalsTotal}</span></TabsTrigger>}
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
                  {tableTab === 'trades' && <Table><TableHeader><TableRow><TableHead>成交时间</TableHead><TableHead>标的</TableHead><TableHead>方向</TableHead><TableHead>用途</TableHead><TableHead className="text-right">价格</TableHead><TableHead className="text-right">数量</TableHead><TableHead className="text-right">成交额</TableHead><TableHead className="text-right">费用</TableHead><TableHead className="text-right">已实现盈亏</TableHead></TableRow></TableHeader><TableBody>
                    {tradesTotal === 0 ? <TableEmpty colSpan={9} message="本次回测没有成交记录" /> : tradeFilterOn && recordItems.length === 0 && !recordLoading ? <TableEmpty colSpan={9} message="没有符合条件的成交记录" action={<Button variant="outline" size="sm" type="button" onClick={clearTradeFilters}>清空筛选</Button>} /> : recordItems.map((trade, index) => <TableRow key={String(trade.id ?? `${trade.timestamp}-${index}`)}><TableCell>{dateTime(trade.timestamp)}</TableCell><TableCell><strong className="font-mono text-[12px]" title={trade.symbol}>{displaySymbol(trade.symbol)}</strong></TableCell><TableCell><span className={cn('rounded px-1.5 py-0.5 text-[10px]', lower(trade.side) === 'buy' ? 'bg-destructive/10 text-destructive' : 'bg-success/10 text-success')}>{sideLabel(trade.side)}</span></TableCell><TableCell>{purposeLabel(trade.purpose)}</TableCell><TableCell className="text-right font-mono text-[12px]">{money(trade.price)}</TableCell><TableCell className="text-right font-mono text-[12px]">{integer(trade.quantity)}</TableCell><TableCell className="text-right font-mono text-[12px]">{money(trade.amount ?? trade.notional)}</TableCell><TableCell className="text-right font-mono text-[12px]" title={feeBreakdownTitle(trade)}>{money(trade.total_fee ?? trade.fee)}</TableCell><TableCell className={cn('text-right font-mono text-[12px]', (trade.realized_pnl || 0) >= 0 ? 'text-destructive' : 'text-success')}>{trade.realized_pnl === undefined ? '—' : signedMoney(trade.realized_pnl)}</TableCell></TableRow>)}
                  </TableBody></Table>}
                  {tableTab === 'orders' && <Table><TableHeader><TableRow><TableHead>委托时间</TableHead><TableHead>标的</TableHead><TableHead>方向</TableHead><TableHead>类型</TableHead><TableHead className="text-right">限价</TableHead><TableHead className="text-right">委托数量</TableHead><TableHead className="text-right">已成交</TableHead><TableHead>状态</TableHead></TableRow></TableHeader><TableBody>
                    {ordersTotal === 0 ? <TableEmpty colSpan={8} message="本次回测没有委托记录" /> : recordItems.map((order) => <TableRow key={order.id}><TableCell>{dateTime(order.created_at || order.timestamp)}</TableCell><TableCell><strong className="font-mono text-[12px]" title={order.symbol}>{displaySymbol(order.symbol)}</strong></TableCell><TableCell><span className={cn('rounded px-1.5 py-0.5 text-[10px]', lower(order.side) === 'buy' ? 'bg-destructive/10 text-destructive' : 'bg-success/10 text-success')}>{sideLabel(order.side)}</span></TableCell><TableCell>{orderTypeLabel(order.order_type || order.type)}</TableCell><TableCell className="text-right font-mono text-[12px]">{money(order.limit_price ?? order.price)}</TableCell><TableCell className="text-right font-mono text-[12px]">{integer(order.quantity)}</TableCell><TableCell className="text-right font-mono text-[12px]">{integer(order.filled_quantity ?? order.filled_qty)}</TableCell><TableCell><Badge>{statusLabel(order.status)}</Badge></TableCell></TableRow>)}
                  </TableBody></Table>}
                  {tableTab === 'withdrawals' && <Table><TableHeader><TableRow><TableHead>日期</TableHead><TableHead className="text-right">提取金额</TableHead><TableHead>模式</TableHead><TableHead>方式</TableHead><TableHead className="text-right">当期盈利基准</TableHead><TableHead>触发来源</TableHead><TableHead className="text-right">提现后现金</TableHead></TableRow></TableHeader><TableBody>
                    {withdrawalsTotal === 0 ? <TableEmpty colSpan={7} message="本次回测没有提现记录" /> : recordItems.map((withdrawal) => <TableRow key={withdrawal.id}><TableCell>{shortDate(withdrawal.date)}</TableCell><TableCell className="text-right font-mono text-[12px] text-destructive">{money(withdrawal.amount)}</TableCell><TableCell>{withdrawal.mode === 'monthly' ? '每月提现' : '每笔提现'}</TableCell><TableCell>{withdrawal.method === 'ratio' ? '按盈利比例' : '按固定金额'}</TableCell><TableCell className="text-right font-mono text-[12px]">{signedMoney(withdrawal.period_profit)}</TableCell><TableCell className="font-mono text-[11px] text-muted-foreground">{withdrawal.trigger}</TableCell><TableCell className="text-right font-mono text-[12px]">{money(withdrawal.cash_after)}</TableCell></TableRow>)}
                  </TableBody></Table>}
                  <div className="px-4 pb-4">
                    <PaginationBar page={recordData?.page ?? recordPage} pageSize={recordData?.page_size ?? recordPageSize} total={paginationTotal} totalPages={paginationTotalPages} onPageChange={setRecordPage} onPageSizeChange={(size) => { setRecordPageSize(size); setRecordPage(1); }} disabled={recordLoading} />
                  </div>
                </Panel>
              </div>
            ) : (
              <Panel>
                <EmptyState icon={<BarChart3 className="size-6" />} title="报告数据不可用" description="该报告缺少指标数据，可能仍在生成或已被删除。" action={<Button variant="outline" size="sm" type="button" onClick={closeDetail}>返回报告列表</Button>} />
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


